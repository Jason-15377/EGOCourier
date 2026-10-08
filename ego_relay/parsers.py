"""日志解析器：四类来源 + 时间戳归一化。

时间戳统一归一化到「微秒 epoch UTC」(us) 作为公共列，同时保留来源原始语义。
真值基准 = 硬件 PTP hw_ptp_ts。

来源：
  hardware_ptp  SEI 解析输出 CSV   -> (frame_index, hw_ptp_us)
  mp4_pts       MP4 pts CSV        -> (frame_index, sei_hw_ptp_us) 按行序对齐
  imu           IMU CSV            -> [(ts_us, type)]
  viewer_app    orbbec.log 文本    -> [{wallclock_us, trace_id, recv_hw_frame_idx, app_recv_us}]
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import config

# ---------------------------------------------------------------------------
# 时间戳归一化
# ---------------------------------------------------------------------------
_WALLCLOCK_RE = re.compile(r"\[?(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}\.\d{3,6})")


def to_us(value: int, unit: Optional[str] = None) -> int:
    """任意数量级时间戳 -> 微秒 epoch UTC。unit 缺省时按数量级自动判定。"""
    v = int(value)
    if unit == "us":
        return v
    if unit == "ns":
        return v // 1000
    if unit == "ms":
        return v * 1000
    # 自动判定：ns(~1e18) / us(~1e15) / ms(~1e12)
    if v > config.UNIT_BOUNDS["ns"]:
        return v // 1000
    if v > config.UNIT_BOUNDS["us"]:
        return v
    if v > config.UNIT_BOUNDS["ms"]:
        return v * 1000
    return v  # 极小值视为 us（多为 0/1 之类哨兵值）


def wallclock_to_us(s: str, tz_hours: int = config.VIEWER_TZ_HOURS) -> int:
    """'2026-09-15 14:33:09.237'（本地 tz_hours）-> epoch 微秒 UTC。"""
    s = s.strip()
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        try:
            dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return 0
    # 假定本地墙钟 = UTC + tz_hours
    epoch_s = dt.replace(tzinfo=timezone.utc).timestamp() - tz_hours * 3600
    return int(round(epoch_s * 1_000_000))


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class SeiData:
    """硬件 PTP（SEI csv）。"""
    frame_index: int
    hw_ptp_us: int


@dataclass
class PtsData:
    """MP4 pts csv（SEI 内拷贝的硬件 PTP）。按行序对帧号。"""
    frame_index: int
    sei_hw_ptp_us: int


@dataclass
class ImuData:
    ts_us: int
    kind: str


@dataclass
class ViewerLine:
    wallclock_us: int
    trace_id: Optional[str] = None
    recv_hw_frame_idx: Optional[int] = None
    app_recv_us: Optional[int] = None
    raw: str = ""


@dataclass
class ParsedSession:
    """一次导入会话的解析结果（已按 (side, frame_index) 聚合对齐前数据）。"""
    name: str
    source_path: str
    device_serial: Optional[str] = None
    trace_id: Optional[str] = None
    hw_ptp: Dict[str, Dict[int, int]] = field(default_factory=dict)      # side -> {frame_index: us}
    sei_hw_ptp: Dict[str, Dict[int, int]] = field(default_factory=dict)  # side -> {frame_index: us}
    app_recv: Dict[str, Dict[int, int]] = field(default_factory=dict)    # side -> {frame_index: app_recv_us}
    imu: List[ImuData] = field(default_factory=list)
    viewer_lines: List[ViewerLine] = field(default_factory=list)
    files: List[dict] = field(default_factory=list)  # log_files 记录
    frame_images: Dict[tuple, str] = field(default_factory=dict)  # (side, ts_us) -> jpg 路径


def _side_of(path: str) -> str:
    low = path.lower()
    if "right" in low or "_r_" in low:
        return "right"
    return "left"  # 默认视为左目（双目光学），可被配置覆盖


# ---------------------------------------------------------------------------
# CSV 列定位（兼容别名）
# ---------------------------------------------------------------------------
def _find_col(header: List[str], aliases: List[str]) -> Optional[int]:
    for i, h in enumerate(header):
        if h.strip().lower() in [a.lower() for a in aliases]:
            return i
    return None


def parse_hardware_ptp(path: Path, side: str) -> List[SeiData]:
    """SEI csv：frame_index,timestamp_us（兼容 frame_idx/hw_ptp_ts 等别名）。"""
    out: List[SeiData] = []
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        rdr = csv.reader(f)
        try:
            header = next(rdr)
        except StopIteration:
            return out
        ci = _find_col(header, config.COL_ALIASES["frame_index"])
        ti = _find_col(header, config.COL_ALIASES["hw_ptp_ts"])
        if ci is None or ti is None:
            return out
        for row in rdr:
            if len(row) <= max(ci, ti):
                continue
            try:
                fi = int(float(row[ci]))
                us = to_us(float(row[ti]))
            except (ValueError, TypeError):
                continue
            if us and fi >= 0:
                out.append(SeiData(fi, us))
    out.sort(key=lambda x: x.frame_index)
    return out


def parse_mp4_pts(path: Path, side: str, start_index: int = 1) -> List[PtsData]:
    """MP4 pts csv：单列 timestamp_us（或含 frame_no/sei_hw_ptp_ts）。按行序对帧号。"""
    out: List[PtsData] = []
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        rdr = csv.reader(f)
        try:
            header = next(rdr)
        except StopIteration:
            return out
        fi_col = _find_col(header, config.COL_ALIASES["frame_index"])
        ti_col = _find_col(header, config.COL_ALIASES["sei_hw_ptp_ts"])
        if ti_col is None:
            ti_col = _find_col(header, config.COL_ALIASES["hw_ptp_ts"])
        if ti_col is None:
            return out
        idx = start_index
        for row in rdr:
            if len(row) <= ti_col:
                continue
            try:
                us = to_us(float(row[ti_col]))
            except (ValueError, TypeError):
                idx += 1
                continue
            if us:
                fi = int(float(row[fi_col])) if fi_col is not None else idx
                out.append(PtsData(fi, us))
            idx += 1
    out.sort(key=lambda x: x.frame_index)
    return out


def parse_imu(path: Path) -> List[ImuData]:
    """IMU csv：timestamp_us,x,y,z,type（兼容 imu_sample_ts）。"""
    out: List[ImuData] = []
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        rdr = csv.reader(f)
        try:
            header = next(rdr)
        except StopIteration:
            return out
        ti = _find_col(header, config.COL_ALIASES["imu_sample_ts"])
        ki = None
        for i, h in enumerate(header):
            if h.strip().lower() == "type":
                ki = i
                break
        if ti is None:
            return out
        for row in rdr:
            if len(row) <= ti:
                continue
            try:
                us = to_us(float(row[ti]))
            except (ValueError, TypeError):
                continue
            if us:
                kind = row[ki].strip() if (ki is not None and len(row) > ki) else ""
                out.append(ImuData(us, kind))
    out.sort(key=lambda x: x.ts_us)
    return out


# ---------------------------------------------------------------------------
# EGOViewer 应用日志（ego-viewer-*.log / orbbec.log 文本）
# 真实字段（经实测）：
#   行首时间戳 = app 接收时刻（本地 wallclock，UTC+8，毫秒）
#   sessionId=<uuid>      —— 会话/trace 标识（真实 trace id，UUID 形式）
#   trace_id:"<32hex>"    —— 规格描述形式（兼容保留）
#   frameIndex / leftFirstIndex / rightFirstIndex  —— 硬件帧序号
#   deviceTimestampUs / *TimestampUs / ptsUs       —— 硬件设备时间戳(us)
#   app_local_receive_ts  —— 规格描述的接收时刻字段（兼容保留）
# ---------------------------------------------------------------------------
_TRACE_RE = re.compile(
    r"sessionId=([0-9a-fA-F]{8}-[0-9a-fA-F-]{27})|trace_id[:=]\s*[\"']?([0-9a-fA-F]{32})[\"']?"
)
_RECV_IDX_RE = re.compile(
    r"recv_hw_frame_(?:idx|index)[:=]\s*(\d+)|frameIndex=(\d+)|leftFirstIndex=(\d+)|rightFirstIndex=(\d+)"
)
_RECV_TS_RE = re.compile(r"app_local_receive_ts[:=]\s*(\d+)")


def parse_viewer_log(path: Path, tz_hours: int = config.VIEWER_TZ_HOURS) -> List[ViewerLine]:
    out: List[ViewerLine] = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _WALLCLOCK_RE.search(line)
            if not m:
                continue
            wall = wallclock_to_us(m.group(1), tz_hours)
            if not wall:
                continue
            tr = _TRACE_RE.search(line)
            trace = None
            if tr:
                trace = (tr.group(1) or tr.group(2) or "").lower() or None
            ri = _RECV_IDX_RE.search(line)
            fi = None
            if ri:
                fi = next((int(g) for g in ri.groups() if g is not None), None)
            rt = _RECV_TS_RE.search(line)
            app_ts = to_us(int(rt.group(1))) if rt else wall  # 缺字段时用行首时间戳
            out.append(ViewerLine(
                wallclock_us=wall,
                trace_id=trace,
                recv_hw_frame_idx=fi,
                app_recv_us=app_ts,
                raw=line.rstrip("\n"),
            ))
    return out


# ---------------------------------------------------------------------------
# 会话导入 / 文件发现
# ---------------------------------------------------------------------------
def discover_files(root: Path) -> List[Tuple[str, str, Path]]:
    """返回 [(source_type, side, path)]，按配置 glob 递归发现。排除音频等无关文件。"""
    found: List[Tuple[str, str, Path]] = []
    for stype, pats in config.SOURCE_PATTERNS.items():
        for pat in pats:
            for p in root.glob(pat):
                if not p.is_file():
                    continue
                # 音频时间戳不是相机视频帧，排除
                if "audio" in str(p).lower():
                    continue
                side = _side_of(str(p))
                found.append((stype, side, p))
    return found


def _session_name_from_path(p: Path) -> str:
    name = p.name
    if p.suffix.lower() == ".zip":
        name = p.stem
    # 去掉 ego-logs- 时间戳前缀等杂质，尽量保留语义名
    return name.strip() or "session"


# 截帧图片文件名：..._camera_left_part0001_<hw_ptp_us>.jpg
_IMG_TS_RE = re.compile(r"_(\d+)\.jpg$")
_IMG_SIDE_RE = re.compile(r"_(left|right)_")


def discover_frame_images(root: Path) -> Dict[tuple, str]:
    """发现 frame_dumps 采样帧 jpg，键 (side, ts_us)，值为绝对路径。"""
    out: Dict[tuple, str] = {}
    for p in root.glob("**/frame_dumps/**/*.jpg"):
        m = _IMG_TS_RE.search(p.name)
        if not m:
            continue
        ts = int(m.group(1))
        sm = _IMG_SIDE_RE.search(p.name)
        side = sm.group(1) if sm else "left"
        out[(side, ts)] = str(p)
    return out


def _extract_zip(zip_path: Path) -> Path:
    import hashlib
    import zipfile

    digest = hashlib.sha256(str(zip_path.resolve()).encode("utf-8")).hexdigest()[:16]
    target = config.IMPORT_TMP / (zip_path.stem + "_" + digest)
    if target.exists():
        return target
    target.mkdir(parents=True, exist_ok=True)
    root = target.resolve()
    with zipfile.ZipFile(zip_path) as z:
        for member in z.infolist():
            candidate = (root / member.filename).resolve()
            if candidate != root and root not in candidate.parents:
                raise ValueError(f"压缩包包含不安全路径: {member.filename}")
            mode = (member.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise ValueError(f"压缩包包含不支持的符号链接: {member.filename}")
        z.extractall(root)
    return target


def import_path(path: str) -> ParsedSession:
    """导入一个本地目录或 zip，解析全部来源文件并聚合。"""
    src = Path(path)
    if not src.exists():
        raise FileNotFoundError(f"路径不存在: {path}")

    work = _extract_zip(src) if src.is_file() and src.suffix.lower() == ".zip" else src
    parsed = ParsedSession(
        name=_session_name_from_path(src),
        source_path=str(path),
    )

    # 设备序列号：从会话目录名提取 EGO_<SERIAL>_ 段
    m = re.search(r"EGO[-_]?([A-Za-z0-9]+)", parsed.name)
    if m:
        parsed.device_serial = m.group(1)

    files = discover_files(work)
    for stype, side, fp in files:
        rel = str(fp.relative_to(work))
        try:
            if stype == "hardware_ptp":
                rows = parse_hardware_ptp(fp, side)
                parsed.hw_ptp.setdefault(side, {})
                for d in rows:
                    parsed.hw_ptp[side][d.frame_index] = d.hw_ptp_us
                parsed.files.append({"source_type": stype, "side": side, "file": rel,
                                     "parsed": 1, "count": len(rows)})
            elif stype == "mp4_pts":
                rows = parse_mp4_pts(fp, side)
                parsed.sei_hw_ptp.setdefault(side, {})
                for d in rows:
                    parsed.sei_hw_ptp[side][d.frame_index] = d.sei_hw_ptp_us
                parsed.files.append({"source_type": stype, "side": side, "file": rel,
                                     "parsed": 1, "count": len(rows)})
            elif stype == "imu":
                rows = parse_imu(fp)
                parsed.imu.extend(rows)
                parsed.files.append({"source_type": stype, "side": side, "file": rel,
                                     "parsed": 1, "count": len(rows)})
            elif stype == "viewer_app":
                lines = parse_viewer_log(fp)
                parsed.viewer_lines.extend(lines)
                # 会话级 trace_id：取该日志中首个出现且出现次数最多的 trace
                from collections import Counter
                tr_counter = Counter(x.trace_id for x in lines if x.trace_id)
                if tr_counter:
                    parsed.trace_id = parsed.trace_id or tr_counter.most_common(1)[0][0]
                parsed.files.append({"source_type": stype, "side": side, "file": rel,
                                     "parsed": 1, "count": len(lines)})
        except Exception as e:  # noqa: BLE001 —— 单个文件失败不阻塞整包
            parsed.files.append({"source_type": stype, "side": side, "file": rel,
                                 "parsed": 0, "count": 0, "note": str(e)})

    parsed.frame_images = discover_frame_images(work)
    return parsed
