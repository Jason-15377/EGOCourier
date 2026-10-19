"""运行依赖探测（启动器 bootstrap.py 与 bat 共用）。

与 ``deps.py`` 的分工：``deps.py`` 管可选的**工具**（ffmpeg、mcap、numpy，
缺失只影响部分功能）；本模块管**不装就起不来**的 Python 包，供启动器在
黑框窗口里用 ASCII 文本逐项报告，并据此决定是否自动安装。

只用标准库，import 时不能有任何第三方依赖（否则在缺依赖的机器上自己就先炸了）。
"""

from __future__ import annotations

import importlib.metadata as _meta
import importlib.util
import os
import sys

# (显示名, import 名, pip 包名, 是否必需, 失败时的影响)
PACKAGES: list[tuple[str, str, str, bool, str]] = [
    ("PySide6", "PySide6", "PySide6", True,
     "图形界面无法启动"),
    ("matplotlib", "matplotlib", "matplotlib", True,
     "分析页签的曲线图无法显示"),
    ("numpy", "numpy", "numpy", True,
     "视频/SEI 分析无法运行"),
    ("paramiko", "paramiko", "paramiko", True,
     "设备日志导出模块导入失败（界面直接打不开）"),
    ("mcap", "mcap", "mcap", False,
     "仅 MCAP 视频分析不可用"),
    ("mcap-ros2-support", "mcap_ros2", "mcap-ros2-support", False,
     "仅 MCAP ROS2 视频分析不可用"),
]

# Python 版本策略：3.10–3.12 有完整轮子；3.13 起 PySide6/matplotlib 轮子不全。
MIN_VERSION = (3, 10)
RECOMMENDED_VERSION = (3, 12)
MAX_TESTED_VERSION = (3, 12)


def version_tuple(text: str | None = None) -> tuple[int, int, int]:
    v = text or sys.version
    out: list[int] = []
    for part in v.split(".")[:3]:
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits) if digits else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out)  # type: ignore[return-value]


def python_problem(version: tuple[int, int, int] | None = None) -> str:
    """当前解释器是否适合运行本程序；返回空串表示合适。"""
    v = version_tuple() if version is None else version
    where = f"当前 Python {v[0]}.{v[1]}.{v[2]}（{sys.executable}）"
    if v[:2] < MIN_VERSION:
        return (f"{where} 版本过低，本程序需要 Python {MIN_VERSION[0]}.{MIN_VERSION[1]} 及以上。")
    if v[:2] > MAX_TESTED_VERSION:
        return (
            f"{where} 版本过新：Python {v[0]}.{v[1]} 上 PySide6 / matplotlib 的预编译包"
            f"覆盖不全，安装依赖会失败。\n"
            f"    请安装 Python {RECOMMENDED_VERSION[0]}.{RECOMMENDED_VERSION[1]}"
            f"（https://www.python.org/downloads/release/python-31210/ ，"
            f"安装时勾选 “Add python.exe to PATH”），然后重新双击 EGO Courier.bat。"
        )
    return ""


def installed_version(import_name: str) -> str | None:
    """已安装的版本号（按发行版名查），未安装返回 None。"""
    dist_name = {
        "PySide6": "PySide6",
        "mcap_ros2": "mcap-ros2-support",
    }.get(import_name, import_name)
    try:
        return _meta.version(dist_name)
    except Exception:
        return None


def _importable(import_name: str) -> bool:
    """能找到模块但不实际导入它（导入 PySide6 很慢，探测阶段不付这个代价）。"""
    try:
        return importlib.util.find_spec(import_name) is not None
    except Exception:
        return False


def missing(include_optional: bool = True) -> list[tuple[str, str, bool, str]]:
    """返回缺失的包：[(显示名, pip 包名, 是否必需, 影响)]。"""
    out = []
    for display, import_name, pip_name, required, impact in PACKAGES:
        if not include_optional and not required:
            continue
        if not _importable(import_name):
            out.append((display, pip_name, required, impact))
    return out


def missing_required() -> list[tuple[str, str, bool, str]]:
    return [m for m in missing() if m[2]]


def summary_rows() -> list[tuple[str, str, bool]]:
    """供打印的检查表：[(显示名, 版本或“缺失”, 是否缺失)]。"""
    rows = []
    for display, import_name, _pip, _req, _impact in PACKAGES:
        ver = installed_version(import_name)
        rows.append((display, ver or "未安装", ver is None))
    return rows


def venv_python_dir() -> str:
    """当前环境是否为虚拟环境（有 pyvenv.cfg 即认为是），返回其根目录。"""
    for base in {sys.prefix, sys.base_prefix, sys.executable}:
        path = base if os.path.isdir(base) else os.path.dirname(base)
        if os.path.isfile(os.path.join(path, "pyvenv.cfg")):
            return path
    return ""
