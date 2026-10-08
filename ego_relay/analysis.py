"""对齐/关联 + 四类异常计算。

关联（方案A 强关联，按会话）：
  viewer_app.log(recv_hw_frame_idx) -> 匹配 hardware_ptp 的 frame_index 拿 hw_ptp_ts
  -> 再以 hw_ptp_ts 匹配 mp4_pts 的 sei_hw_ptp_ts -> 合并进 frame_entries。
  trace_id 取自 app 日志 / 会话级。

异常规则（阈值可配置，默认见 config.DEFAULT_THRESHOLDS_US）：
  app_receive_delay  |app_local_receive_ts − hw_ptp_ts|      > 50ms
  sei_copy_error     |sei_hw_ptp_ts − hw_ptp_ts|             >  2ms
  binocular_ptp_diff |left.hw_ptp − right.hw_ptp| (同帧号)   >  1ms
  imu_video_sync     |imu_sample_ts − hw_ptp_ts| (最近IMU)   >  2ms
"""

from __future__ import annotations

import bisect
import json
from pathlib import Path
from typing import Dict, List, Optional

from . import config, db
from .parsers import ImuData, ParsedSession

SEVERITY = {"error": "异常", "warn": "警告"}


def _nearest_ts(ts_us: int, sorted_ts: List[int]) -> Optional[int]:
    if not sorted_ts:
        return None
    i = bisect.bisect_left(sorted_ts, ts_us)
    cands = []
    if i < len(sorted_ts):
        cands.append(sorted_ts[i])
    if i > 0:
        cands.append(sorted_ts[i - 1])
    return min(cands, key=lambda x: abs(x - ts_us))


