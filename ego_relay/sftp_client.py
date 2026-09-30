"""SSH2 + SFTP 设备日志客户端。

设备日志对外暴露协议：**SSH2 + SFTP 文件传输**
- 非 ADB、非 SMB 网络共享、非 HTTP 接口。
- 接入凭证：设备 IP、ssh 用户名、ssh 密码。
- 两种拉取方式：
  1. 递归 SFTP 下载远端日志目录到本地。
  2. 先经 shell 在远端打包压缩（tar -czf），再 SFTP 下载单个压缩包。

依赖 paramiko（pip install paramiko）。
"""

from __future__ import annotations

import os
import shlex
from datetime import datetime
from typing import List, Optional, Callable

import paramiko

ProgressCb = Optional[Callable[[str, int, int], None]]  # (local_path, got, total)


class SftpClient:
    """SSH2 + SFTP 客户端。"""

    def __init__(self, host: str, username: str, password: str,
                 port: int = 22, timeout: int = 15):
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.timeout = timeout
        self.ssh: Optional[paramiko.SSHClient] = None
        self.sftp: Optional[paramiko.SFTPClient] = None

    def connect(self) -> None:
        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            self.ssh.connect(self.host, self.port, self.username, self.password,
                             timeout=self.timeout, banner_timeout=self.timeout,
                             auth_timeout=self.timeout)
        except Exception as e:  # noqa: BLE001
            self.ssh.close()
            self.ssh = None
            raise ConnectionError(f"SSH 连接 {self.username}@{self.host}:{self.port} 失败: {e}") from e
        self.sftp = self.ssh.open_sftp()

    def _check(self):
        if not self.ssh or not self.sftp:
            raise ConnectionError("SSH/SFTP 尚未连接，请先 connect()")

    # ------------------------------------------------------------- 远端操作
    def exec(self, command: str, timeout: int = 120) -> str:
        """执行远端 shell 命令，返回 stdout（命令失败抛 RuntimeError）。"""
        self._check()
        stdin, stdout, stderr = self.ssh.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        rc = stdout.channel.recv_exit_status()
        if rc != 0:
            raise RuntimeError(f"远端命令失败(rc={rc}): {command}\n{err or out}")
        return out

    def pack_remote_logs(self, remote_dirs: List[str], archive_path: str) -> str:
        """把多个远端日志目录打包压缩到 archive_path（tar czf）。"""
        dirs = " ".join(shlex.quote(d) for d in remote_dirs)
        archive = shlex.quote(archive_path)
        # 先确保目标目录存在，再打包
        self.exec(f"mkdir -p $(dirname {archive})")
        self.exec(f"tar -czf {archive} {dirs}")
        return archive_path

    def download(self, remote_path: str, local_path: str,
                 progress_cb: ProgressCb = None) -> str:
        """SFTP 下载单个文件到本地，可选进度回调 (local_path, got, total)。"""
        self._check()
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        try:
            size = self.sftp.stat(remote_path).st_size
        except Exception:  # noqa: BLE001
            size = None

        def _cb(got, total):
            if progress_cb:
                progress_cb(local_path, got, total or size or 0)

        self.sftp.get(remote_path, local_path, callback=_cb)
        return local_path

    def download_dir(self, remote_dir: str, local_dir: str,
                     progress_cb: ProgressCb = None) -> List[str]:
        """递归下载远端目录到本地，返回下载的本地文件列表。"""
        self._check()
        remote_dir = remote_dir.rstrip("/")
        os.makedirs(local_dir, exist_ok=True)
        downloaded = []
        try:
            entries = self.sftp.listdir_attr(remote_dir)
        except FileNotFoundError:
            return downloaded
        for attr in entries:
            rpath = f"{remote_dir}/{attr.filename}"
            lpath = os.path.join(local_dir, attr.filename)
            if attr.st_mode is not None and (attr.st_mode & 0o170000) == 0o040000:
                downloaded += self.download_dir(rpath, lpath, progress_cb)
            else:
                self.download(rpath, lpath, progress_cb)
                downloaded.append(lpath)
        return downloaded

    def clear_remote_logs(self, remote_dirs: List[str]) -> None:
        """清空远端日志目录（删除目录内文件，保留目录本身）。"""
        for d in remote_dirs:
            self.exec(f"find '{d}' -type f -delete 2>/dev/null || true")

    def close(self) -> None:
        try:
            if self.sftp:
                self.sftp.close()
        finally:
            self.sftp = None
        try:
            if self.ssh:
                self.ssh.close()
        finally:
            self.ssh = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def pull_logs(host: str, username: str, password: str,
              remote_dirs: List[str], local_dir: str,
              zip_first: bool = True, port: int = 22,
              progress_cb: ProgressCb = None) -> List[str]:
    """一键拉取设备日志到本地。

    - zip_first=True:  先在远端打包，再下载单个 tar.gz（省流量、文件少）。
    - zip_first=False: 直接递归 SFTP 下载每个远端目录。
    返回本地下载到的文件列表。
    """
    local_dir = os.path.abspath(local_dir)
    os.makedirs(local_dir, exist_ok=True)
    files = []
    with SftpClient(host, username, password, port=port) as cli:
        if zip_first:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            archive = f"/tmp/ego_logs_{ts}.tar.gz"
            try:
                cli.pack_remote_logs(remote_dirs, archive)
                local_arc = os.path.join(local_dir, f"ego_logs_{ts}.tar.gz")
                cli.download(archive, local_arc, progress_cb)
                files.append(local_arc)
            finally:
                try:
                    cli.exec(f"rm -f {shlex.quote(archive)}")
                except Exception:
                    pass
        else:
            for d in remote_dirs:
                files += cli.download_dir(d, local_dir, progress_cb)
    return files
