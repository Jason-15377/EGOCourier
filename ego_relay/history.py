"""会话记录（分析历史）登记簿：每次「日志分析」完成后写一条 JSON。

历史库里只存**指向**报告文件、数据目录、本身数据的元信息，不复制报告内容；
报告文件被手动删除/移动后，记录保留但标记为「报告缺失」（数据目录亦然），
这样用户仍能看到当时测了哪些目录、结论如何。

存储位置：``DATA_ROOT/history/sessions.json``（原子写入，损坏时自动归档）。
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config

HISTORY_DIR = config.DATA_ROOT / "history"
SESSIONS_JSON = HISTORY_DIR / "sessions.json"

_MUTABLE_FIELDS = {
    "status", "note", "kind", "root", "out_root", "html_path",
    "updated_at", "summary", "parts", "device_labels", "duration_s",
}


class HistoryError(Exception):
    """登记簿读写失败（文件损坏且无法归档、无权限等）。"""


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=1)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def load_sessions() -> List[Dict[str, Any]]:
    """读取全部记录（新→旧）。文件不存在返回空表。"""
    if not SESSIONS_JSON.exists():
        return []
    try:
        raw = json.loads(SESSIONS_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # 损坏不丢数据：改名归档一份，再以空表继续，避免整个 Tab 不可用。
        bad = SESSIONS_JSON.with_name(
            f"sessions.bad-{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        try:
            os.replace(SESSIONS_JSON, bad)
        except OSError:
            raise HistoryError(f"历史登记簿损坏且无法归档：{exc}") from exc
        return []
    if isinstance(raw, dict):
        raw = raw.get("sessions", [])
    rows = [r for r in raw if isinstance(r, dict) and r.get("id")]
    # 同一秒内写入的记录 created_at 相同；用稳定排序保留文件里的“新→旧”
    # 插入顺序，不能拿随机 UUID 当第二排序键（否则先后顺序会随机翻转）。
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return rows


def _save_all(rows: List[Dict[str, Any]]) -> None:
    _atomic_write(SESSIONS_JSON, {"version": 1, "sessions": rows})


def record_analysis(
    *,
    kind: str,
    root: str,
    out_root: str,
    html_path: str,
    status: str,
    summary: Optional[Dict[str, Any]] = None,
    parts: Optional[List[Dict[str, Any]]] = None,
    device_labels: Optional[List[str]] = None,
    duration_s: Optional[float] = None,
    created_at: Optional[str] = None,
    note: str = "",
) -> Dict[str, Any]:
    """写入一条分析记录并返回该记录。

    同一输出目录 + 同一状态 + 数据目录在 3 秒内重复登记视为同一次分析的
    重复回调（例如广播同时收听到完工与结束），直接返回已有记录不新增。
    """
    rows = load_sessions()
    now = created_at or _now_iso()
    norm_root = _norm_path(root)
    for row in rows[:5]:
        if (row.get("status") == status and _norm_path(row.get("root")) == norm_root
                and _norm_path(row.get("out_root")) == _norm_path(out_root)):
            if _within_seconds(row.get("created_at"), now, 3.0):
                return row
    entry = {
        "id": uuid.uuid4().hex,
        "created_at": now,
        "updated_at": now,
        "status": status,          # finished / partial / failed
        "kind": kind,              # single / multi / flat（报告粒度）
        "root": norm_root,
        "out_root": _norm_path(out_root),
        "html_path": _norm_path(html_path),
        "summary": summary or {},
        "parts": parts or [],
        "device_labels": device_labels or [],
        "duration_s": duration_s,
        "note": note or "",
    }
    rows.insert(0, entry)
    _save_all(rows)
    return entry


def update_session(session_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
    """修改备注/标签等可变字段。"""
    unknown = set(fields) - _MUTABLE_FIELDS
    if unknown:
        raise ValueError(f"不支持修改的字段：{sorted(unknown)}")
    rows = load_sessions()
    for row in rows:
        if row.get("id") == session_id:
            row.update(fields)
            row["updated_at"] = _now_iso()
            _save_all(rows)
            return row
    return None


def delete_session(session_id: str, *, delete_files: bool = False) -> bool:
    """删除记录；``delete_files`` 为真时一并删除其引用的报告文件。

    **永不删除数据目录**：那是用户原始采集数据，工具的职责只是分析它们。
    """
    rows = load_sessions()
    kept, removed = [], None
    for row in rows:
        if row.get("id") == session_id:
            removed = row
            continue
        kept.append(row)
    if removed is None:
        return False
    if delete_files:
        for key in ("html_path",):
            path = (removed.get(key) or "").strip()
            if path:
                try:
                    Path(path).unlink()
                except OSError:
                    pass
    _save_all(kept)
    return True


def prune_missing(rows: Optional[List[Dict[str, Any]]] = None) -> int:
    """标记报告/数据目录是否仍存在，返回「报告缺失」条数（不删记录）。"""
    rows = rows if rows is not None else load_sessions()
    missing = 0
    for row in rows:
        html = (row.get("html_path") or "").strip()
        row["_html_missing"] = not (html and Path(html).is_file())
        root = (row.get("root") or "").strip()
        row["_root_missing"] = not (root and Path(root).is_dir())
        if row["_html_missing"]:
            missing += 1
    return missing


def find_sessions(root: Optional[str] = None, out_root: Optional[str] = None,
                  status: Optional[str] = None) -> List[Dict[str, Any]]:
    """按数据目录/输出目录/状态过滤，供其他 Tab 反查。"""
    want_root = _norm_path(root) if root else None
    want_out = _norm_path(out_root) if out_root else None
    out = []
    for row in load_sessions():
        if want_root is not None and _norm_path(row.get("root")) != want_root:
            continue
        if want_out is not None and _norm_path(row.get("out_root")) != want_out:
            continue
        if status is not None and row.get("status") != status:
            continue
        out.append(row)
    return out


# --------------------------------------------------------------------- helpers
def _norm_path(value: Optional[str]) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return str(Path(text).expanduser().resolve())
    except OSError:
        return text


def _within_seconds(left: Optional[str], right: str, limit: float) -> bool:
    try:
        a = datetime.fromisoformat(str(left))
        b = datetime.fromisoformat(str(right))
    except (TypeError, ValueError):
        return False
    return abs((b - a).total_seconds()) <= limit


def summarize_results(results) -> Dict[str, Any]:
    """从分析结果（PartResult 列表）抽取记录用的统计，不依赖 GUI。

    直接复用 ``ego_checker.reporting.outputs.compute_stats``，保证与报告
    首页统计口径一致。
    """
    from ego_checker.reporting import outputs as rep

    stats = rep.compute_stats(results)
    return {
        "total": stats["total"],
        "normal": stats["normal"],
        "abnormal": stats["abnormal"],
        "errors": stats["errors"],
        "unverified": stats["unverified"],
        "whitelisted": stats["whitelisted"],
        "pass_s": round(float(stats["pass_s"]), 1),
        "fail_s": round(float(stats["fail_s"]), 1),
        "collect_s": round(float(stats["collect_s"]), 1),
    }


def summarize_parts(results) -> List[Dict[str, Any]]:
    """每个 Part 一行的精简信息，用于详情面板的 Part 表。"""
    rows: List[Dict[str, Any]] = []
    for r in results:
        roles = "/".join(
            (cam.label or role) for role, cam in r.cameras.items()) or "-"
        devices = list(dict.fromkeys(
            str(v) for v in (getattr(r, "camera_device_by_role", {}) or {}).values() if v))
        rows.append({
            "name": getattr(r, "name", "") or "",
            "data_folder": getattr(r, "data_folder", "") or "",
            "status": getattr(r, "business_status", "") or "",
            "cameras": roles,
            "devices": devices,
            "fps": round(float(getattr(r, "fps", 0) or 0), 1),
            "duration_s": round(float(getattr(r, "duration_s", 0) or 0), 1),
            "firmware": getattr(r, "firmware", "") or "",
        })
    return rows


def device_labels_of(results) -> List[str]:
    """结果中出现过的设备名（去重，保持出现顺序）。"""
    labels: List[str] = []
    for r in results:
        for name in (getattr(r, "camera_device_by_role", {}) or {}).values():
            text = str(name or "").strip()
            if text and text not in labels:
                labels.append(text)
    return labels
