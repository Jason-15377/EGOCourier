"""reporting.outputs 的实现。"""
import csv
import html
import re
import os
import string
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from ego_checker.domain.constants import ROLE_LABELS
from ego_checker.domain.models import AbnormalFrame, PartResult
from ego_checker.analysis.timing import expected_interval_us
from ego_checker.runtime.version import APP_VERSION
from ego_checker.reporting.columns import ReportColumns, SHORT_LABEL, _dev_short, _imu_context_entries


def generate_result_csv(results: List[PartResult], out_path: Path,
                        params: Optional[Dict] = None) -> Path:
    cols = ReportColumns(results)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols.headers())
        for r in results:
            w.writerow(cols.row_cells(r))
    return out_path


_PROBLEM_LABELS = {"FAIL": "异常", "ERROR": "执行异常", "UNVERIFIED": "未验证"}


def problem_parts(results: List[PartResult]) -> List[Dict]:
    """列出未通过的 Part 及原因（与明细表列口径一致）。

    仅用于完成提示与报告快速定位，不改变统计口径；whitelisted 的 Part
    （用户已确认豁免）不列入。每项包含：
      index   在 results 中的下标（HTML 行锚点 part-{index+1} 使用同一顺序）
      name    Part 名
      status  FAIL / ERROR / UNVERIFIED
      label   中文状态
      reasons [(列名, 单元格文本), ...] 失败的检查列
    """
    if not results:
        return []
    cols = ReportColumns(results)
    headers = cols.headers()
    problems: List[Dict] = []
    for index, r in enumerate(results):
        if getattr(r, "whitelisted", False):
            continue
        status = str(getattr(r, "business_status", "") or "")
        if status == "PASS":
            continue
        cells = cols.row_cells(r)
        reasons = []
        # 最后一列是业务结论，原因只取各检查列（与 headers 对齐）。
        for header, cell in zip(headers[:-1], cells[:-1]):
            text = str(cell or "")
            if text.startswith(("FAIL", "ERROR")):
                reasons.append((str(header), text))
        if not reasons:
            error_text = str(getattr(r, "error", "") or "").strip()
            failed = [str(key) for key, ok in (getattr(r, "checks", {}) or {}).items()
                      if not ok]
            if error_text:
                reasons.append(("执行异常", error_text))
            elif failed:
                reasons.append(("未通过检查",
                                "、".join(failed[:5]) + (" 等" if len(failed) > 5 else "")))
            elif status == "UNVERIFIED":
                reasons.append(("未验证", "没有可执行的检查项"))
        problems.append({
            "index": index,
            "name": str(getattr(r, "name", "") or f"Part {index + 1}"),
            "status": status or "FAIL",
            "label": _PROBLEM_LABELS.get(status, status or "异常"),
            "reasons": reasons,
        })
    return problems


def compute_stats(results: List[PartResult]) -> Dict:
    total = len(results)
    valid = [r for r in results if not r.error]
    abnormal = [r for r in valid if r.business_status == "FAIL" and not r.whitelisted]
    normal = [r for r in valid if r.overall_pass]
    whitelisted = [r for r in valid if r.whitelisted]
    collect_s = sum(r.duration_s for r in valid)
    pass_s = sum(r.duration_s for r in normal)
    fail_s = sum(r.duration_s for r in abnormal)
    return {
        "total": total, "valid": len(valid), "abnormal": len(abnormal),
        "errors": sum(r.business_status == "ERROR" for r in results),
        "unverified": sum(r.business_status == "UNVERIFIED" for r in results),
        "audio_unverified": sum(any(
            e.status != "N/A"
            and (e.continuity == "UNVERIFIED"
                 or ((e.had_timestamps if e.had_timestamps is not None else bool(e.timestamps))
                     and not (e.had_sizes if e.had_sizes is not None else bool(e.sizes))))
            for e in r.audio_evidence) for r in results),
        "audio_suspected": sum(any(e.continuity == "SUSPECTED"
                                   for e in r.audio_evidence) for r in results),
        "normal": len(normal), "whitelisted": len(whitelisted),
        "collect_s": collect_s, "pass_s": pass_s, "fail_s": fail_s,
    }


