"""HTML 分析报告导出（供 GUI「导出报告」按钮）。

包含：概览统计、内嵌图表(PNG base64)、异常区间表格、来源文件清单。
"""

from __future__ import annotations

import base64
import html
import io
from datetime import datetime, timezone
from typing import List, Optional

from ego_relay import db


def _fmt_local(us: Optional[int]) -> str:
    if not us:
        return "-"
    return (datetime.fromtimestamp(us / 1e6, tz=timezone.utc)
            .astimezone().strftime("%Y-%m-%d %H:%M:%S"))


def _fig_to_png(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110)
    buf.seek(0)
    return "data:image/png;base64," + base64.b64encode(buf.read()).decode("ascii")


def _pct(ratio: float) -> str:
    return f"{ratio * 100:.3f}%"


def build_html_report(session_id: int, stats: dict, segments: List[dict],
                      figure=None, out_path: Optional[str] = None) -> str:
    s = db.get_session(session_id)
    if s is None:
        raise FileNotFoundError(f"会话不存在: {session_id}")
    dev = db.query_one("SELECT serial FROM devices WHERE id=?", (s.get("device_id"),))
    sn = (dev or {}).get("serial") or "-"
    files = s.get("meta", {}).get("files", [])
    imu = stats.get("imu", {})
    vid = stats.get("video", {})
    sc = stats.get("script", {})

    # ---- 与 video_analyzer.py 对齐的指标块 ----
    # SEI / CSV 比对
    sei_rows = ""
    for side, v in sc.get("sei_vs_csv", {}).items():
        status = "一致" if v.get("match") else ("数量不同" if not v.get("count_match") else "不一致")
        sei_rows += (f"<tr><td>{side}</td><td>{v.get('sei_count',0)}</td>"
                     f"<td>{v.get('csv_count',0)}</td><td>{v.get('mismatch',0)}</td>"
                     f"<td>{status}</td></tr>")
    if not sei_rows:
        sei_rows = "<tr><td colspan=5>无 SEI/CSV 数据</td></tr>"

    # 视频丢帧检测
    drop_rows = ""
    for side, v in sc.get("streams", {}).items():
        state = "PASS" if v.get("gaps", 0) == 0 else "FAIL"
        drop_rows += (f"<tr><td>{side} color</td><td>{v.get('total_frames',0)}</td>"
                      f"<td>{v.get('expected_hz',0):.0f} Hz</td><td>{v.get('actual_hz',0):.2f} Hz</td>"
                      f"<td>{v.get('duration_s',0):.2f}s</td><td>{v.get('gaps',0)}</td>"
                      f"<td>{v.get('lost_frames',0)}</td>"
                      f"<td>{v.get('drop_rate',0)*100:.4f}%</td><td>{state}</td></tr>")
    if not drop_rows:
        drop_rows = "<tr><td colspan=9>无视频流</td></tr>"

    imu_sc = sc.get("imu", {})
    stereo = sc.get("stereo_sync", {})
    imuv = sc.get("imu_video_sync", {})
    sync_badge = lambda ok: ("正常" if ok else "异常")  # noqa: E731

    # 图表（可选）
    img_html = ""
    if figure is not None:
        img_html = f'<img src="{_fig_to_png(figure)}" style="max-width:100%;border:1px solid #ddd;border-radius:6px">'

    # 异常区间表格
    seg_rows = ""
    if segments:
        seg_rows = "".join(
            f"<tr><td>{_fmt_local(x.get('start_us'))}</td><td>{_fmt_local(x.get('end_us'))}</td>"
            f"<td>{x.get('max_ms', 0):.1f}</td><td>{html.escape(str(x.get('trace') or '-'))}</td></tr>"
            for x in segments)
    else:
        seg_rows = "<tr><td colspan=4>无超阈值片段</td></tr>"

    # 来源文件
    file_rows = "".join(
        f"<tr><td>{html.escape(f.get('source_type',''))}</td>"
        f"<td>{html.escape(str(f.get('side') or '-'))}</td>"
        f"<td>{f.get('count',0)}</td>"
        f"<td>{html.escape(str(f.get('file','')))}</td></tr>" for f in files)

    thr_ms = stats.get("seg_threshold_us", 50000) / 1000.0

    doc = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>EGO 分析报告 - {html.escape(s['name'])}</title>
