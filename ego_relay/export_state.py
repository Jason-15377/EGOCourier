# -*- coding: utf-8 -*-
"""增量导出记录：按 EGOViewer 根目录记住 app/SDK 日志的上次成功导出起点。

只覆盖本机 app/SDK 日志：手机日志拉取的是“最新一个导出包”，设备固件日志是
设备端全量快照，二者都不适合按时间做增量，仍保持全量行为。

记录的是导出时间范围的**结束时刻**，而不是导出完成时刻：导出期间新写入的
日志不会被本次收集，下次从结束时刻重新扫，宁可重复，不丢日志。若用户把
开始时间调到上次增量起点之后（中间留了一段没导），本次不会推进记录，避免
把未导出的区间误标为已导出。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from ego_relay import config


STATE_FILE_NAME = "export_incremental.json"
LOCAL_KEYS = ("app", "sdk")


def state_path() -> Path:
    """增量记录文件：与其它运行数据一起放在 data/ 下，随项目可迁移。"""
    return Path(config.DATA_ROOT) / STATE_FILE_NAME


def _root_key(root: str) -> str:
    """不同安装根目录互不干扰；Windows 下大小写不敏感。"""
    return os.path.normcase(os.path.abspath(str(root or "")))


def load_state(path: Optional[Path] = None) -> dict:
    target = Path(path) if path else state_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {"version": 1, "roots": {}}
    if not isinstance(raw, dict) or not isinstance(raw.get("roots"), dict):
        return {"version": 1, "roots": {}}
    raw["version"] = 1
    return raw


def save_state(state: dict, path: Optional[Path] = None) -> None:
    """原子写入，避免断电/杀进程留下半个 JSON。"""
    target = Path(path) if path else state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    payload = json.dumps(state, ensure_ascii=False, indent=1)
    with open(tmp, "w", encoding="utf-8") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, target)


def get_watermarks(root: str, keys: Optional[Iterable[str]] = None,
                   path: Optional[Path] = None) -> dict:
    keys = [k for k in (keys or LOCAL_KEYS) if k in LOCAL_KEYS]
    entry = load_state(path)["roots"].get(_root_key(root)) or {}
    result = {}
    for key in keys:
        try:
            stamp = float(entry.get(key))
        except (TypeError, ValueError):
            continue
        if stamp > 0:
            result[key] = stamp
    return result


def effective_watermark(root: str, keys: Optional[Iterable[str]] = None,
                        path: Optional[Path] = None) -> Optional[float]:
    """本次增量导出可用的统一起点。

    任一选中分类没有记录时返回 None（该分类只能全量），避免用另一分类的较新
    起点把它的旧日志跳过。全部都有记录时取最早的那个，保证不漏。
    """
    keys = [k for k in (keys or LOCAL_KEYS) if k in LOCAL_KEYS]
    if not keys:
        return None
    marks = get_watermarks(root, keys, path)
    if len(marks) != len(keys):
        return None
    return min(marks.values())


def set_watermarks(root: str, keys: Iterable[str], stamp: float, *,
                   export_dir: str = "", path: Optional[Path] = None) -> None:
    keys = [k for k in keys if k in LOCAL_KEYS]
    if not keys:
        return
    state = load_state(path)
    entry = state["roots"].setdefault(_root_key(root), {})
    for key in keys:
        entry[key] = float(stamp)
    entry["last_export"] = str(export_dir or "")
    entry["updated"] = datetime.now().isoformat(timespec="seconds")
    state["version"] = 1
    save_state(state, path)


def clear_root(root: str, path: Optional[Path] = None) -> bool:
    state = load_state(path)
    removed = state["roots"].pop(_root_key(root), None) is not None
    if removed:
        save_state(state, path)
    return removed


def format_stamp(stamp: Any) -> str:
    try:
        return datetime.fromtimestamp(float(stamp)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "—"
