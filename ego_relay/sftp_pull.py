"""一键拉取设备日志（SFTP）。

流程：BLE 配网拿到设备 IP → SSH/SFTP 拉取 app 日志与固件日志到本地。
产物交给「日志分析」页做整目录分析，本模块只负责取回文件。

拉取产物：
- zip_first=True  → 单个 .tar.gz
- zip_first=False → 目录
"""

from __future__ import annotations

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
