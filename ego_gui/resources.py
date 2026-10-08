"""EGO Courier 桌面版资源定位。

`assets/` 里的标志/图标在「直接跑源码」和「PyInstaller 打包后的 exe」两种模式下
路径不同：打包后资源被解压到 `sys._MEIPASS` 临时目录，而不是脚本所在目录。
本模块把两种来源统一成一组 `asset_path()` 调用，供 GUI 与打包脚本共用。
"""

from __future__ import annotations

import os
import sys

# 图标资源相对 assets/ 的文件名
ICON_ICO = "ego_courier.ico"   # 多分辨率，供 exe / 任务栏
ICON_PNG = "logo_256.png"      # 256px PNG，Qt 运行时回退用
LOGO_PNG = "logo.png"          # 512px PNG，文档 / 关于页用


def bundle_root() -> str:
    """返回 `assets/` 所在的项目（或打包解压）根目录。"""
    # PyInstaller onefile/onedir 都会设置 sys._MEIPASS
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return str(meipass)
    # 源码模式：ego_gui/ 的上一级
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def asset_path(name: str) -> str:
    """返回 assets/<name> 的绝对路径（不保证存在）。"""
    return os.path.join(bundle_root(), "assets", name)


def existing_icon() -> str:
    """按优先级返回一个真实存在的图标路径，找不到则返回空串。"""
    for name in (ICON_ICO, ICON_PNG, LOGO_PNG):
        p = asset_path(name)
        if os.path.isfile(p):
            return p
    return ""
