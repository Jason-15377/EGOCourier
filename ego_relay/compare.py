"""多会话对比：横向比较多个会话的关键指标，并生成汇总 HTML。

供桌面 GUI 的「对比选中 / 导出汇总报告」与 Web 端 `/api/sessions/compare` 复用。
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import db


def _fmt(us):
    if not us:
        return "-"
    return (datetime.fromtimestamp(us / 1e6, tz=timezone.utc)
            .astimezone().strftime("%Y-%m-%d %H:%M:%S"))


def compare_sessions(session_ids) -> list:
    """按会话逐个提取可对比指标，返回 [ {指标}, ... ]（顺序与传入一致）。"""
    rows = []
    for sid in session_ids:
        s = db.get_session(sid)
        if not s:
            continue
        meta = s.get("meta") or {}
        stats = meta.get("stats", {})
        sc = stats.get("script", {})
        streams = sc.get("streams", {}) or {}
        drop = max((v.get("lost_frames", 0) for v in streams.values()), default=0)
        drop_rate = max((v.get("drop_rate", 0) for v in streams.values()), default=0)
        stereo = sc.get("stereo_sync") or {}
        imuv = sc.get("imu_video_sync") or {}
        dq = stats.get("data_quality") or {}
        anom = db.session_stats(sid).get("anomalies", 0)
        rows.append({
            "id": sid,
            "name": s.get("name", ""),
            "serial": db.query_one("SELECT serial FROM devices WHERE id=?",
                                   (s.get("device_id"),)) or {},
            "start": s.get("start_us"),
            "end": s.get("end_us"),
            "frames": stats.get("total_frames", 0),
            "anomalies": anom,
            "drop_frames": drop,
            "drop_rate": drop_rate,
            "stereo_match": stereo.get("matched_pairs", 0),
            "stereo_avg": stereo.get("avg_diff_us", 0),
            "stereo_synced": stereo.get("is_synced", True),
            "imuv_avg": imuv.get("avg_off_us", 0),
            "imuv_synced": imuv.get("is_synced", True),
            "garbage_ts": sum((dq.get("garbage") or {}).values()),
        })
    return rows


def compare_table_rows(rows) -> list:
    """把 compare_sessions 结果转成适合表格的行（每个会话一行，纯值）。"""
    out = []
    for r in rows:
        serial = r["serial"].get("serial") if isinstance(r["serial"], dict) else r["serial"]
        out.append({
            "会话": f"#{r['id']} {r['name']}",
            "设备": serial or "-",
            "开始时间": _fmt(r["start"]),
            "帧数": r["frames"],
            "异常": r["anomalies"],
            "丢帧数": r["drop_frames"],
            "丢帧率": f"{r['drop_rate'] * 100:.3f}%",
            "左右匹配": r["stereo_match"],
            "左右均值(µs)": f"{r['stereo_avg']:.1f}",
            "左右同步": "✅" if r["stereo_synced"] else "✗",
            "IMU均值(µs)": f"{r['imuv_avg']:.1f}",
            "IMU同步": "✅" if r["imuv_synced"] else "✗",
            "垃圾时间戳": r["garbage_ts"],
        })
    return out


def compare_html(session_ids) -> str:
    """生成多会话汇总对比的自包含 HTML。"""
    rows = compare_sessions(session_ids)
    trs = compare_table_rows(rows)
    headers = ["会话", "设备", "开始时间", "帧数", "异常", "丢帧数", "丢帧率",
               "左右匹配", "左右均值(µs)", "左右同步", "IMU均值(µs)", "IMU同步", "垃圾时间戳"]
    html = ["<html lang='zh'><head><meta charset='utf-8'><title>多会话对比汇总</title><style>",
            "body{font-family:'Microsoft YaHei',sans-serif;background:#f5f6f8;color:#222;margin:0}",
            "h1{background:#1f2937;color:#fff;padding:16px 24px;margin:0;font-size:20px}",
            ".wrap{padding:20px;max-width:1300px;margin:0 auto}",
            "table{border-collapse:collapse;width:100%;font-size:13px;background:#fff}",
            "th,td{border:1px solid #e5e7eb;padding:6px 8px;text-align:left}",
            "th{background:#f9fafb;font-weight:600}tr:hover{background:#f0f7ff}",
            ".err{color:#b91c1c}.ok{color:#047857}.sum td{background:#eef2ff;font-weight:600}",
            "</style></head><body>"]
    html.append(f"<h1>多会话对比汇总（{len(rows)} 条）</h1><div class='wrap'>")
    html.append("<table><tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr>")
    total = {h: 0 for h in headers}
    for tr in trs:
        html.append("<tr>" + "".join(f"<td>{tr[h]}</td>" for h in headers) + "</tr>")
        for h in headers:
            v = tr[h]
            if h in ("帧数", "异常", "丢帧数", "左右匹配", "垃圾时间戳") and isinstance(v, int):
                total[h] += v
    if rows:
        n = len(rows)
        sum_row = {
            "会话": "合计/平均", "设备": "-", "开始时间": "-",
            "帧数": total["帧数"], "异常": total["异常"], "丢帧数": total["丢帧数"],
            "丢帧率": f"{sum(r['drop_rate'] for r in rows)/n*100:.3f}%",
            "左右匹配": total["左右匹配"],
            "左右均值(µs)": f"{sum(r['stereo_avg'] for r in rows)/n:.1f}",
            "左右同步": f"{sum(1 for r in rows if r['stereo_synced'])}/{n}",
            "IMU均值(µs)": f"{sum(r['imuv_avg'] for r in rows)/n:.1f}",
            "IMU同步": f"{sum(1 for r in rows if r['imuv_synced'])}/{n}",
            "垃圾时间戳": total["垃圾时间戳"],
        }
        html.append("<tr class='sum'>" + "".join(f"<td>{sum_row[h]}</td>" for h in headers) + "</tr>")
    html.append("</table></div></body></html>")
    return "\n".join(html)
