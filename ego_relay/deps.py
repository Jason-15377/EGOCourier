"""运行依赖检测：ffmpeg/ffprobe、mcap、numpy 是否可用，以及安装指引。

日志/CSV 分析（EGO 管道）不依赖第三方，开箱即用；
视频分析（video_analyzer 处理 MP4/MCAP）需要 ffmpeg/ffprobe（必需）与 mcap（可选）。
"""

from __future__ import annotations

import glob
import os
import shutil


def _winget_path(binary: str) -> str:
    """在 winget 安装目录里直接找 ffmpeg/ffprobe（PATH 未刷新时也能命中）。"""
    base = os.environ.get("LOCALAPPDATA", "")
    pat = os.path.join(base, "Microsoft", "WinGet", "Packages",
                       "Gyan.FFmpeg*", "*", "bin", binary)
    hits = sorted(glob.glob(pat))
    return hits[0] if hits else ""


def ffmpeg_path() -> str:
    return shutil.which("ffmpeg") or _winget_path("ffmpeg.exe") or ""


def ffprobe_path() -> str:
    return shutil.which("ffprobe") or _winget_path("ffprobe.exe") or ""


def has_ffmpeg() -> bool:
    """ffmpeg 与 ffprobe 都存在才算可用（视频分析必需）。"""
    return bool(ffmpeg_path()) and bool(ffprobe_path())


def ensure_ffmpeg_on_path() -> None:
    """若 ffmpeg/ffprobe 不在当前进程 PATH 中，把 winget 安装的 bin 目录加进 PATH。

    目的：无论应用如何启动（终端旧 PATH / 新装 ffmpeg 后），video_analyzer 调用
    ffmpeg/ffprobe 子进程时都能找到，避免“装了却仍提示未安装”。
    """
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    base = os.environ.get("LOCALAPPDATA", "")
    bins = sorted(glob.glob(os.path.join(
        base, "Microsoft", "WinGet", "Packages", "Gyan.FFmpeg*", "*", "bin")))
    if bins:
        p = bins[0]
        cur = os.environ.get("PATH", "")
        if p not in cur:
            os.environ["PATH"] = p + os.pathsep + cur


def has_mcap() -> bool:
    try:
        import mcap  # noqa: F401
        return True
    except Exception:
        return False


def has_numpy() -> bool:
    try:
        import numpy  # noqa: F401
        return True
    except Exception:
        return False


def check() -> dict:
    return {
        "ffmpeg": has_ffmpeg(),
        "ffmpeg_path": ffmpeg_path(),
        "ffprobe_path": ffprobe_path(),
        "mcap": has_mcap(),
        "numpy": has_numpy(),
    }


def ffmpeg_install_help() -> str:
    return (
        "ffmpeg 未安装。视频 SEI 提取、丢帧检测、帧截取需要 ffmpeg 与 ffprobe；\n"
        "日志/CSV 分析不依赖它，仍可正常使用。\n\n"
        "Windows 安装方式（任选其一）：\n"
        "  1) winget install Gyan.FFmpeg\n"
        "  2) choco install ffmpeg\n"
        "  3) 手动下载：https://www.gyan.dev/ffmpeg/builds/\n"
        "     解压后把 bin 目录（含 ffmpeg.exe 与 ffprobe.exe）加入系统 PATH，并重启应用。\n\n"
        "macOS:  brew install ffmpeg\n"
        "Ubuntu:  sudo apt install ffmpeg\n\n"
        "ffprobe 会随 ffmpeg 一并安装。\n"
        "可选：MCAP 视频支持需要 `pip install mcap mcap-ros2-support`（没有也能用）。"
    )
