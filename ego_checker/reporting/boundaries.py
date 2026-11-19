"""跨段边界章节渲染；保留显式调用的独立导出接口。"""
import collections
import csv
from datetime import datetime, timedelta, timezone
import html
from pathlib import Path

from ego_checker.analysis.boundaries import analyze_boundaries
from ego_checker.data.discovery import discover_parts

REPORT_NAME = "跨段边界测试报告.html"


def collect_boundary_rows(results, root, params=None, *, parts=None,
                          checkpoint=None, run_notice="", progress=None):
    """Collect evidence without publishing a separate report file."""
    if parts is None:
        parts, error = discover_parts(Path(root), input_mode=(params or {}).get("input_mode", "single"))
        if error:
            raise ValueError(f"跨段报告输入发现失败：{error}")
    return analyze_boundaries(parts, results, params, checkpoint=checkpoint,
                              run_notice=run_notice, progress=progress)


def render_boundary_section(rows, run_notice=""):
    """Compatibility API for callers that explicitly need one string."""
    return "".join(iter_boundary_section(rows, run_notice))


def iter_boundary_section(rows, run_notice=""):
    """Yield scoped fragments, with no nested HTML document or external files."""
    if not rows:
        return
    counts = collections.Counter(row["结论"] for row in rows)
    esc = lambda value: html.escape(str(value), quote=True)
    headers = list(rows[0]) if rows else ["结论", "说明"]
    yield '''<section id="segment-boundaries">
<style>#segment-boundaries .boundary-scroll{overflow:auto;background:white}
#segment-boundaries table{border-collapse:collapse;width:100%;white-space:nowrap}
#segment-boundaries th,#segment-boundaries td{border:1px solid #dce3ec;padding:8px;text-align:left}
#segment-boundaries th{background:#eaf0f8}
#segment-boundaries .PASS{background:#e7f6ec}
#segment-boundaries .FAIL,#segment-boundaries .ERROR{background:#ffeded}
#segment-boundaries .REVIEW,#segment-boundaries .UNVERIFIED{background:#fff7df}
#segment-boundaries p{max-width:1100px}</style><h2>跨段边界测试报告</h2>'''
    yield "<p>" + "　".join(f"{status}：{counts[status]}" for status in ("PASS", "FAIL", "REVIEW", "ERROR", "UNVERIFIED")) + "</p>"
    if run_notice:
        yield f"<p>{esc(run_notice)}</p>"
    yield '''<h3>范围与判定口径</h3><p>统计同设备、同源文件系列、同类型传感器的相邻分段连续性，结论单独统计，不替代分段内部检查。
边界间隔＝后段首时间戳−前段末时间戳。</p>
<p>间隔≥丢帧周期倍数×理论周期判FAIL（估算缺失数＝round(间隔/理论周期)−1）；正向且小于阈值判PASS；
重叠或重复判REVIEW；缺少邻段、缺段、来源歧义或证据不足判UNVERIFIED；读取或分析失败判ERROR。
本章节仅核验相机与IMU边界，音频载荷、视频解码及硬件原因不在验证范围内。</p>'''
    yield "<h3>边界明细</h3>"
    if rows:
        yield '<div class="boundary-scroll"><table><thead><tr>' + ''.join(f'<th>{esc(h)}</th>' for h in headers) + '</tr></thead><tbody>'
        for row in rows:
            yield f'<tr class="{esc(row["结论"])}">' + ''.join(f'<td>{esc(row[h])}</td>' for h in headers) + '</tr>'
        yield '</tbody></table></div>'
    else:
        yield '<p>UNVERIFIED：没有可核验的分段边界。</p>'
    yield '</section>'


def generate_boundary_report(results, out_dir, root, params=None, *, parts=None,
                             checkpoint=None, run_notice=""):
    """Explicit standalone export; GUI/CLI use the embedded section instead."""
    rows = collect_boundary_rows(results, root, params, parts=parts,
                                 checkpoint=checkpoint, run_notice=run_notice)
    esc = lambda value: html.escape(str(value), quote=True)
    headers = list(rows[0]) if rows else ["结论", "说明"]
    created = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    document = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
                '<title>跨段边界测试报告</title></head><body>'
                f'<p>生成时间：{created}（北京时间）<br>数据根目录：{esc(root)}</p>'
                + render_boundary_section(rows, run_notice))

    # Exclusive creation protects historical reports and concurrent runs.
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    index = 0
    while True:
        suffix = f"({index})" if index else ""
        target = directory / (Path(REPORT_NAME).stem + suffix + ".html")
        csv_path = target.with_suffix(".csv")
        try:
            html_stream = target.open("x", encoding="utf-8")
        except FileExistsError:
            index += 1
            continue
        try:
            csv_stream = csv_path.open("x", encoding="utf-8-sig", newline="")
        except FileExistsError:
            html_stream.close()
            target.unlink()
            index += 1
            continue
        except BaseException:
            html_stream.close()
            target.unlink()
            raise
        break
    try:
        with html_stream, csv_stream:
            writer = csv.DictWriter(csv_stream, fieldnames=headers)
            writer.writeheader()
            writer.writerows(rows)
            document += f'<p><a href="{esc(csv_path.name)}">下载边界明细CSV</a></p></body></html>'
            html_stream.write(document)
    except BaseException:
        target.unlink(missing_ok=True)
        csv_path.unlink(missing_ok=True)
        raise
    return target, csv_path