_HTML_TPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Ego同步帧测试报告 {version}</title>
<style>
:root{{
  --blue:#3370ff; --blue-hover:#2e64e0;
  --green:#00b42a; --amber:#d97706; --red:#f53f3f; --red-dark:#e34d59;
  --text:#1f2329; --text2:#4e5969; --muted:#86909c;
  --bd:#e4e6eb; --grid:#f0f1f3; --hover:#f7f8fa; --sel:#e8f3ff; --bg:#f7f8fa;
}}
*{{box-sizing:border-box;}}
body{{font-family:"Segoe UI","Microsoft YaHei",sans-serif;margin:0;background:var(--bg);color:var(--text);font-size:13px;}}
.wrap{{max-width:1600px;margin:0 auto;padding:24px 22px 60px;}}
h1{{font-size:18px;margin:0;font-weight:700;color:#1d2129;letter-spacing:0.5px;}}
.sub{{color:var(--muted);font-size:12px;font-weight:500;letter-spacing:0.2px;margin:6px 0 20px;}}
h2{{font-size:15px;margin:24px 0 12px;font-weight:600;color:#1d2129;}}
.card{{background:#fff;border:1px solid var(--bd);border-radius:8px;padding:16px 18px;margin-bottom:16px;}}
.kv{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px 28px;}}
.kv .k{{color:var(--muted);font-size:12px;font-weight:500;letter-spacing:0.2px;}}
.kv .v{{font-weight:600;font-size:13px;color:var(--text);}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;}}
.stat{{background:#fff;border:1px solid var(--bd);border-radius:8px;padding:14px;text-align:center;}}
.stat .n{{font-size:24px;font-weight:700;color:#1d2129;}}
.stat .l{{color:var(--muted);font-size:12px;font-weight:500;letter-spacing:0.2px;margin-top:4px;}}
.tablewrap{{max-height:70vh;overflow:auto;border:1px solid var(--bd);border-radius:8px;scroll-padding-top:42px;}}
table{{border-collapse:collapse;width:100%;background:#fff;font-size:13px;color:var(--text);}}
th,td{{border:1px solid var(--grid);padding:6px 10px;text-align:left;vertical-align:top;
  white-space:normal;overflow-wrap:anywhere;word-break:break-word;}}
.tablewrap > table > thead th{{position:sticky;top:0;background:#f5f7fa;z-index:2;font-weight:600;color:var(--text2);font-size:12px;}}
tbody tr:hover{{background:var(--hover);}}
tr.row-abnormal td{{background:#fff8f8;}}
tr.row-abnormal td:first-child{{box-shadow:inset 3px 0 0 var(--red);}}
tr.row-normal td{{background:#fff;}}
tr.row-wl td{{color:var(--muted);}}
tr.row-main{{scroll-margin-top:48px;}}
tr.row-main:target td{{background:#fff1f0;}}
.problem-nav ul{{margin:4px 0 0;padding-left:18px;}}
.problem-nav li{{margin:4px 0;}}
.problem-nav a{{color:var(--blue);font-weight:600;text-decoration:none;}}
.problem-nav a:hover{{text-decoration:underline;}}
.problem-nav .more{{color:var(--muted);margin-top:6px;}}
.bdg{{display:inline-block;padding:1px 8px;border-radius:4px;font-size:12px;font-weight:600;line-height:17px;vertical-align:1px;}}
.bdg.pass{{color:var(--green);background:#e8ffea;border:1px solid #b8f2c2;}}
.bdg.suspected{{color:var(--amber);background:#fffbeb;border:1px solid #fbbf24;}}
.bdg.fail{{color:var(--red);background:#ffece8;border:1px solid #ffcfc4;}}
.overn{{color:var(--red);font-weight:600;}}
.m{{color:var(--text2);}}
details.cd{{margin-top:3px;font-size:11px;contain:inline-size;max-width:100%;overflow-x:auto;}}
details.cd summary{{cursor:pointer;color:var(--blue);font-weight:600;font-size:12px;}}
details.cd table.mini{{width:100%;font-size:11px;margin-top:4px;}}
details.cd table.mini td,details.cd table.mini th{{padding:3px 8px;border:1px solid var(--grid);}}
.err{{color:var(--red);}}
.notice{{color:#92400e;background:#fffbeb;border:1px solid #fbbf24;border-radius:8px;
  padding:12px 16px;margin:0 0 18px;font-weight:600;}}
.footer{{color:var(--muted);font-size:12px;font-weight:500;letter-spacing:0.2px;margin-top:28px;text-align:center;}}
</style>
</head>
<body>
<div class="wrap">
<h1>🎥 Ego同步帧测试报告 {version}</h1>
<div class="sub">生成时间: {gen_time} ｜ 分析根目录: {root} ｜ 相机: {cam_list}</div>
{run_notice}
{analysis_notices}
{session_section}
{meta_section}

<h2>⚙ 评估参数</h2>
<div class="card"><div class="kv">
{param_cards}
</div></div>

<h2>📊 统计结果</h2>
{audio_note}<div class="card"><div class="stats">
<div class="stat"><div class="n">{st_total}</div><div class="l">Part 总数</div></div>
<div class="stat"><div class="n" style="color:var(--green)">{st_normal}</div><div class="l">正常 Part</div></div>
<div class="stat"><div class="n" style="color:var(--red)">{st_abnormal}</div><div class="l">异常 Part</div></div>
<div class="stat"><div class="n">{st_errors}</div><div class="l">执行异常 Part</div></div>
<div class="stat"><div class="n">{st_unverified}</div><div class="l">必需证据未验证 Part</div></div>
{audio_stats}<div class="stat"><div class="n">{collect_s:.1f}</div><div class="l">采集时长 (s)</div></div>
<div class="stat"><div class="n" style="color:var(--green)">{pass_s:.1f}</div><div class="l">合格时长 (s)</div></div>
<div class="stat"><div class="n" style="color:var(--red)">{fail_s:.1f}</div><div class="l">不合格时长 (s)</div></div>
</div></div>

{problem_section}

<h2>📋 明细表</h2>
<p>详情行号从 1 开始；含表头的 CSV 按物理行号减 1 显示，第一条时间戳记录为第 1 行。未知来源行号显示“-”。</p>
<div class="tablewrap">
<table>
{colgroup}
<thead><tr>{headers}</tr></thead>
<tbody>
{rows}
</tbody>
</table>
</div>

{boundary_section}
{evidence_section}
<div class="footer">Ego同步帧测试报告 — 自包含报告 · 单元格内可展开异常详情</div>
</div>
</body>
</html>
"""


def _esc(v) -> str:
    return html.escape(str(v))


_STATUS = re.compile(r"\b(PASS|SUSPECTED|FAIL)\b")


_OVERN = re.compile(r"超([0-9][0-9.]*%?)")


# 明细表单元格里的绝对路径只保留文件名：一行错误信息往往重复多份长路径，
# 完整路径放在 title 提示里，避免整行被撑成几千像素高。
_WIN_PATH = re.compile(r"[A-Za-z]:\\(?:[^\\\r\n；,()\"'<>|]+[\\/])+")


def _cell_html(text) -> str:
    """把单元格文本渲染为 HTML: 状态转彩色徽章, 超阈计数标红, 分行保留."""
    raw = str(text)
    shortened = _WIN_PATH.sub(lambda _match: "…\\", raw)
    esc = html.escape(shortened)
    esc = _STATUS.sub(
        lambda m: f"<span class='bdg {m.group(1).lower()}'>{m.group(1)}</span>", esc)
    esc = _OVERN.sub(lambda m: f"<span class='overn'>超{m.group(1)}</span>", esc)
    esc = esc.replace("\n", "<br>")
    if shortened != raw:
        return f"<div title='{html.escape(raw, quote=True)}'>{esc}</div>"
    return esc


def _more(n: int, limit: int, colspan: int) -> str:
    if n <= limit:
        return ""
    return (f"<tr><td colspan={colspan} style='color:var(--muted);text-align:center'>"
            f"... 还有 {n - limit} 条</td></tr>")


# 紧凑明细表各列的相对宽度。列宽按权重归一化，列数随设备/检查项增减时
# 仍然能被浏览器分配在表格宽度内，不会把某一列压成一条细缝。
_COMPACT_WIDTH_WEIGHTS = {
    "数据文件夹名称": 11.5,
    "Part数据名称": 4.5,
    "相机 / 帧率 / 固件": 5.5,
    "视频 / 音频时长": 10.0,
    "视频时长": 10.0,
    "相机帧数对齐": 7.5,
    "鱼眼三目帧数对齐": 7.5,
    "相机/鱼眼三目帧数对齐": 7.5,
    "Depth/Depth RGB帧数对齐": 7.5,
    "帧间隔波动(ms)": 7.5,
    "相机基准同步对齐": 7.5,
    "相机同步": 7.5,
    "IMU与相机同步": 6.5,
    "夹爪与相机同步": 6.5,
    "丢帧": 6.5,
    "音频完整性": 12.5,
    "同步与对齐": 6.5,
    "IMU频率核对": 6.5,
    "业务结论": 7.0,
}


def _compact_colgroup(headers) -> str:
    weights = [_COMPACT_WIDTH_WEIGHTS.get(header, 8.0) for header in headers]
    total = sum(weights) or 1.0
    return "".join(
        f"<col style='width:{weight / total * 100:.2f}%'>"
        for weight in weights)


def _cam_sync_over_summary(sr) -> str:
    """相机同步异常数按对端相机标明：左侧目20/1800 右目80/1800 …（分母为基准相机帧数）。"""
    if not sr.abnormal:
        return f"同步异常 {sr.over_count}/{sr.matched_frames}"
    counts = {}
    for a in sr.abnormal:
        label = a.b_label or a.b_role or '其他相机'
        counts[label] = counts.get(label, 0) + 1
    return "同步异常 " + " ".join(f"{label}{n}/{sr.total_frames}" for label, n in counts.items())


def _drop_quota(n_groups: int, limit: int = 50) -> int:
    """多相机/设备合并详情按组平均分配显示配额，取整且总数不超过 limit。"""
    return max(1, limit // n_groups) if n_groups else limit


def _alloc_drop_rows(entries, row_fn, limit: int = 50):
    """按组平均配额取异常行，总数截断到 limit；返回 (rows, total)。"""
    per = _drop_quota(len(entries), limit)
    rows = [row_fn(key, rd, a) for key, rd in entries for a in rd.abnormal[:per]]
    rows = rows[:limit]
    total = sum(len(rd.abnormal) for _key, rd in entries)
    return rows, total


def _data_row(index):
    """源数据的 0 基位置转为 1 基显示；未知位置不能显示成第 0 行。"""
    return index + 1 if index is not None and index >= 0 else "-"


def _detail_row(abnormal: AbnormalFrame, *, peer: bool = False):
    line = abnormal.b_csv_line if peer else abnormal.csv_line
    if line is not None and line >= 2:
        return line - 1
    return _data_row(abnormal.b_row_index if peer else abnormal.row_index)


def _imu_sync_threshold_text(results: List[PartResult]) -> str:
    """只展示最终同步判定保存的阈值，包含跨段重算后的值。"""
    by_device: Dict[str, set] = {}
    for result in results:
        if result.imu_error:
            continue
        for role, sync in result.imu_sync.items():
            if sync.threshold_us > 0:
                device = result.camera_device_by_role.get(role, "")
                by_device.setdefault(device, set()).add(sync.threshold_us)
    if not by_device:
        return "未执行"
    values = {value for group in by_device.values() for value in group}
    if len(values) == 1:
        suffix = "（各设备）" if len(by_device) > 1 else ""
        return f"< {next(iter(values)) / 1000:.3f} ms{suffix}"
    items = []
    for device, thresholds in sorted(by_device.items()):
        values_text = "、".join(f"< {value / 1000:.3f} ms" for value in sorted(thresholds))
        if len(thresholds) > 1:
            values_text += "（按Part实际值）"
        items.append(f"{device} {values_text}" if device else values_text)
    return "；".join(items)


def _depth_sync_threshold_text(results: List[PartResult]) -> str:
    values = {sync.threshold_us for result in results for group in ('depth', 'depth_rgb')
              for sync in result.cam_sync_groups.get(group, {}).values() if sync.threshold_us > 0}
    if not values:
        return '未执行'
    text = '、'.join(f'< {value / 1000:.3f} ms' for value in sorted(values))
    return text + ('（按Part实际值）' if len(values) > 1 else '')


def _grip_frequency_text(results: List[PartResult]) -> str:
    values = {}
    errors = False
    for result in results:
        errors = errors or bool(result.grip_frequency_error or result.grip_frequency_error_by_device)
        if result.grip_frequency_hz is not None and not result.grip_frequency_error:
            inferred = '推算' in result.grip_frequency_source
            values.setdefault('', set()).add((result.grip_frequency_hz, inferred))
        for device, rate in result.grip_frequency_hz_by_device.items():
            if device not in result.grip_frequency_error_by_device:
                inferred = '推算' in result.grip_frequency_source_by_device.get(device, '')
                values.setdefault(device, set()).add((rate, inferred))
    if not values:
        return 'ERROR 配置无效/缺失，未执行' if errors else '未执行'
    text = '；'.join(
        f'{device + ": " if device else ""}{rate:g} Hz / {1000 / rate:.9f} ms'
        + ('（推算）' if inferred else '')
        for device, rates in sorted(values.items()) for rate, inferred in sorted(rates))
    return text + ('；部分设备/分段配置ERROR' if errors else '')


def _imu_frequency_text(results: List[PartResult]) -> str:
    by_device: Dict[str, set] = {}
    error_devices = set()
    for result in results:
        if result.imu_frequency_hz is not None:
            by_device.setdefault("", set()).add(result.imu_frequency_hz)
        for device, rate in result.imu_frequency_hz_by_device.items():
            by_device.setdefault(device, set()).add(rate)
        if result.imu_frequency_error:
            error_devices.add("")
        error_devices.update(result.imu_frequency_error_by_device)
    if not by_device:
        return "ERROR 未取得IMU理论频率" if error_devices else "无IMU数据"

    def rate_text(rate):
        return f"{rate:g} Hz / {1000.0 / rate:.3f} ms"

    all_rates = {rate for rates in by_device.values() for rate in rates}
    if len(all_rates) == 1 and not error_devices:
        suffix = "（各设备）" if len(by_device) > 1 else ""
        return rate_text(next(iter(all_rates))) + suffix

    parts = []
    for device in sorted(set(by_device) | error_devices, key=str.casefold):
        label = device or "当前设备"
        rates = by_device.get(device, set())
        value = "、".join(rate_text(rate) for rate in sorted(rates))
        if len(rates) > 1:
            value += "（按Part）"
        if device in error_devices:
            value = (value + "；" if value else "") + "ERROR 未取得理论频率"
        parts.append(f"{label}: {value}")
    return "；".join(parts)


def _cam_first(role, result, camera):
    if camera.timestamps:
        return camera.timestamps[0]
    if camera.retained_first is not None:
        return camera.retained_first
    sample = (result.boundary_samples or {}).get('cameras', {}).get(role) or {}
    return sample.get('first', '-')


def _cam_last(role, result, camera):
    if camera.timestamps:
        return camera.timestamps[-1]
    if camera.retained_last is not None:
        return camera.retained_last
    sample = (result.boundary_samples or {}).get('cameras', {}).get(role) or {}
    return sample.get('last', '-')


def detail_rows(desc, *, limit=None):
    """把 row_details 的描述符转成 GUI 表格用 (标题, 表头, 行列表).

    与 _render_detail 内容保持一致 (HTML 折叠详情与窗口表格共用同一数据).
    desc 为 (kind, payload) 元组, 或 List[AbnormalFrame] (通用兜底).
    """
    def ms(v):
        return f"{v / 1000.0:.3f}"

    if isinstance(desc, tuple):
        kind, payload = desc
    else:
        kind, payload = None, desc
    if kind == "frame_count":
        r = payload
        return ("对齐详情", ["相机", "帧数", "首帧时间戳", "尾帧时间戳"],
                [[cam.label or ROLE_LABELS.get(role, role), cam.count,
                  _cam_first(role, r, cam), _cam_last(role, r, cam)]
                 for role, cam in r.cameras.items()])
    if kind == "frame_count_group":
        r, roles, title = payload
        return (title, ["相机", "帧数", "首帧时间戳", "尾帧时间戳"],
                [[r.cameras[role].label or ROLE_LABELS.get(role, role),
                  r.cameras[role].count,
                  _cam_first(role, r, r.cameras[role]),
                  _cam_last(role, r, r.cameras[role])]
                 for role in roles if role in r.cameras])
    if kind == "interval":
        iv, _role = payload
        return ("帧间隔异常", ["行号", "行号i时间戳", "行号i+1时间戳", "帧间隔(ms)", "偏差dev(ms)"],
                [[_detail_row(a), a.ts_a, a.ts_b, ms(a.value_us),
                  ms(abs(a.value_us - iv.expected_us))]
                 for a in iv.abnormal])
    if kind == "cam_sync":
        sr, role = payload
        # 「最近邻时间戳」在「最近邻相机」之前; 同时给出该相机行号
        base_label = sr.base_label or ROLE_LABELS.get(role, role)
        return (_cam_sync_over_summary(sr),
                ["行号", "同步方向", "基准相机时间戳", "最近邻时间戳",
                 "最近邻相机", "最近邻行号", "最大偏差(ms)", "阈值(ms)"],
                [[_detail_row(a),
                  f"{base_label}→{a.b_label or ROLE_LABELS.get(a.b_role, a.b_role) or '其他相机'}",
                  a.ts_a, a.ts_b,
                  a.b_label or a.b_role or "其他相机",
                  _detail_row(a, peer=True),
                  ms(a.value_us), f'<{ms(a.threshold_us)}'] for a in sr.abnormal[:limit]])
    if kind == "imu_sync":
        sr, _role = payload
        return (f"异常详情（异常帧数）{sr.over_count}/（总帧数）{sr.total_frames}",
                ["行号", "基准相机时间戳", "IMU时间戳", "IMU行号", "最大偏差(ms)", "阈值(ms)"],
                [[_detail_row(a), a.ts_a, a.ts_b,
                  _detail_row(a, peer=True),
                  ms(a.value_us), f"<{ms(a.threshold_us)}"] for a in sr.abnormal[:limit]])
    if kind == 'imu_frequency':
        result = payload
        rows = []
        for item in result.imu_frequency_audit:
            def hz(key):
                value = item[key]
                return f'{value:.6f}' if value is not None else '未验证'
            evidence = '；'.join(
                f'{source["source"]}；记录{source["records"]}条；'
                f'首尾时间戳{source["first_us"]}→{source["last_us"]}µs；'
                f'CSV物理行{source["first_csv_line"]}→{source["last_csv_line"]}'
                if source['first_csv_line'] is not None else
                f'{source["source"]}；记录{source["records"]}条；'
                f'首尾时间戳{source["first_us"]}→{source["last_us"]}µs；CSV行号不适用'
                for source in item['sources'])
            rows.append([item['device'] or '当前设备', item['sensor'], hz('configured_hz'),
                         hz('measured_hz'), item['rounded_hz'] if item['rounded_hz'] is not None else '未验证',
                         hz('reference_hz'), item['record_count'], item['interval_count'],
                         f'{item["span_us"] / 1e6:.6f}', item['configuration_source'],
                         evidence or '未取得分类源记录', item['status'] + ' ' + item['reason']])
        return ('IMU三传感器频率核对（按源内记录间隔数/首尾跨度，四舍五入整数Hz）',
                ['设备', '传感器', 'JSON频率(Hz)', '实测平均频率(Hz)', '实测整数Hz',
                 'mag基准(Hz)', '源记录数', '源内间隔数', '跨度合计(s)', '基准配置来源', '实测来源', '结论'], rows)
    if kind == 'grip_sync_unavailable':
        result, role = payload
        device = result.camera_device_by_role.get(role, '')
        error = result.unavailable_checks.get(f'grip_sync_{role}', '')
        return ('夹爪同步未执行', ['设备', '原因'],
                [[device or '本设备', error or '未执行']])
    if kind == 'grip_frequency':
        result, role = payload
        device = result.camera_device_by_role.get(role, '')
        sync = result.grip_sync.get(role)
        rate = result.grip_frequency_hz_by_device.get(device) if device else result.grip_frequency_hz
        source = (result.grip_frequency_source_by_device.get(device, '') if device
                  else result.grip_frequency_source)
        error = result.unavailable_checks.get(f'grip_sync_{role}', '')
        if error:
            status = 'ERROR ' + error
        elif '推算' in source:
            status = '推算频率（非JSON配置）'
        else:
            status = '已使用JSON频率'
        return ('夹爪频率与同步阈值', ['设备', '配置频率(Hz)', '同步阈值(ms)', '配置来源', '状态'],
                [[device or '本设备', f'{rate:g}' if rate is not None else '未验证',
                  f'<{sync.threshold_us / 1000:.9f}' if sync else '未执行',
                  source or error or '未提供配置来源', status]])
    if kind == "grip_sync":
        sr, _role = payload
        return (f"异常详情（异常帧数）{sr.over_count}/（总帧数）{sr.total_frames}",
                ["行号", "基准相机时间戳", "夹爪时间戳", "夹爪行号",
                 "最大偏差(ms)", "阈值(ms)"],
                [[_detail_row(a), a.ts_a, a.ts_b,
                  _detail_row(a, peer=True),
                  ms(a.value_us), f"<{a.threshold_us / 1000:.9f}"]
                 for a in sr.abnormal[:limit]])
    if kind == "rgb_drop":
        entries = payload
        rows, _total = _alloc_drop_rows(entries, lambda role, rd, a: [
            rd.label or ROLE_LABELS.get(role, role), _detail_row(a), a.ts_a, a.ts_b,
            ms(a.value_us), a.lost if a.anomaly_type == "丢帧" else "-", a.anomaly_type])
        return ("相机连续性异常",
                ["相机", "行号i", "行号i时间戳", "行号i+1时间戳", "帧间隔(ms)", "丢帧数", "异常类型"], rows)
    if kind == "imu_drop":
        rd = payload
        return ("IMU丢帧异常", ["行号i", "行号i时间戳", "行号i+1时间戳", "帧间隔(ms)", "丢帧数", "异常类型"],
                [[_detail_row(a), a.ts_a, a.ts_b, ms(a.value_us),
                  a.lost if a.anomaly_type == "丢帧" else "-", a.anomaly_type]
                 for a in rd.abnormal])
    if kind == "imu_drop_devices":
        entries = payload
        rows, _total = _alloc_drop_rows(entries, lambda dev, rd, a: [
            _dev_short(dev), _detail_row(a), a.ts_a, a.ts_b, ms(a.value_us),
            a.lost if a.anomaly_type == "丢帧" else "-", a.anomaly_type])
        return ("IMU丢帧异常",
                ["设备", "行号i", "行号i时间戳", "行号i+1时间戳", "帧间隔(ms)", "丢帧数", "异常类型"], rows)
    if kind == "grip_drop":
        rd = payload
        return ("夹爪丢帧异常", ["行号i", "行号i时间戳", "行号i+1时间戳", "帧间隔(ms)", "丢帧数", "异常类型"],
                [[_detail_row(a), a.ts_a, a.ts_b, ms(a.value_us),
                  a.lost if a.anomaly_type == "丢帧" else "-", a.anomaly_type]
                 for a in rd.abnormal])
    if kind == "grip_drop_devices":
        entries = payload
        rows, _total = _alloc_drop_rows(entries, lambda dev, rd, a: [
            _dev_short(dev), _detail_row(a), a.ts_a, a.ts_b, ms(a.value_us),
            a.lost if a.anomaly_type == "丢帧" else "-", a.anomaly_type])
        return ("夹爪丢帧异常",
                ["设备", "行号i", "行号i时间戳", "行号i+1时间戳", "帧间隔(ms)", "丢帧数", "异常类型"], rows)
    if kind == "audio_diagnostics":
        from ego_checker.reporting.audio import diagnostic_rows
        return ('音频首尾覆盖/波形/时长诊断', ['来源', '项目', '记录位置', '实测值', '判定依据'],
                diagnostic_rows(payload))
    if kind == "audio_continuity":
        events = [(evidence.device, event) for evidence in payload
                  for event in evidence.continuity_events]
        multi_device = len({device for device, _event in events if device}) > 1
        headers = ["CSV包位置", "当前时间戳", "下一包时间戳", "当前包采样帧数",
                   "理论下一时间戳", "偏差(ms)", "缺失/重叠采样帧数", "异常类型"]
        if multi_device:
            headers.insert(0, "设备")
        rows = []
        for device, event in events:
            row = [event.record_number, event.current_timestamp_us,
                   event.next_timestamp_us,
                   event.packet_frames if event.packet_frames is not None else "-",
                   event.expected_next_timestamp_us,
                   ms(event.deviation_us), event.affected_frames,
                   event.anomaly_type]
            if multi_device:
                row.insert(0, _dev_short(device) if device else "未知设备")
            rows.append(row)
        return ("音频丢帧/时序异常详情",
                headers, rows)
    if kind == "row_align":
        ra, roles = payload
        headers = ["行号"] + [f"{SHORT_LABEL.get(ro, ro)}时间戳" for ro in roles] + ["最大偏差(ms)"]
        rows = [[_detail_row(a)] + [a.cam_ts.get(ro, "-") for ro in roles] + [ms(a.value_us)]
                for a in ra.abnormal]
        return (f"同行同步异常 {len(ra.abnormal)}个", headers, rows)
    if kind == "sei":
        abnormal = payload
        return ("SEI异常", ["相机", "行号", "SEI时间戳", "消息/CSV时间戳", "偏差(ms)", "说明"],
                [[ROLE_LABELS.get(a.cam, a.cam), _detail_row(a),
                  a.ts_b if a.ts_b is not None else "-",
                  a.ts_a if a.ts_a is not None else "-", ms(a.value_us),
                  a.detail or "-"]
                 for a in abnormal])
    if kind in ("imu_context", "cam_context"):
        title, headers, rows = _context_detail_table(kind, payload)
        return title, headers, list(rows)
    # 通用兜底
    return ("异常详情", ["行号", "值(ms)", "阈值(ms)", "说明"],
            [[_detail_row(a), ms(a.value_us),
              ms(a.threshold_us) if a.threshold_us else "-", a.detail]
             for a in (payload or [])])


def detail_blocks(desc):
    """把一列描述符展开为 (标题, 表头, 行) 列表; 支持一格多块 (异常详情 + 跨段样本来源)."""
    if isinstance(desc, list) and desc and isinstance(desc[0], tuple):
        return [detail_rows(t) for t in desc]
    if desc:
        return [detail_rows(desc)]
    return []


def _render_detail(kind, payload) -> str:
    """按检查项渲染单元格内折叠详情表格. kind 见 ReportColumns.row_details."""
    LIMIT = 50
    if kind == "frame_count":
        r = payload
        rows = "".join(
            f"<tr><td>{_esc(cam.label or ROLE_LABELS.get(role, role))}</td><td>{cam.count}</td>"
            f"<td>{_cam_first(role, r, cam)}</td>"
            f"<td>{_cam_last(role, r, cam)}</td></tr>"
            for role, cam in r.cameras.items())
        return (f"<details class='cd'><summary>对齐详情</summary>"
                f"<table class='mini'><thead><tr><th>相机</th><th>帧数</th>"
                f"<th>首帧时间戳</th><th>尾帧时间戳</th></tr></thead>"
                f"<tbody>{rows}</tbody></table></details>")
    if kind == "frame_count_group":
        r, roles, title = payload
        rows = "".join(
            f"<tr><td>{_esc(r.cameras[role].label or ROLE_LABELS.get(role, role))}</td>"
            f"<td>{r.cameras[role].count}</td>"
            f"<td>{_cam_first(role, r, r.cameras[role])}</td>"
            f"<td>{_cam_last(role, r, r.cameras[role])}</td></tr>"
            for role in roles if role in r.cameras)
        return (f"<details class='cd'><summary>{_esc(title)}</summary>"
                f"<table class='mini'><thead><tr><th>相机</th><th>帧数</th>"
                f"<th>首帧时间戳</th><th>尾帧时间戳</th></tr></thead>"
                f"<tbody>{rows}</tbody></table></details>")
    if kind == "interval":
        iv, _role = payload
        n = len(iv.abnormal)
        body = "".join(
            f"<tr><td>{_detail_row(a)}</td><td>{a.ts_a}</td><td>{a.ts_b}</td>"
            f"<td>{a.value_us / 1000:.3f}</td>"
            f"<td>{abs(a.value_us - iv.expected_us) / 1000:.3f}</td></tr>"
            for a in iv.abnormal[:LIMIT])
        return (f"<details class='cd'><summary>异常{n}个</summary>"
                f"<table class='mini'><thead><tr><th>行号</th><th>行号i时间戳</th>"
                f"<th>行号i+1时间戳</th><th>帧间隔(ms)</th><th>偏差dev(ms)</th></tr></thead>"
                f"<tbody>{body}{_more(n, LIMIT, 5)}</tbody></table></details>")
    if kind in ('cam_sync', 'imu_sync', 'grip_sync', 'grip_frequency', 'imu_frequency'):
        title, headers, rows = detail_rows((kind, payload), limit=LIMIT)
        total_rows = len(rows) if kind in ('grip_frequency', 'imu_frequency') else len(payload[0].abnormal)
        head = ''.join(f'<th>{_esc(value)}</th>' for value in headers)
        body = ''.join('<tr>' + ''.join(f'<td>{_esc(value)}</td>' for value in row) + '</tr>'
                       for row in rows[:LIMIT])
        return (f"<details class='cd'><summary>{_esc(title)}</summary>"
                f"<table class='mini'><thead><tr>{head}</tr></thead>"
                f"<tbody>{body}{_more(total_rows, LIMIT, len(headers))}</tbody></table></details>")
    if kind == "rgb_drop":
        entries = payload
        rows, total = _alloc_drop_rows(entries, lambda role, rd, a: [
            rd.label or ROLE_LABELS.get(role, role), _detail_row(a), a.ts_a, a.ts_b,
            f"{a.value_us / 1000:.3f}", a.lost], LIMIT)
        body = "".join("<tr>" + "".join(f"<td>{_esc(v)}</td>" for v in row) + "</tr>" for row in rows)
        return (f"<details class='cd'><summary>相机丢帧</summary>"
                f"<table class='mini'><thead><tr><th>相机</th><th>行号i</th><th>行号i时间戳</th>"
                f"<th>行号i+1时间戳</th><th>帧间隔(ms)</th><th>丢帧数</th></tr></thead>"
                f"<tbody>{body}{_more(total, len(rows), 6)}</tbody></table></details>")
    if kind == "imu_drop":
        rd = payload
        n = len(rd.abnormal)
        body = "".join(
            f"<tr><td>{_detail_row(a)}</td><td>{a.ts_a}</td><td>{a.ts_b}</td>"
            f"<td>{a.value_us / 1000:.3f}</td>"
            f"<td>{a.lost if a.anomaly_type == '丢帧' else '-'}</td>"
            f"<td>{_esc(a.anomaly_type)}</td></tr>"
            for a in rd.abnormal[:LIMIT])
        return (f"<details class='cd'><summary>异常详情</summary>"
                f"<table class='mini'><thead><tr><th>行号i</th><th>行号i时间戳</th>"
                f"<th>行号i+1时间戳</th><th>帧间隔(ms)</th><th>丢帧数</th>"
                f"<th>异常类型</th></tr></thead>"
                f"<tbody>{body}{_more(n, LIMIT, 6)}</tbody></table></details>")
    if kind == "imu_drop_devices":
        entries = payload  # [(dev, DropResult)]
        rows, total = _alloc_drop_rows(entries, lambda dev, rd, a: [
            _dev_short(dev), _detail_row(a), a.ts_a, a.ts_b, f"{a.value_us / 1000:.3f}",
            a.lost if a.anomaly_type == '丢帧' else '-', a.anomaly_type], LIMIT)
        body = "".join("<tr>" + "".join(f"<td>{_esc(v)}</td>" for v in row) + "</tr>" for row in rows)
        return (f"<details class='cd'><summary>IMU丢帧</summary>"
                f"<table class='mini'><thead><tr><th>设备</th><th>行号i</th><th>行号i时间戳</th>"
                f"<th>行号i+1时间戳</th><th>帧间隔(ms)</th><th>丢帧数</th>"
                f"<th>异常类型</th></tr></thead>"
                f"<tbody>{body}{_more(total, len(rows), 7)}</tbody></table></details>")
    if kind == "grip_drop_devices":
        entries = payload  # [(dev, DropResult)]
        rows, total = _alloc_drop_rows(entries, lambda dev, rd, a: [
            _dev_short(dev), _detail_row(a), a.ts_a, a.ts_b, f"{a.value_us / 1000:.3f}",
            a.lost if a.anomaly_type == '丢帧' else '-', a.anomaly_type], LIMIT)
        body = "".join("<tr>" + "".join(f"<td>{_esc(v)}</td>" for v in row) + "</tr>" for row in rows)
        return (f"<details class='cd'><summary>夹爪丢帧</summary>"
                f"<table class='mini'><thead><tr><th>设备</th><th>行号i</th><th>行号i时间戳</th>"
                f"<th>行号i+1时间戳</th><th>帧间隔(ms)</th><th>丢帧数</th>"
                f"<th>异常类型</th></tr></thead>"
                f"<tbody>{body}{_more(total, len(rows), 7)}</tbody></table></details>")
    if kind == "grip_drop":
        _title, headers, rows = detail_rows((kind, payload))
        n = len(rows)
        header = "".join(f"<th>{_esc(value)}</th>" for value in headers)
        body = "".join(
            "<tr>" + "".join(f"<td>{_esc(value)}</td>" for value in row) + "</tr>"
            for row in rows[:LIMIT])
        return (f"<details class='cd'><summary>异常详情</summary>"
                f"<table class='mini'><thead><tr>{header}</tr></thead>"
                f"<tbody>{body}{_more(n, LIMIT, len(headers))}</tbody></table></details>")
    if kind in ("audio_continuity", "audio_diagnostics"):
        title, headers, rows = detail_rows((kind, payload))
        n = len(rows)
        header = "".join(f"<th>{_esc(value)}</th>" for value in headers)
        body = "".join(
            "<tr>" + "".join(f"<td>{_esc(value)}</td>" for value in row) + "</tr>"
            for row in rows[:LIMIT])
        return (f"<details class='cd'><summary>{_esc(title)} {n}条</summary>"
                f"<table class='mini'><thead><tr>{header}</tr></thead>"
                f"<tbody>{body}{_more(n, LIMIT, len(headers))}</tbody></table></details>")
    if kind == "row_align":
        ra, roles = payload
        n = len(ra.abnormal)
        th = "".join(f"<th>{_esc(SHORT_LABEL.get(ro, ro))}时间戳</th>" for ro in roles)
        body = "".join(
            "<tr><td>%s</td>%s<td>%.3f</td></tr>" % (
                _detail_row(a),
                "".join(f"<td>{a.cam_ts.get(ro, '-')}</td>" for ro in roles),
                a.value_us / 1000.0)
            for a in ra.abnormal[:LIMIT])
        return (f"<details class='cd'><summary>异常{n}个</summary>"
                f"<table class='mini'><thead><tr><th>行号</th>{th}<th>最大偏差(ms)</th></tr></thead>"
                f"<tbody>{body}{_more(n, LIMIT, len(roles) + 2)}</tbody></table></details>")
    if kind == "sei":
        abnormal = payload
        n = len(abnormal)
        body = "".join(
            f"<tr><td>{_esc(ROLE_LABELS.get(a.cam, a.cam))}</td><td>{_detail_row(a)}</td>"
            f"<td>{a.ts_b if a.ts_b is not None else '-'}</td>"
            f"<td>{a.ts_a if a.ts_a is not None else '-'}</td>"
            f"<td>{a.value_us / 1000:.3f}</td>"
            f"<td>{_esc(a.detail or '-')}</td></tr>"
            for a in abnormal[:LIMIT])
        return (f"<details class='cd'><summary>异常{n}个</summary>"
                f"<table class='mini'><thead><tr><th>相机</th><th>行号</th><th>SEI时间戳</th>"
                f"<th>消息/CSV时间戳</th><th>偏差(ms)</th><th>说明</th></tr></thead>"
                f"<tbody>{body}{_more(n, LIMIT, 6)}</tbody></table></details>")
    if kind == "imu_context":
        title, headers, rows = detail_rows((kind, payload))
        header = "".join(f"<th>{_esc(value)}</th>" for value in headers)
        body = "".join("<tr>" + "".join(f"<td>{_esc(value)}</td>" for value in row) + "</tr>"
                       for row in rows)
        return (f"<details class='cd'><summary>{_esc(title)}</summary>"
                f"<table class='mini'><thead><tr>{header}</tr></thead>"
                f"<tbody>{body}</tbody></table></details>")
    if kind == "cam_context":
        title, headers, rows = detail_rows((kind, payload))
        header = "".join(f"<th>{_esc(value)}</th>" for value in headers)
        body = "".join("<tr>" + "".join(f"<td>{_esc(value)}</td>" for value in row) + "</tr>"
                       for row in rows)
        return (f"<details class='cd'><summary>{_esc(title)}</summary>"
                f"<table class='mini'><thead><tr>{header}</tr></thead>"
                f"<tbody>{body}</tbody></table></details>")
    # 兜底: 仅当 payload 确为异常帧列表时才渲染通用表格。
    if isinstance(payload, list) and all(
            hasattr(item, 'csv_line') for item in payload):
        return _cell_detail_html(payload)
    return ""


def _cell_detail_html(abnormal: List[AbnormalFrame]) -> str:
    """通用异常帧折叠表格 (行号/值/阈/说明)."""
    if not abnormal:
        return ""
    body = "".join(
        f"<tr><td>{_detail_row(a)}</td><td>{a.value_us / 1000:.3f}</td>"
        f"<td>{a.threshold_us / 1000:.3f}</td><td>{_esc(a.detail)}</td></tr>"
        for a in abnormal[:50])
    more = _more(len(abnormal), 50, 4)
    return (f"<details class='cd'><summary>异常 {len(abnormal)} 帧</summary>"
            f"<table class='mini'><thead><tr><th>数据行号</th><th>值(ms)</th>"
            f"<th>阈(ms)</th><th>说明</th></tr></thead><tbody>{body}{more}"
            f"</tbody></table></details>")


def _render_session_section(results: List[PartResult]) -> str:
    return "".join(_iter_session_section(results))


def _iter_session_section(results):
    seen = set()
    for result in results:
        info = result.session_info
        if not info:
            continue
        key = (str(result.path), info.get("basis", ""), tuple(info.get("warnings", [])),
               tuple(info.get("evidence", [])))
        if key in seen:
            continue
        if not seen:
            yield ("<section><h2>多设备会话识别</h2><p>会话元数据警告单独展示；"
                   "传感器检查及总判定仍按实际采集数据计算。</p>")
        seen.add(key)
        # 报告不展示会话告警框；告警保留在结果与运行日志中。
        evidence = "".join(f"<li>{_esc(item)}</li>" for item in info.get("evidence", []))
        yield (f"<div><h3>{_esc(str(result.path))}</h3>"
                      f"<p>识别依据：{_esc(info.get('basis', ''))}</p>"
                      f"<details><summary>会话证据与原始开始时间</summary><ul>{evidence}</ul></details></div>")
    if seen:
        yield "</section>"


def _iter_meta_section(results: List[PartResult]):
    """元数据完整性/准确性检测 section；无检测项时不显示。"""
    seen = {}
    for r in results:
        for item in getattr(r, 'meta_integrity', []) or []:
            seen.setdefault((item.source, item.path, item.status), item)
    if not seen:
        return
    items = list(seen.values())
    problems = [i for i in items if i.status in ('ERROR', 'WARN')]
    passed = sum(1 for i in items if i.status == 'PASS')
    yield ('<section><h2>🧾 元数据完整性与准确性</h2>'
           '<p>独立于传感器判定；ERROR/WARN 逐项列出，完好数据继续分析。</p>')
    if not problems:
        yield f'<p>元数据完整（{len(items)} 项检测通过）。</p>'
    else:
        yield ('<div class="tablewrap"><table><thead><tr><th>状态</th><th>类别</th>'
               '<th>字段路径</th><th>产品</th><th>期望</th><th>实测</th><th>来源</th></tr></thead><tbody>')
        for i in sorted(problems, key=lambda x: (x.status != 'ERROR', x.source, x.path)):
            cls = 'fail' if i.status == 'ERROR' else 'suspected'
            yield (f"<tr><td><span class='bdg {cls}'>{i.status}</span></td><td>{_esc(i.category)}</td>"
                   f"<td>{_esc(i.path)}</td><td>{_esc(i.product)}</td><td>{_esc(i.expected)}</td>"
                   f"<td>{_esc(i.actual)}</td><td>{_esc(i.source)}</td></tr>")
        yield '</tbody></table></div>'
    yield '</section>'


def _render_meta_section(results: List[PartResult]) -> str:
    return "".join(_iter_meta_section(results))


def _render_analysis_notices(results: List[PartResult]) -> str:
    return "".join(_iter_analysis_notices(results))


def _iter_analysis_notices(results):
    """报告不展示分段告警框；告警仍保留在结果与运行日志中供排查."""
    return
    yield  # pragma: no cover - 保持生成器语义


def _iter_html_template(fields):
    formatter = string.Formatter()
    for literal, name, spec, conversion in formatter.parse(_HTML_TPL):
        yield literal
        if name is None:
            continue
        value = fields[name]
        if name in ("rows", "boundary_section", "session_section", "analysis_notices", "meta_section"):
            yield from value
        else:
            if conversion:
                value = formatter.convert_field(value, conversion)
            yield formatter.format_field(value, spec)


def _context_detail_table(kind, payload):
    """Keep GUI's list API while HTML consumes context rows incrementally."""
    if kind == "imu_context":
        multi = any(device for device, _, _, _ in _imu_context_entries(payload))
        actual_matches = any(match is not None for _, _, _, match in _imu_context_entries(payload))
        headers = (["方向", "源分段", "文件", "当前Part相机行号", "相机时间戳(µs)",
                    "源IMU数据行号(1基)", "IMU时间戳(µs)", "偏差(ms)"] if actual_matches else
                   ["方向", "源分段", "文件", "引用帧数", "引用范围(µs)", "边界时间戳(µs)"])
        if multi:
            headers.insert(0, "设备")

        def rows():
            for device, direction, sample, match in _imu_context_entries(payload):
                row = ["前一段" if direction == "previous" else "后一段", sample.part,
                       Path(sample.source).name if sample.source else "-"]
                if actual_matches:
                    source_row = "-"
                    if match.ts_b in sample.timestamps_us:
                        source_pos = sample.timestamps_us.index(match.ts_b)
                        if source_pos < len(sample.row_indices):
                            source_row = _data_row(sample.row_indices[source_pos])
                    row.extend([_detail_row(match), match.ts_a, source_row,
                                match.ts_b, f"{match.value_us / 1000:.3f}"])
                else:
                    row.extend([len(sample.timestamps_us),
                        f"{sample.timestamps_us[0]}..{sample.timestamps_us[-1]}" if sample.timestamps_us else "-",
                        sample.timestamp_us])
                if multi:
                    row.insert(0, device or "-")
                yield row
        return "跨段样本来源", headers, rows()

    ctx, matches = payload
    def rows():
        for match in matches:
            direction, sample, source_row = "", None, "-"
            for candidate_direction, candidate in ctx.items():
                candidate_ts = candidate.extra_ts.get(match.b_role, [])
                if match.ts_b in candidate_ts:
                    direction, sample = candidate_direction, candidate
                    source_pos = candidate_ts.index(match.ts_b)
                    candidate_rows = candidate.extra_rows.get(match.b_role, [])
                    if source_pos < len(candidate_rows):
                        source_row = _data_row(candidate_rows[source_pos])
                    break
            source = sample.sources.get(match.b_role, sample.source) if sample is not None else None
            yield ["前一段" if direction == "previous" else "后一段" if direction == "next" else "-",
                   sample.neighbor_part if sample is not None else "-",
                   Path(source).name if source else "-",
                   (f"{match.cam_label or ROLE_LABELS.get(match.cam, match.cam) or '-'} → "
                    f"{match.b_label or ROLE_LABELS.get(match.b_role, match.b_role)}"),
                   _detail_row(match), match.ts_a if match.ts_a is not None else "-", source_row,
                   match.ts_b if match.ts_b is not None else "-", f"{match.value_us / 1000:.3f}"]
    return ("跨段样本来源", ["方向", "源分段", "文件", "同步方向", "当前Part相机行号",
            "当前帧时间戳(µs)", "最近邻源行号", "最近邻时间戳(µs)", "偏差(ms)"], rows())


def _iter_render_detail(kind, payload):
    if kind in ("imu_context", "cam_context"):
        title, headers, rows = _context_detail_table(kind, payload)
        header = "".join(f"<th>{_esc(value)}</th>" for value in headers)
        yield (f"<details class='cd'><summary>{_esc(title)}</summary>"
               f"<table class='mini'><thead><tr>{header}</tr></thead><tbody>")
        for row in rows:
            yield "<tr>" + "".join(f"<td>{_esc(value)}</td>" for value in row) + "</tr>"
        yield "</tbody></table></details>"
    else:
        yield _render_detail(kind, payload)


def _clip_text(text, limit=80) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:max(1, limit - 1)] + "…"


def _render_problem_section(results: List[PartResult]) -> str:
    """报告顶部的问题快速定位卡片（无问题时整段省略）。"""
    problems = problem_parts(results)
    if not problems:
        return ""
    limit = 20
    items = []
    for item in problems[:limit]:
        reasons = "；".join(
            f"{_esc(header)}：{_esc(_clip_text(text, 72))}"
            for header, text in item["reasons"][:2])
        if len(item["reasons"]) > 2:
            reasons += f"；另有 {len(item['reasons']) - 2} 项"
        suffix = f" — {reasons}" if reasons else ""
        items.append(
            f"<li><a href='#part-{item['index'] + 1}'>{_esc(item['name'])}</a>"
            f"（{_esc(item['label'])}）{suffix}</li>")
    more = (f"<div class='more'>… 其余 {len(problems) - limit} 个问题 Part "
            "见下方明细表</div>" if len(problems) > limit else "")
    return (
        "<h2>🚨 问题快速定位</h2>"
        "<div class='card problem-nav'>"
        f"<p>本次共 {len(problems)} 个问题 Part（异常 / 执行异常 / 未验证），点击名称跳到对应明细行。</p>"
        f"<ul>{''.join(items)}</ul>{more}</div>"
    )


def _iter_report_rows(results, cols):
    for index, r in enumerate(results):
        cells = cols.row_cells(r)
        details = cols.row_details(r)
        cls = []
        if r.error:
            cls.append("row-abnormal")
        elif r.whitelisted:
            cls.append("row-wl")
        elif not r.overall_pass:
            cls.append("row-abnormal")
        else:
            cls.append("row-normal")
        clsstr = " class='row-main " + " ".join(cls) + "'"
        yield f"<tr id='part-{index + 1}'{clsstr}>"
        for ci, cval in enumerate(cells):
            yield "<td>"
            yield _cell_html(cval)
            d = details[ci] if ci < len(details) else None
            if isinstance(d, tuple):        # (kind, payload)
                yield from _iter_render_detail(*d)
            elif isinstance(d, list):       # 一列多个详情块 (异常详情 + 跨段样本来源)
                if d and isinstance(d[0], tuple):
                    for blk in d:
                        yield from _iter_render_detail(*blk)
                elif d:                     # List[AbnormalFrame] (row_align / sei)
                    yield from _iter_render_detail(None, d)
            elif d:
                yield from _iter_render_detail(None, d)
            yield "</td>"
        yield "</tr>\n"


def generate_html(results: List[PartResult], out_path: Path, root: Path,
                  params: Optional[Dict] = None,
                  firmware_map: Optional[Dict[str, str]] = None,
                  run_notice: str = "", boundary_rows: Optional[List[Dict]] = None,
                  evidence=None, *, checkpoint=None) -> Path:
    params = params or {}
    # 固件版本映射 (part名 -> 固件)
    if firmware_map:
        for r in results:
            if r.name in firmware_map:
                r.firmware = firmware_map[r.name]

    stats = compute_stats(results)
    # HTML 报告用紧凑列：同一检查项的每相机结果在单元格内纵向排列，
    # 避免几十列把明细表撑到需要左右横移。CSV/窗口结果表仍用完整列。
    cols = ReportColumns(results, compact=True)
    header_list = cols.headers()
    headers = "".join(f"<th>{_esc(h)}</th>" for h in header_list)
    colgroup = _compact_colgroup(header_list)

    cam_list = " / ".join(cols.cam_label.get(role, role) for role in cols.roles) or "-"
    p = params
    # 是否含深度流 (深度产品): 决定是否追加 depth 相关卡控标准卡片
    has_depth = any(role in cols.roles for role in ('depth', 'depth_rgb'))

    # RGB 丢帧阈值 = 1.5 × 帧间隔基准, 换算为最终 ms 值。
    # 优先用帧间隔检查已记录的基准值，避免依赖已被流式释放的原始时间戳。
    rgb_mult = p.get("rgb_drop_mult", 1.5)
    rgb_ms = None
    for r in results:
        for role, c in r.cameras.items():
            interval = r.intervals.get(role)
            if interval and interval.expected_us > 0:
                rgb_ms = interval.expected_us * rgb_mult / 1000.0
            elif len(c.timestamps) >= 2:
                rgb_ms = expected_interval_us(c.timestamps) * rgb_mult / 1000.0
            if rgb_ms is not None:
                break
        if rgb_ms is not None:
            break
    rgb_str = f"{rgb_ms:.3f} ms" if rgb_ms else f"{rgb_mult} × 帧间隔基准"

    # 报告必须展示实际参与丢帧判定的阈值。多设备分析按设备分别检查，不能用
    # 合并后的 r.imu 重新估算；多路时间轴交错会人为缩短中位间隔。
    imu_mult = p.get("imu_drop_mult", 1.5)
    device_thresholds: Dict[str, set] = {}
    for result in results:
        if result.imu_error:
            continue
        for device, drop in result.imu_drop_by_device.items():
            if drop.threshold_us > 0:
                device_thresholds.setdefault(device, set()).add(drop.threshold_us)
    if device_thresholds:
        all_thresholds = {value for values in device_thresholds.values() for value in values}
        if len(all_thresholds) == 1:
            value = next(iter(all_thresholds))
            suffix = "（各设备）" if len(device_thresholds) > 1 else ""
            imu_str = f"{value / 1000.0:.3f} ms{suffix}"
        else:
            device_items = []
            for device in sorted(device_thresholds, key=str.casefold):
                values = "、".join(f"{value / 1000.0:.3f} ms"
                                  for value in sorted(device_thresholds[device]))
                device_items.append(f"{device} {values}")
            imu_str = "；".join(device_items)
    else:
        actual_thresholds = sorted({result.imu_drop.threshold_us
                                    for result in results
                                    if not result.imu_error and result.imu_drop
                                    and result.imu_drop.threshold_us > 0})
        if actual_thresholds:
            imu_str = "、".join(f"{value / 1000.0:.3f} ms"
                               for value in actual_thresholds)
            if len(actual_thresholds) > 1:
                imu_str += "（按Part）"
        else:
            imu_ms = None
            for result in results:
                if result.imu and not result.imu_error:
                    imu_ms = expected_interval_us(result.imu) * imu_mult / 1000.0
                    break
            imu_str = f"{imu_ms:.3f} ms" if imu_ms else f"{imu_mult} × IMU周期"

    grip_sync_thresholds = sorted({sync.threshold_us
                                   for result in results
                                   for sync in result.grip_sync.values()
                                   if sync.threshold_us > 0})
    grip_sync_str = ("、".join(f"< {value / 1000.0:.9f} ms"
                              for value in grip_sync_thresholds)
                     if grip_sync_thresholds else "未执行")

    param_cards = ""
    audio_enabled = any(result.audio_enabled for result in results)
    param_items = [
        ("帧数对齐首/尾行阈值", f"< {p.get('frame_count_ms', 1)} ms"),
        ("帧间隔波动阈值", f"{p.get('interval_ms', 10)} ms"),
        ("同行同步对齐阈值", f"{p.get('row_align_ms', 10)} ms" +
         ("（不含深度）" if has_depth else "")),
        ("相机时间戳同步阈值", f"{p.get('cam_sync_ms', 10)} ms"),
        ("IMU理论频率/周期", _imu_frequency_text(results)),
        ("IMU时间戳同步阈值", _imu_sync_threshold_text(results)),
        ("夹爪与所属相机同步阈值", grip_sync_str),
        ("夹爪JSON频率/周期", _grip_frequency_text(results)),
        ("相机丢帧阈值", rgb_str),
        ("IMU丢帧阈值", imu_str),
    ]
    if not audio_enabled:
        pass  # 用户关闭音频测试：报告不列出音频参数与结论。
    elif p.get("audio_target_s") is not None:
        ppm = p.get("audio_drift_ppm")
        param_items.append(("音频目标时长", f"{p['audio_target_s']} s"))
        param_items.append(("音频时钟漂移",
                            f"{ppm} ppm" if ppm is not None else "UNVERIFIED（未提供实测值）"))
    if audio_enabled:
        param_items.append(("音频SHA256留证", "启用" if p.get("audio_sha256") else "关闭"))
    # 深度产品: 追加 depth 相关卡控标准 (仅当数据含 depth 相机)
    if has_depth:
        depth_settings = {r.depth_sync_enabled for r in results
                          if any(role in r.cameras for role in ('depth', 'depth_rgb'))}
        if len(depth_settings) > 1:
            depth_sync_text = '按分段设置'
            depth_scope_text = '按各分段开关设置；关闭时 Depth 不参与相机间同步，Depth RGB 仅比较左/右/上目'
        else:
            depth_enabled = next(iter(depth_settings), p.get('depth_sync', True))
            depth_sync_text = '启用' if depth_enabled else '未测试（用户关闭）'
            depth_scope_text = ('depth、depth_rgb 分别指向其余4目并独立出结论' if depth_enabled
                                else 'Depth 未测试；Depth RGB → 左/右/上目（不含 Depth）')
        param_items += [
            ("Depth 同步测试", depth_sync_text),
            ("深度同步共用阈值", _depth_sync_threshold_text(results)),
            ("RGB组(左目↔右目↔上目) 同步阈值", "1 ms"),
            ("深度同步基准", depth_scope_text),
            ("深度参与检查项", "仅帧间隔/同步/丢帧，不参与同行对齐"),
        ]
    for k, v in param_items:
        param_cards += f"<div><div class='k'>{_esc(k)}</div><div class='v'>{_esc(v)}</div></div>"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    from ego_checker.reporting.boundaries import iter_boundary_section
    from ego_checker.reporting.evidence import render_evidence_section
    fields = dict(
        session_section=_iter_session_section(results),
        meta_section=_iter_meta_section(results),
        analysis_notices=_iter_analysis_notices(results),
        evidence_section=render_evidence_section(evidence, out_path),
        boundary_section=iter_boundary_section(boundary_rows, run_notice),
        version=_esc(APP_VERSION),
        gen_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        root=_esc(str(root)), cam_list=_esc(cam_list),
        run_notice=(f'<div class="notice">⚠ {_esc(run_notice)}</div>'
                    if run_notice else ""),
        param_cards=param_cards,
        st_total=stats["total"], st_normal=stats["normal"],
        st_abnormal=stats["abnormal"], st_wl=stats["whitelisted"],
        st_errors=stats["errors"], st_unverified=stats["unverified"],
        audio_note=('' if not cols.audio_enabled else
                    '<p>音频：载荷差 &lt;144000 采样帧、单处差 &lt;24000 帧、'
                    '首包起点为 0 判 PASS；旧 CSV 倒退/重复/覆盖异常判 FAIL，'
                    '聚簇疑点判 SUSPECTED；实际时长仅记录，不设最短时长卡控。</p>'),
        audio_stats=('' if not cols.audio_enabled else
                     '<div class="stat"><div class="n">%d</div><div class="l">音频证据未验证'
                     '（含旧CSV包完整性；诊断，不与业务结果相加）</div></div>'
                     '<div class="stat"><div class="n" style="color:var(--amber)">%d</div>'
                     '<div class="l">音频节拍疑似异常（诊断，不与业务结果相加）</div></div>'
                     % (stats["audio_unverified"], stats["audio_suspected"])),
        problem_section=_render_problem_section(results),
        collect_s=stats["collect_s"], pass_s=stats["pass_s"], fail_s=stats["fail_s"],
        headers=headers, colgroup=colgroup, rows=_iter_report_rows(results, cols),
    )
    # Publish only after every section and UTF-8 write has completed. Each run
    # owns a unique temporary file in the destination directory (same filesystem).
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{out_path.name}.", suffix=".tmp", dir=out_path.parent)
    try:
        try:
            stream = os.fdopen(descriptor, "w", encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            raise
        with stream:
            for fragment in _iter_html_template(fields):
                for start in range(0, len(fragment), 65536):
                    if checkpoint:
                        checkpoint()
                    stream.write(fragment[start:start + 65536])
            stream.flush()
            os.fsync(stream.fileno())
        if checkpoint:
            checkpoint()
        os.replace(temporary, out_path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return out_path


from ego_checker.reporting.columns import REPORT_NAME, unique_path
