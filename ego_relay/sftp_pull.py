"""一键拉取设备日志 + 自动导入分析。

流程：BLE 配网拿到设备 IP → SSH/SFTP 拉取 app 日志与固件日志到本地 →
自动解包并导入本工具会话（复用 parsers.import_path + analysis.align_and_analyze）。

拉取产物：
- zip_first=True  → 单个 .tar.gz（需先解包再导入）
- zip_first=False → 目录（可直接导入）
"""

from __future__ import annotations

import os
import shutil
import tarfile
import tempfile
from pathlib import Path

from . import analysis, db, parsers
from .sftp_client import pull_logs as _sftp_pull


def pull_logs(host: str, username: str, password: str,
              remote_dirs, local_dir: str,
              zip_first: bool = True, port: int = 22,
              progress_cb=None) -> list:
    """从设备 SFTP 拉取日志，返回本地产物文件列表。"""
    if isinstance(remote_dirs, str):
        remote_dirs = [d.strip() for d in remote_dirs.replace("&", "\n").splitlines() if d.strip()]
    return _sftp_pull(host, username, password, list(remote_dirs),
                      local_dir, zip_first=zip_first, port=port,
                      progress_cb=progress_cb)


def _extract_targz(path: str, dest: str) -> str:
    root = Path(dest).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "r:gz") as tf:
        for member in tf.getmembers():
            candidate = (root / member.name).resolve()
            if candidate != root and root not in candidate.parents:
                raise ValueError(f"压缩包包含不安全路径: {member.name}")
            if member.issym() or member.islnk():
                raise ValueError(f"压缩包包含不支持的链接: {member.name}")
        tf.extractall(root)
    return str(root)


def import_pulled(target: str, thresholds_us: dict = None,
                  extra_dirs=None) -> dict:
    """Import a pulled archive/directory, optionally merging extra directories."""
    target = os.path.abspath(target)
    src = Path(target)
    is_targz = src.is_file() and src.name.lower().endswith((".tar.gz", ".tgz"))
    temp_work = None
    if is_targz:
        temp_work = tempfile.mkdtemp(prefix="ego_pull_")
        work = _extract_targz(target, temp_work)
    else:
        work = target
    merge_root = None
    if extra_dirs:
        merge_root = tempfile.mkdtemp(prefix="ego_pull_merge_")
        merge_root = Path(merge_root)
        shutil.copytree(work, merge_root, dirs_exist_ok=True)
        for extra in extra_dirs:
            extra_path = Path(extra)
            if extra_path.is_dir():
                destination = merge_root / extra_path.name
                shutil.copytree(extra_path, destination, dirs_exist_ok=True)
        work = str(merge_root)
    try:
        ps = parsers.import_path(work)
        r = analysis.align_and_analyze(ps, thresholds_us or None)
        s = db.get_session(r["session_id"])
        return {"session": s, "stats": db.session_stats(r["session_id"])}
    finally:
        if merge_root:
            shutil.rmtree(merge_root, ignore_errors=True)
        if temp_work:
            shutil.rmtree(temp_work, ignore_errors=True)