<style>
body{{font-family:'Segoe UI','Microsoft YaHei',sans-serif;margin:26px;color:#222;background:#f7f8fa}}
h1{{font-size:20px}} h2{{font-size:15px;border-bottom:1px solid #ddd;padding-bottom:6px;margin-top:26px}}
.kpis{{display:flex;flex-wrap:wrap;gap:12px}}
.kpi{{background:#fff;border:1px solid #e2e5ea;border-radius:8px;padding:10px 16px;min-width:130px}}
.kpi b{{display:block;font-size:20px}} .kpi span{{color:#6b7280;font-size:12px}}
table{{border-collapse:collapse;background:#fff;margin-top:8px;font-size:13px;width:100%}}
th,td{{border:1px solid #e2e5ea;padding:6px 10px;text-align:left}}
th{{background:#f1f3f5}} tr:nth-child(even){{background:#fafbfc}}
.meta{{color:#555;font-size:13px;line-height:1.7}}
</style></head><body>
<h1>EGO 分析报告</h1>
<div class="meta">
会话: <b>{html.escape(s['name'])}</b><br>
设备 SN: {html.escape(sn)} &nbsp; trace_id: {html.escape(str(s.get('trace_id') or '-'))}<br>
采集时间: {_fmt_local(s.get('start_us'))} ~ {_fmt_local(s.get('end_us'))} &nbsp; 生成: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
</div>

<h2>概览统计</h2>
<div class="kpis">
  <div class="kpi"><b>{stats.get('total_frames',0)}</b><span>总样本数</span></div>
  <div class="kpi"><b>{imu.get('dropped',0)}</b><span>IMU丢点</span></div>
  <div class="kpi"><b>{_pct(imu.get('ratio',0))}</b><span>IMU丢点占比</span></div>
  <div class="kpi"><b>{vid.get('dropped',0)}</b><span>视频丢帧</span></div>
  <div class="kpi"><b>{_pct(vid.get('ratio',0))}</b><span>视频丢帧占比</span></div>
  <div class="kpi"><b>{len(segments)}</b><span>PTP同步异常片段</span></div>
  <div class="kpi"><b>{stats.get('trace_matched',0)}</b><span>traceID匹配</span></div>
</div>

<h2>数据图表（阈值 ±{thr_ms:.0f} ms）</h2>
{img_html or '<p>无图表</p>'}

<h2>SEI 时间戳 vs CSV 比对</h2>
<table><tr><th>侧</th><th>SEI 数量</th><th>CSV 数量</th><th>不匹配</th><th>状态</th></tr>{sei_rows}</table>

<h2>视频丢帧检测</h2>
<table><tr><th>数据源</th><th>总帧数</th><th>预期速率</th><th>实际速率</th><th>时长</th><th>丢帧事件</th><th>丢失帧</th><th>丢帧率</th><th>状态</th></tr>{drop_rows}</table>

<h2>IMU 丢点检测</h2>
<table><tr><th>总样本</th><th>预期速率</th><th>丢点事件</th><th>丢失</th><th>丢点率</th><th>状态</th></tr>
<tr><td>{imu_sc.get('total',0)}</td><td>{imu_sc.get('expected_hz',0):.0f} Hz</td>
<td>{imu_sc.get('gaps',0)}</td><td>{imu_sc.get('lost',0)}</td>
<td>{imu_sc.get('drop_rate',0)*100:.4f}%</td>
<td>{"PASS" if imu_sc.get('lost',0)==0 else "FAIL"}</td></tr></table>

<h2>左右 Color 时间戳同步检测</h2>
<table><tr><th>匹配对数</th><th>平均偏差</th><th>最大偏差</th><th>同步阈值</th><th>状态</th></tr>
<tr><td>{stereo.get('matched_pairs',0)}</td><td>{stereo.get('avg_diff_us',0):.1f} µs</td>
<td>{stereo.get('max_diff_us',0)} µs</td><td>{stereo.get('threshold_us',0):.0f} µs</td>
<td>{sync_badge(stereo.get('is_synced'))}</td></tr></table>

<h2>IMU 与视频时间戳同步检测</h2>
<table><tr><th>平均偏移</th><th>最大偏移</th><th>最小偏移</th><th>同步阈值</th><th>超标数</th><th>状态</th></tr>
<tr><td>{imuv.get('avg_off_us',0):.1f} µs</td><td>{imuv.get('max_off_us',0)} µs</td>
<td>{imuv.get('min_off_us',0)} µs</td><td>{imuv.get('threshold_us',0):.0f} µs</td>
<td>{imuv.get('outliers',0)}</td><td>{sync_badge(imuv.get('is_synced'))}</td></tr></table>

<h2>同步异常区间</h2>
<table><tr><th>起始时间</th><th>结束时间</th><th>最大偏移(ms)</th><th>归属traceID</th></tr>{seg_rows}</table>

<h2>来源文件</h2>
<table><tr><th>类型</th><th>侧</th><th>行数</th><th>文件</th></tr>{file_rows}</table>
</body></html>"""
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(doc)
    return doc
