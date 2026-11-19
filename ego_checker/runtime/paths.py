"""源码、安装包及冻结运行的路径职责。

本包由 EGO Courier 内嵌使用，仓库布局为 ``<项目根>/ego_checker``，
不再有独立的 ``src`` 父目录。分析相关的版本、资源与运行日志统一落到
EGO Courier 的数据目录，避免在仓库根散落日志与产物。
"""
from pathlib import Path
import sys

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
# 项目根：源码模式下即包含 ego_gui / ego_relay / ego_checker 的目录。
SOURCE_ROOT = PACKAGE_ROOT.parent


def repository_root():
    """返回源码检出根目录（冻结运行时不可用）。"""
    if getattr(sys, 'frozen', False):
        raise RuntimeError('This operation requires a source checkout')
    return SOURCE_ROOT


def resource_path(name):
    path = PACKAGE_ROOT / 'resources' / name
    if path.is_file():
        return path
    raise FileNotFoundError(path)


def application_directory():
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS)
    return SOURCE_ROOT


def data_directory():
    """分析运行日志等持久化目录：优先复用 EGO Courier 的 data/。"""
    try:
        from ego_relay import config as relay_config
        return Path(relay_config.DATA_ROOT)
    except Exception:  # noqa: BLE001 - 独立运行时退回到工具目录
        base = (Path(sys.executable).resolve().parent
                if getattr(sys, 'frozen', False) else SOURCE_ROOT)
        return base / 'data'