# ---------------------------------------------------------------------------
# 采集质量统计（IMU 丢点 / 视频丢帧 / PTP 异常片段 / trace 命中）
# ---------------------------------------------------------------------------
def _imu_drop_stats(imu: List[ImuData]) -> dict:
    """按 IMU 时间戳间隔估算丢点。期望间隔取正常小间隔的中位数。"""
    ts = sorted(x.ts_us for x in imu)
    total = len(ts)
    if total < 2:
        return {"total": total, "dropped": 0, "ratio": 0.0}
    diffs = [b - a for a, b in zip(ts, ts[1:]) if b > a]
    if not diffs:
        return {"total": total, "dropped": 0, "ratio": 0.0}
    diffs.sort()
    med = diffs[len(diffs) // 2] or 1
    # 正常间隔约为中位数 ±50%；超出视为丢点
    dropped = 0
    for d in diffs:
        if d > med * 1.5:
            dropped += max(0, int(round(d / med)) - 1)
    exp_total = total + dropped
    return {"total": total, "dropped": dropped,
            "ratio": dropped / exp_total if exp_total else 0.0}


def _video_drop_stats(hw_ptp: dict) -> dict:
    """按帧序号连续性估算视频丢帧。"""
    total = 0
    dropped = 0
    for side, m in hw_ptp.items():
        idx = sorted(m.keys())
        total += len(idx)
        for a, b in zip(idx, idx[1:]):
            gap = b - a
            if gap > 1:
                dropped += gap - 1
    exp = total + dropped
    return {"total": total, "dropped": dropped,
            "ratio": dropped / exp if exp else 0.0}


def _ptp_anomaly_segments(frame_rows: List[tuple], thr_us: int) -> List[dict]:
    """连续超过阈值的时间偏移片段（按 left 目 hw_ptp 基准，偏移 = 视图中取最大组差）。"""
    # frame_rows: (side, fi, hw, sei, app, imu, trace)
    left = [(fi, hw, sei, app) for (side, fi, hw, sei, app, imu, trace) in frame_rows
            if side == "left" and hw is not None]
    left.sort(key=lambda x: x[0])
    segs: List[dict] = []
    cur = None
    for fi, hw, sei, app in left:
        off = max([abs((app or hw) - hw), abs((sei or hw) - hw)])
        over = off > thr_us
        if over:
            if cur is None:
                cur = {"start_us": hw, "end_us": hw, "max_us": off, "frames": 1, "trace": None}
            else:
                cur["end_us"] = hw
                cur["max_us"] = max(cur["max_us"], off)
                cur["frames"] += 1
        else:
            if cur is not None:
                segs.append(cur)
                cur = None
    if cur is not None:
        segs.append(cur)
    return segs


# ---------------------------------------------------------------------------
# 与 video_analyzer.py 对齐的指标（纯 Python 实现，移植自参考脚本）
#   SEI/CSV 比对、丢帧检测、IMU 丢点、左右同步、IMU-视频同步
# ---------------------------------------------------------------------------
def _median_ts(ts):
    s = sorted(ts)
    return s[len(s) // 2]


def _auto_rate(ts):
    """从时间戳中位数间隔推算采样率(Hz)，同脚本 auto_detect_rate。"""
    if len(ts) < 10:
        return 30.0
    sample = sorted(ts[:200])
    deltas = [b - a for a, b in zip(sample, sample[1:]) if b > a]
    if not deltas:
        return 30.0
    med = _median_ts(deltas)
    if med <= 0:
        return 30.0
    return 1_000_000.0 / med


def _detect_gaps(ts, rate, mult=1.5):
    """丢帧检测（同脚本 detect_gaps）。返回 (事件数, 丢失帧数, 缺口列表)。"""
    ts = sorted(ts)
    n = len(ts)
    if n < 2:
        return 0, 0, []
    iv = 1_000_000.0 / rate
    thr = iv * mult
    events = lost = 0
    gaps = []
    for i in range(n - 1):
        d = ts[i + 1] - ts[i]
        if d > thr:
            l = min(max(1, round(d / iv) - 1), n)
            events += 1
            lost += l
            gaps.append((ts[i], ts[i + 1], d, l))
    return events, lost, gaps


def _compare_sei_csv(sei: dict, csv_ts: dict):
    """SEI 时间戳 vs CSV 时间戳比对（同脚本 compare_sei_csv）。"""
    sei_set = set(sei.values())
    csv_set = set(csv_ts.values())
    not_in_csv = [v for v in sei.values() if v not in csv_set]
    not_in_sei = [v for v in csv_ts.values() if v not in sei_set]
    return {"sei_count": len(sei), "csv_count": len(csv_ts),
            "count_match": len(sei) == len(csv_ts),
            "match": not not_in_csv and not not_in_sei,
            "mismatch": len(not_in_csv) + len(not_in_sei)}


def _stereo_sync(left_ts, right_ts):
    """左右 Color 时间戳同步（同脚本 check_stereo_sync）。"""
    left_ts = sorted(left_ts)
    right_ts = sorted(right_ts)
    if not left_ts or not right_ts:
        return {"matched_pairs": 0, "avg_diff_us": 0.0, "max_diff_us": 0,
                "threshold_us": 0.0, "is_synced": False}
    iv = _median_ts([b - a for a, b in zip(left_ts, left_ts[1:]) if b > a]) or 33_333
    thr = iv / 2.0
    li = ri = matched = 0
    diffs = []
    while li < len(left_ts) and ri < len(right_ts):
        d = abs(left_ts[li] - right_ts[ri])
        if d <= thr:
            matched += 1
            diffs.append(d)
            li += 1
            ri += 1
        elif left_ts[li] < right_ts[ri]:
            li += 1
        else:
            ri += 1
    return {"matched_pairs": matched,
            "avg_diff_us": (sum(diffs) / len(diffs)) if diffs else 0.0,
            "max_diff_us": max(diffs) if diffs else 0,
            "threshold_us": thr, "is_synced": matched > 0}


def _imu_video_sync(imu_ts, vid_ts, thr_us):
    """IMU 与视频时间戳同步偏移（同脚本 _calc_imu_video_offset）。"""
    imu = sorted(imu_ts)
    if not imu or not vid_ts:
        return {"avg_off_us": 0.0, "max_off_us": 0, "min_off_us": 0,
                "threshold_us": thr_us, "is_synced": False}
    offsets = []
    outliers = 0
    for vt in vid_ts:
        idx = bisect.bisect_left(imu, vt)
        if idx < len(imu):
            off = abs(imu[idx] - vt)
        else:
            off = abs(imu[-1] - vt)
        if idx > 0 and abs(imu[idx - 1] - vt) < off:
            off = abs(imu[idx - 1] - vt)
        offsets.append(off)
        if off > thr_us:
            outliers += 1
    return {"avg_off_us": sum(offsets) / len(offsets),
            "max_off_us": max(offsets), "min_off_us": min(offsets),
            "threshold_us": thr_us, "outliers": outliers,
            "is_synced": sum(1 for o in offsets if o <= thr_us) > 0}


def compute_script_metrics(parsed: ParsedSession) -> dict:
    """移植 video_analyzer.py 的指标，返回 dict。"""
    imu_ts = sorted(x.ts_us for x in parsed.imu)
    imu_rate = 1000.0
    imu_events, imu_lost, imu_gaps = _detect_gaps(imu_ts, imu_rate)
    imu_total = len(imu_ts)

    streams = {}
    sei_csv = {}
    for side, m in parsed.hw_ptp.items():
        ts = sorted(m.values())
        rate = _auto_rate(ts)
        events, lost, gaps = _detect_gaps(ts, rate)
        dur = (ts[-1] - ts[0]) / 1e6 if len(ts) >= 2 else 0
        actual = (len(ts) - 1) / dur if dur > 0 else 0
        streams[side] = {
            "total_frames": len(ts), "expected_hz": rate, "duration_s": dur,
            "actual_hz": actual, "gaps": events, "lost_frames": lost,
            "drop_rate": (lost / (len(ts) + lost)) if (len(ts) + lost) else 0.0,
        }
        sei_csv[side] = _compare_sei_csv(m, parsed.sei_hw_ptp.get(side, {}))

    stereo = _stereo_sync(list(parsed.hw_ptp.get("left", {}).values()),
                          list(parsed.hw_ptp.get("right", {}).values()))
    imu_sync = _imu_video_sync(imu_ts, list(parsed.hw_ptp.get("left", {}).values()),
                               stereo.get("threshold_us") or 16_667)

    return {
        "sei_vs_csv": sei_csv,
        "streams": streams,
        "imu": {"total": imu_total, "expected_hz": imu_rate,
                "gaps": imu_events, "lost": imu_lost,
                "drop_rate": (imu_lost / (imu_total + imu_lost)) if (imu_total + imu_lost) else 0.0},
        "stereo_sync": stereo,
        "imu_video_sync": imu_sync,
    }


def align_and_analyze(parsed: ParsedSession, thresholds_us: Optional[Dict[str, int]] = None,
                      session_id: Optional[int] = None) -> dict:
    """把解析结果对齐为 frame_entries 并计算异常，写库，返回统计。

    session_id 缺省时新建会话；给定则就地重置该会话并重算（用于附加 app 日志/改阈值后重算）。
    """
    thr = dict(config.DEFAULT_THRESHOLDS_US)
    if thresholds_us:
        thr.update(thresholds_us)

    # 会话级设备 / 起止时间
    device_id = db.create_device(parsed.device_serial or parsed.name, name=parsed.name) \
        if parsed.device_serial else db.create_device(parsed.name, name=parsed.name)

    # 该会话覆盖的帧号集合：以 hardware_ptp 为主，合并 mp4_pts
    frame_idx_by_side: Dict[str, set] = {}
    for side in set(parsed.hw_ptp) | set(parsed.sei_hw_ptp):
        s = set(parsed.hw_ptp.get(side, {}))
        s |= set(parsed.sei_hw_ptp.get(side, {}))
        frame_idx_by_side[side] = s

    imu_ts = [x.ts_us for x in parsed.imu]

    # app 日志：recv_hw_frame_idx -> (app_recv_us, trace_id)
    app_by_frame: Dict[int, tuple] = {}
    for line in parsed.viewer_lines:
        if line.recv_hw_frame_idx is not None:
            recv = line.app_recv_us or line.wallclock_us
            if recv:
                app_by_frame.setdefault(line.recv_hw_frame_idx,
                                        (recv, line.trace_id))

    frame_rows: List[tuple] = []
    anom_rows: List[tuple] = []
    all_frame_indices: set = set()
    for side, idxs in frame_idx_by_side.items():
        for fi in sorted(idxs):
            hw = parsed.hw_ptp.get(side, {}).get(fi)
            sei = parsed.sei_hw_ptp.get(side, {}).get(fi)
            app, trace = app_by_frame.get(fi, (None, None))
            if trace is None:
                trace = parsed.trace_id
            imu = _nearest_ts(hw, imu_ts) if hw else None
            frame_rows.append((side, fi, hw, sei, app, imu, trace))
            all_frame_indices.add(fi)

            # 规则 2：SEI 拷贝错误
            if hw and sei:
                delta = abs(sei - hw)
                if delta > thr["sei_copy_error"]:
                    anom_rows.append(_anom("sei_copy_error", fi, side, sei, hw, delta,
                                           thr["sei_copy_error"]))
            # 规则 1：应用接收延迟 —— 应用一次收到一帧（双目对），只按帧报一次，避免左右重复
            if hw and app and side == "left":
                delta = abs(app - hw)
                if delta > thr["app_receive_delay"]:
                    anom_rows.append(_anom("app_receive_delay", fi, "frame", app, hw, delta,
                                           thr["app_receive_delay"]))
            # 规则 4：IMU-视频同步
            if hw and imu is not None:
                delta = abs(imu - hw)
                if delta > thr["imu_video_sync"]:
                    anom_rows.append(_anom("imu_video_sync", fi, side, imu, hw, delta,
                                           thr["imu_video_sync"]))

    # 规则 3：双目 PTP 同步差（同 frame_index）
    left = parsed.hw_ptp.get("left", {})
    right = parsed.hw_ptp.get("right", {})
    for fi in sorted(set(left) & set(right)):
        d = abs(left[fi] - right[fi])
        if d > thr["binocular_ptp_diff"]:
            anom_rows.append(_anom("binocular_ptp_diff", fi, "binocular", left[fi], right[fi],
                                   d, thr["binocular_ptp_diff"]))

    # 起止时间（优先 hw_ptp 真值；无硬件 PTP 时回退到 IMU/SEI/日志等所有时间戳）
    # 过滤低于 1973 年的“垃圾时间戳”（帧号/序号被误当 us 时会出现 ~1e6 的值）
    _MIN_TS_US = 100_000_000_000
    hw_all = [v for m in parsed.hw_ptp.values() for v in m.values() if v >= _MIN_TS_US]
    if hw_all:
        start_us, end_us = min(hw_all), max(hw_all)
    else:
        ts_all = [x.ts_us for x in parsed.imu if x.ts_us >= _MIN_TS_US]
        ts_all += [v for m in parsed.sei_hw_ptp.values()
                   for v in m.values() if v >= _MIN_TS_US]
        ts_all += [l.wallclock_us for l in parsed.viewer_lines
                   if l.wallclock_us and l.wallclock_us >= _MIN_TS_US]
        if ts_all:
            start_us, end_us = min(ts_all), max(ts_all)
        else:
            start_us = end_us = None

    # 采集质量统计（供 GUI 概览面板 / 图表 / 报告使用）
    seg_thr = thr.get("app_receive_delay", 50_000)  # 同步异常片段阈值（默认 50ms）
    # ---- 数据校验：统计被“垃圾时间戳”过滤掉的量（低于 1973 年的异常值） ----
    _floor = 100_000_000_000
    data_quality = {
        "floor_us": _floor,
        "garbage": {
            "hw_ptp": sum(1 for m in parsed.hw_ptp.values() for v in m.values() if v < _floor),
            "sei": sum(1 for m in parsed.sei_hw_ptp.values() for v in m.values() if v < _floor),
            "imu": sum(1 for x in parsed.imu if x.ts_us < _floor),
            "viewer": sum(1 for l in parsed.viewer_lines
                          if l.wallclock_us and l.wallclock_us < _floor),
        },
    }
    stats = {
        "total_frames": len(frame_rows),
        "imu": _imu_drop_stats(parsed.imu),
        "video": _video_drop_stats(parsed.hw_ptp),
        "ptp_anomaly_segments": _ptp_anomaly_segments(frame_rows, seg_thr),
        "trace_matched": sum(1 for r in frame_rows if r[6]),
        "seg_threshold_us": seg_thr,
        "data_quality": data_quality,
        "script": compute_script_metrics(parsed),   # 与 video_analyzer.py 对齐的指标
    }

    # 同一来源路径只保留一条会话：已存在则就地重算，否则新建（避免重复分析产生多条）
    if session_id is None:
        session_id = db.find_session_by_source(parsed.source_path)

    if session_id is None:
        session_id = db.create_session(
            device_id, parsed.name, parsed.source_path, parsed.trace_id,
            start_us, end_us,
            {"files": parsed.files, "thresholds_us": thr, "stats": stats},
        )
    else:
        # 就地重置并重算：清空旧帧/异常/文件/关联，更新会话行
        db.reset_session(session_id)
        db.exec_write("DELETE FROM trace_sessions WHERE session_id=?", (session_id,))
        db.exec_write(
            "UPDATE sessions SET device_id=?, name=?, source_path=?, trace_id=?, "
            "start_us=?, end_us=?, status='ok', meta=? WHERE id=?",
            (device_id, parsed.name, parsed.source_path, parsed.trace_id,
             start_us, end_us, json.dumps({"files": parsed.files, "thresholds_us": thr,
                                           "stats": stats}, ensure_ascii=False), session_id),
        )

    db.exec_many(
        "INSERT INTO frame_entries(session_id,side,frame_index,hw_ptp_us,sei_hw_ptp_us,"
        "app_recv_us,imu_us,trace_id) VALUES(?,?,?,?,?,?,?,?)",
        [(session_id, side, fi, hw, sei, app, imu, trace)
         for (side, fi, hw, sei, app, imu, trace) in frame_rows],
    )
    _flush_anomalies(anom_rows, session_id)
    _attach_frame_images(session_id, frame_rows, parsed.frame_images)
    db.exec_many(
        "INSERT INTO log_files(session_id,source_type,side,file_path,parsed,count,note) "
        "VALUES(?,?,?,?,?,?,?)",
        [(session_id, f["source_type"], f.get("side"), f.get("file"), f.get("parsed", 0),
          f.get("count", 0), f.get("note")) for f in parsed.files],
    )
    if parsed.trace_id:
        db.exec_write("INSERT INTO trace_sessions(trace_id,session_id) VALUES(?,?)",
                      (parsed.trace_id, session_id))

    return {"session_id": session_id, "frames": len(frame_rows),
            "anomalies": len(anom_rows),
            "start_us": start_us, "end_us": end_us,
            "thresholds_us": thr}


def _anom(rule: str, fi: int, side: str, a: int, b: int, delta: int, thr: int) -> tuple:
    return (rule, fi, side, a, b, delta, thr,
            config.SEVERITY_BY_RULE.get(rule, "warn"),
            config.RULE_META.get(rule, {}).get("detail", ""))


def _flush_anomalies(anom_rows: List[tuple], session_id: int) -> None:
    now = db.now_us()
    rows = [(session_id, r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], None, now)
            for r in anom_rows]
    db.exec_many(
        "INSERT INTO anomalies(session_id,rule,frame_index,side,ts_a_us,ts_b_us,delta_us,"
        "threshold_us,severity,detail,frame_image,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )


def _resolve_img(frame_images: dict, side: str, ts_us: Optional[int], tol_us: int = 500_000) -> Optional[str]:
    """按硬件时间戳匹配采样帧 jpg（优先精确，其次最近且在容差内）。"""
    if not ts_us:
        return None
    if (side, ts_us) in frame_images:
        return frame_images[(side, ts_us)]
    # 最近匹配
    cands = [(s, t) for (s, t) in frame_images if s == side and abs(t - ts_us) <= tol_us]
    if not cands:
        return None
    best = min(cands, key=lambda st: abs(st[1] - ts_us))
    return frame_images[best]


def _attach_frame_images(session_id: int, frame_rows: List[tuple], frame_images: dict) -> None:
    """异常入库后，按 (frame_index, side) 反查硬件时间戳并回填采样帧图片。"""
    frame_hw = {(r[0], r[1]): r[2] for r in frame_rows if r[2] is not None}
    for a in db.query("SELECT id, frame_index, side FROM anomalies WHERE session_id=?",
                      (session_id,)):
        side_key = a["side"] if a["side"] in ("left", "right") else "left"
        hw = frame_hw.get((side_key, a["frame_index"]))
        if hw is None:
            continue
        img = _resolve_img(frame_images, side_key, hw)
        if img:
            db.exec_write("UPDATE anomalies SET frame_image=? WHERE id=?", (img, a["id"]))


def recompute_session(session_id: int, thresholds_us: Optional[Dict[str, int]] = None) -> dict:
    """按现有 log_files 重新解析（阈值变更或附加 app 日志后重算），就地更新同一会话。"""
    return align_and_analyze(_reparse_from_db(session_id), thresholds_us, session_id=session_id)


def attach_app_log(session_id: int, app_log_path: str) -> dict:
    """把 EGOViewer app 日志关联进已有会话，复制到项目附件区并就地重算。

    app 日志提供 trace_id(sessionId) 与应用接收时刻，从而让 app_receive_delay 可计算。
    """
    src = Path(app_log_path)
    if not src.is_file():
        raise FileNotFoundError(f"app 日志不存在: {app_log_path}")

    att_dir = Path(config.DATA_ROOT) / "attachments" / f"session_{session_id}"
    att_dir.mkdir(parents=True, exist_ok=True)
    dst = att_dir / src.name
    import shutil
    shutil.copy2(src, dst)

    from .parsers import parse_viewer_log
    lines = parse_viewer_log(dst)
    # 避免重复附加同一文件
    exists = db.query_one(
        "SELECT id FROM log_files WHERE session_id=? AND file_path=?", (session_id, str(dst)))
    if not exists:
        db.exec_write(
            "INSERT INTO log_files(session_id,source_type,side,file_path,parsed,count) "
            "VALUES(?,?,?,?,?,?)",
            (session_id, "viewer_app", "left", str(dst), 1, len(lines)))

    return recompute_session(session_id)


def _reparse_from_db(session_id: int) -> ParsedSession:
    """从已存 log_files 的路径重建解析输入（文件需仍存在）。"""
    from .parsers import ParsedSession
    s = db.get_session(session_id)
    files = db.query("SELECT * FROM log_files WHERE session_id=?", (session_id,))
    root = Path(s["source_path"])
    if root.suffix.lower() == ".zip":
        from .parsers import _extract_zip
        root = _extract_zip(Path(s["source_path"]))
    parsed = ParsedSession(name=s["name"], source_path=s["source_path"])
    parsed.trace_id = s["trace_id"]
    for f in files:
        fp = root / f["file_path"]
        parsed.files.append({"source_type": f["source_type"], "side": f["side"],
                             "file": f["file_path"], "parsed": f["parsed"],
                             "count": f["count"], "note": f["note"]})
        if not fp.exists():
            continue
        stype, side = f["source_type"], f["side"]
        if stype == "hardware_ptp":
            for d in _parse_hw(fp):
                parsed.hw_ptp.setdefault(side, {})[d.frame_index] = d.hw_ptp_us
        elif stype == "mp4_pts":
            for d in _parse_pts(fp):
                parsed.sei_hw_ptp.setdefault(side, {})[d.frame_index] = d.sei_hw_ptp_us
        elif stype == "imu":
            parsed.imu.extend(_parse_imu(fp))
        elif stype == "viewer_app":
            parsed.viewer_lines.extend(_parse_viewer(fp))
    from .parsers import discover_frame_images
    parsed.frame_images = discover_frame_images(root)
    # 会话级 trace_id：由 app 日志中最常见的 sessionId 决定
    from collections import Counter
    tr_counter = Counter(x.trace_id for x in parsed.viewer_lines if x.trace_id)
    if tr_counter:
        parsed.trace_id = tr_counter.most_common(1)[0][0]
    return parsed


def _parse_hw(fp):
    from .parsers import parse_hardware_ptp
    return parse_hardware_ptp(fp, "left")


def _parse_pts(fp):
    from .parsers import parse_mp4_pts
    return parse_mp4_pts(fp, "left")


def _parse_imu(fp):
    from .parsers import parse_imu
    return parse_imu(fp)


def _parse_viewer(fp):
    from .parsers import parse_viewer_log
    return parse_viewer_log(fp)
