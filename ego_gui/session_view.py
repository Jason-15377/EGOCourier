"""会话列表的展示推导：把数据库行翻译成「一次导出批次」能看懂的字段。

会话是按来源路径去重的，而每次「开始一键导出」都会新建带时间戳的目录，
所以一条会话基本等于**一次导出批次**。列表要回答的是：
这次导出从哪来、包含哪些设备、有没有可对齐的数据。

数据库里的原始字段不适合直接展示：
- `device_serial` 在解析不出真实 SN 时会退化成会话名（两者完全一样），列出来等于重复；
- `name` 是文件名/目录名（`merged`、`ego-logs-app-20261020_092621_490`），不是人话；
- `frames=0` 和 `frames=正常` 在界面上都只是个数字，看不出「这批日志没参与对齐」。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Dict, List, Optional

# 目录/文件名里带的时间戳：EGO_export_20261020_103936 / ego-logs-app-20261020_092621_490
_TS_RE = re.compile(r"(20\d{6})[_-](\d{6})")
_EGO_EXPORT_RE = re.compile(r"^EGO_export_(\d{8})_(\d{6})$")


def _meta(session: Dict) -> Dict:
    m = session.get("meta")
    if isinstance(m, str):
        try:
            return json.loads(m) or {}
        except (ValueError, TypeError):
            return {}
    return m or {}


def source_kind(session: Dict) -> str:
    """判断这批日志从哪来：手机日志 / 电脑端导出 / 分析目录。"""
    path = (session.get("source_path") or "").replace("\\", "/")
    files = _meta(session).get("files") or []
    rels = [str(f.get("file") or "").replace("\\", "/") for f in files]
    if "/mobile/" in path + "/" or any(r.startswith("mobile/") for r in rels):
        return "手机日志"
    if "app-logs-zips" in path or "ego-logs-app-" in path:
        return "手机日志"
    if _EGO_EXPORT_RE.match(os.path.basename(path.rstrip("/")) or ""):
        return "电脑端导出"
    if "firmware/" in path or any(r.startswith("firmware/") for r in rels):
        return "电脑端导出"
    return "分析目录"


def batch_time(session: Dict) -> str:
    """导出批次时间：优先取目录名里的时间戳，其次 created_at，最后 start_us。"""
    base = os.path.basename((session.get("source_path") or "").rstrip("/\\"))
    m = _TS_RE.search(base) or _TS_RE.search(session.get("name") or "")
    if m:
        d, t = m.group(1), m.group(2)
        try:
            return datetime.strptime(d + t, "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    for key in ("created_at", "start_us"):
        v = session.get(key)
        if v:
            try:
                # created_at 存的是 us；start_us 也是 us
                return datetime.fromtimestamp(float(v) / 1_000_000).strftime(
                    "%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError, OSError, OverflowError):
                continue
    return "—"


def batch_label(session: Dict) -> str:
    """会话名：把文件名/目录名转成人能读的批次名。"""
    base = os.path.basename((session.get("source_path") or "").rstrip("/\\"))
    m = _EGO_EXPORT_RE.match(base)
    if m:
        d, t = m.group(1), m.group(2)
        return f"导出批次 {d[:4]}-{d[4:6]}-{d[6:]} {t[:2]}:{t[2:4]}:{t[4:]}"
    if "ego-logs-app-" in base:
        m2 = _TS_RE.search(base)
        if m2:
            d, t = m2.group(1), m2.group(2)
            return (f"手机日志包 {d[:4]}-{d[4:6]}-{d[6:]} "
                    f"{t[:2]}:{t[2:4]}:{t[4:]}")
    name = (session.get("name") or "").strip()
    if not name or name.lower() in {"merged", "session"}:
        # 手工导入的临时目录，用时间点区分，光看 "merged" 没有信息量
        when = batch_time(session)
        return f"本地导入 {when}" if when != "—" else "本地导入"
    return name or base or f"会话 #{session.get('id', '?')}"


def device_label(session: Dict) -> str:
    """设备列：只显示真实 SN；解析不出时明确说「未识别」，不要拿会话名冒充。

    历史数据里，解析不出 SN 时 device 表退而用会话名建记录（serial==name），
    于是出现 serial='merged'、serial='export' 这种把目录名当设备的情况。
    这里按「像不像设备序列号」再兜一层，避免把路径片段显示成设备。
    """
    serial = (session.get("device_serial") or "").strip()
    name = (session.get("name") or "").strip()
    if not serial or serial == name:
        return "未识别"
    low = serial.lower()
    if low in {"export", "merged", "session", "ego"} or "/" in serial or "\\" in serial:
        return "未识别"
    return serial


def file_count(session: Dict) -> int:
    return len(_meta(session).get("files") or [])


def data_state(session: Dict) -> str:
    """数据状态：有帧才算可分析，否则标「仅日志」。"""
    frames = session.get("frames") or 0
    return "仅日志" if frames == 0 else "可对齐"


def row_view(session: Dict) -> Dict[str, str]:
    """一行会话的全部展示字段。"""
    frames = int(session.get("frames") or 0)
    anomalies = int(session.get("anomalies") or 0)
    if frames == 0:
        frames_text = "无可对齐数据"
    else:
        frames_text = str(frames)
    return {
        "id": str(session.get("id", "")),
        "label": batch_label(session),
        "kind": source_kind(session),
        "device": device_label(session),
        "files": str(file_count(session)),
        "frames": frames_text,
        "anomalies": str(anomalies) if frames else "—",
        "state": data_state(session),
        "trace": (session.get("trace_id") or "—")[:16],
        "trace_full": session.get("trace_id") or "",
        "time": batch_time(session),
    }
