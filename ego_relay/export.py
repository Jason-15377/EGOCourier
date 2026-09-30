"""一键打包导出：原始文件 + 解析摘要 + 对齐帧表 CSV + 异常 CSV + 自包含 HTML 报告。

输出为 zip，可整包发给他人或归档。
"""

from __future__ import annotations

import csv
import html
import io
import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import config, db
from . import __appname__


def _fmt_us(us: Optional[int]) -> str:
    if us is None:
        return ""
    return str(us)


def _fmt_local(us: Optional[int]) -> str:
    """微秒 epoch UTC -> 'YYYY-MM-DD HH:MM:SS.mmm'(本地时区)。"""
    if not us:
        return ""
    dt = datetime.fromtimestamp(us / 1_000_000, tz=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _frames_csv_rows(session_id: int, trace_id: Optional[str] = None) -> list:
    frames = db.session_frames(session_id)
    if trace_id:
        frames = [f for f in frames if f["trace_id"] == trace_id]
    rows = [["frame_index", "side", "hw_ptp_ts_us", "hw_ptp_ts_local",
             "sei_hw_ptp_ts_us", "app_recv_ts_us", "imu_ts_us", "trace_id"]]
    for f in frames:
        rows.append([f["frame_index"], f["side"], _fmt_us(f["hw_ptp_us"]),
                     _fmt_local(f["hw_ptp_us"]), _fmt_us(f["sei_hw_ptp_us"]),
                     _fmt_us(f["app_recv_us"]), _fmt_us(f["imu_us"]),
                     f["trace_id"] or ""])
    return rows


def _anomaly_csv_rows(session_id: int) -> list:
    rows = [["rule", "rule_label", "frame_index", "side", "ts_a_us", "ts_b_us",
             "delta_us", "threshold_us", "severity", "detail"]]
    for a in db.session_anomalies(session_id):
        rows.append([a["rule"], config.RULE_META.get(a["rule"], {}).get("label", a["rule"]),
                     a["frame_index"], a["side"], _fmt_us(a["ts_a_us"]), _fmt_us(a["ts_b_us"]),
                     a["delta_us"], a["threshold_us"], a["severity"], a["detail"] or ""])
    return rows


def _html_report(session_id: int, s: dict, stats: dict) -> str:
    rules = stats.get("anomalies_by_rule", [])
    by_rule = {r["rule"]: r for r in rules}
    anoms = db.session_anomalies(session_id)

    rule_rows = "".join(
        f"<tr><td>{html.escape(rule)}</td><td>{html.escape(config.RULE_META.get(rule,{}).get('label',rule))}</td>"
        f"<td>{by_rule[rule].get('n',0)}</td>"
        f"<td>{by_rule[rule].get('max_delta',0)} μs</td></tr>"
        for rule in sorted(by_rule, key=lambda k: -by_rule[k]["n"])
    ) or "<tr><td colspan=4>无异常</td></tr>"

    def _img_tag(path):
        try:
            if not path or not os.path.exists(path):
                return ""
            import base64
            data = base64.b64encode(open(path, "rb").read()).decode("ascii")
            return f'<img src="data:image/jpeg;base64,{data}" width="120" style="border:1px solid #ccc">'
        except Exception:
            return ""

    anom_rows = "".join(
        f"<tr class='{a['severity']}'><td>{html.escape(a['rule'])}</td>"
        f"<td>{a['frame_index']}</td><td>{html.escape(a['side'])}</td>"
        f"<td>{a['delta_us']}</td><td>{a['threshold_us']}</td>"
        f"<td>{html.escape(a['severity'])}</td><td>{_img_tag(a.get('frame_image'))}</td></tr>"
        for a in anoms[:300]
    ) or "<tr><td colspan=7>无</td></tr>"

    thr = s.get("meta", {}).get("thresholds_us", config.DEFAULT_THRESHOLDS_US)

    return f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>EGO Courier - 会话报告 {html.escape(s['name'])}</title>
<style>
body{{font-family:'Segoe UI','Microsoft YaHei',sans-serif;margin:24px;color:#222}}
h1{{font-size:22px}} h2{{font-size:16px;margin-top:28px;border-bottom:1px solid #ddd;padding-bottom:6px}}
table{{border-collapse:collapse;margin-top:8px;font-size:13px}}
th,td{{border:1px solid #ccc;padding:6px 10px;text-align:left}}
th{{background:#f5f5f5}}
.kpi{{display:inline-block;margin:0 24px 8px 0}}
.kpi b{{font-size:26px;display:block}}
tr.error td{{background:#fdecea}}
tr.warn td{{background:#fff7e6}}
.meta{{color:#555;font-size:13px;line-height:1.7}}
</style></head><body>
<h1>EGO Courier · 会话分析报告</h1>
<div class="meta">
会话: <b>{html.escape(s['name'])}</b><br>
设备: {html.escape(s.get('device_serial') or '-')} &nbsp; trace_id: {html.escape(s.get('trace_id') or '-')}<br>
时间范围: {_fmt_local(s.get('start_us'))} ~ {_fmt_local(s.get('end_us'))} (UTC+8)<br>
来源路径: {html.escape(s.get('source_path') or '-')}
</div>
<div class="kpi"><b>{stats.get('frames',0)}</b>帧(对齐)</div>
<div class="kpi"><b>{len(anoms)}</b>异常</div>
<div class="kpi"><b>{len(s.get('meta',{}).get('files',[]))}</b>来源文件</div>

<h2>阈值配置 (μs)</h2>
<table><tr><th>规则</th><th>阈值 μs</th><th>说明</th></tr>
{''.join(f"<tr><td>{html.escape(r)}</td><td>{thr.get(r)}</td><td>{html.escape(config.RULE_META.get(r,{}).get('detail',''))}</td></tr>" for r in thr)}
</table>

<h2>异常统计（按规则）</h2>
<table><tr><th>规则</th><th>名称</th><th>数量</th><th>最大偏差 μs</th></tr>{rule_rows}</table>

<h2>异常明细（前300条）</h2>
<table><tr><th>规则</th><th>帧</th><th>侧</th><th>偏差 μs</th><th>阈值 μs</th><th>级别</th><th>截帧</th></tr>{anom_rows}</table>

<p class="meta">本报告由 {__appname__} 生成 · {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
</body></html>"""


def export_session(session_id: int, out_path: Optional[str] = None,
                   trace_id: Optional[str] = None) -> str:
    """打包会话为 zip，返回 zip 路径。"""
    s = db.get_session(session_id)
    if not s:
        raise FileNotFoundError(f"会话不存在: {session_id}")
    stats = db.session_stats(session_id)

    # 原始文件来源目录（可能为 zip，需要提取）
    src = Path(s["source_path"])
    src_root = src
    if src.suffix.lower() == ".zip":
        from .parsers import _extract_zip
        src_root = _extract_zip(src)

    name = s["name"].replace("/", "_").replace("\\", "_") or f"session_{session_id}"
    tag = f"_{trace_id}" if trace_id else ""
    if not out_path:
        out_path = str(config.EXPORT_DIR / f"{name}{tag}_{session_id}.zip")
    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        # 原始文件
        files = db.query("SELECT * FROM log_files WHERE session_id=?", (session_id,))
        added = set()
        for f in files:
            rel = f["file_path"]
            if rel in added:
                continue
            added.add(rel)
            fp = src_root / rel
            if fp.exists():
                z.write(fp, f"raw/{rel}")

        # 解析摘要
        z.writestr("summary.json", json.dumps({
            "session_id": session_id, "name": s["name"],
            "device_serial": s.get("device_serial"), "trace_id": s.get("trace_id"),
            "start_us": s.get("start_us"), "end_us": s.get("end_us"),
            "start_local": _fmt_local(s.get("start_us")), "end_local": _fmt_local(s.get("end_us")),
            "source_path": s.get("source_path"), "stats": stats,
            "thresholds_us": s.get("meta", {}).get("thresholds_us", {}),
            "files": s.get("meta", {}).get("files", []),
        }, ensure_ascii=False, indent=2))

        # 帧表 / 异常 CSV
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\n").writerows(_frames_csv_rows(session_id, trace_id))
        z.writestr("frames.csv", buf.getvalue().encode("utf-8-sig"))
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\n").writerows(_anomaly_csv_rows(session_id))
        z.writestr("anomalies.csv", buf.getvalue().encode("utf-8-sig"))

        # 异常截帧图片（自动截帧）
        for a in db.session_anomalies(session_id):
            img = a.get("frame_image")
            if img and os.path.exists(img):
                try:
                    z.write(img, f"anomaly_frames/anom_{a['id']}_{a['rule']}_{a['frame_index']}_{a['side']}.jpg")
                except Exception:
                    pass

        # HTML 报告
        z.writestr("report.html", _html_report(session_id, s, stats).encode("utf-8"))

    return out_path
