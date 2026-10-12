# -*- mode: python ; coding: utf-8 -*-
"""EGO Courier 便携版打包配置（launcher + runtime）。

与 ``ego_courier.spec`` 的区别：把 PyInstaller 的内容目录从 ``_internal/`` 改名成
``runtime/``，并让第三方库（PySide6 / matplotlib / numpy / paramiko …）全部摊在
``runtime/`` 里，exe 只保留引导器与启动脚本。产出结构对齐团队里既有的
``Ego-Vue Simple Evaluate``：

    EGO Courier/
    ├── EGO Courier.exe        ← 引导器（双击启动）
    ├── runtime/               ← Python 解释器 + 全部第三方依赖
    ├── assets/                ← 标志 / 图标
    ├── app/                   ← ego_relay / ego_gui 源码（可热改）
    └── data/                  ← 运行数据（DB / 导出 / 缓存）

用法：
    python -m PyInstaller --noconfirm --clean ego_courier_portable.spec
或
    build_portable.bat
"""

import os
import shutil

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# ---------------------------------------------------------------- 开关

APP_NAME = "EGO Courier"

# 目标机器上不一定有开发工具，把源码以明文放进 runtime/app，改代码不必重打包。
BUNDLE_SOURCE = os.environ.get("EGO_BUNDLE_SOURCE", "1") == "1"

HERE = os.path.abspath(os.getcwd())

# ---------------------------------------------------------------- 资源

datas = []

# 图标 / 标志（运行时 resources.py 通过 sys._MEIPASS/assets 读取）
datas += [(os.path.join(HERE, "assets"), "assets")]

# matplotlib 的 mpl-data（字体、样式表）；不收集会导致图形缺字/报错
try:
    datas += collect_data_files("matplotlib")
except Exception:
    pass

# 应用源码明文入包：供启动器优先从 runtime/app 导入（便于现场热改）
if BUNDLE_SOURCE:
    _ignores = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")
    _stage = os.path.join(HERE, "build", "_portable_app")
    shutil.rmtree(_stage, ignore_errors=True)
    for pkg in ("ego_relay", "ego_gui"):
        src = os.path.join(HERE, pkg)
        if os.path.isdir(src):
            # 先拷到暂存目录并剔除 __pycache__：带旧 .pyc 进包会让现场改 .py 后
            # 出现「改了没生效」的假象（mtime 相同或时区偏差时尤其明显）。
            dst = os.path.join(_stage, pkg)
            shutil.copytree(src, dst, ignore=_ignores)
            datas += [(dst, os.path.join("app", pkg))]

# ---------------------------------------------------------------- 二进制

binaries = []

# BLE 本地库
_ble = os.path.join(HERE, "lib", "EgoLowBle.dll")
if os.path.isfile(_ble):
    binaries.append((_ble, "lib"))

# ---------------------------------------------------------------- 导入

hiddenimports = []

# 这些包按子模块动态使用，PyInstaller 静态分析容易漏
hiddenimports += collect_submodules("ego_relay")
hiddenimports += collect_submodules("ego_gui")
hiddenimports += [
    "PySide6.QtSvg",
    "matplotlib.backends.backend_qtagg",
    "mcap",
    "mcap_ros2_support",
]

# ---------------------------------------------------------------- 裁剪
# 不用的 Qt 大件一律排除，能省几百 MB（本应用纯本地 GUI，无网页/多媒体/3D）
excludes = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQml",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtTest",
    "PySide6.QtSql",
    "PySide6.QtNetworkAuth",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtPositioning",
    "PySide6.QtSerialPort",
    "PySide6.QtSensors",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtStateMachine",
    "PySide6.QtSpatialAudio",
    "PySide6.QtTextToSpeech",
    "PySide6.QtWebSockets",
    "PySide6.QtWebChannel",
    # 科学计算中不用的重件
    "tkinter",
    "PyQt5",
    "PyQt6",
    "IPython",
    "jupyter",
    "pytest",
    "notebook",
]

# ---------------------------------------------------------------- 分析

a = Analysis(
    [os.path.join("packaging", "entry_portable.py")],
    pathex=[HERE],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

_icon = os.path.join(HERE, "assets", "ego_courier.ico")
_icon_arg = _icon if os.path.isfile(_icon) else None

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                    # UPX 压缩会显著提高杀软误报率，保持关闭
    console=False,                # GUI 程序，不要控制台窗口
    disable_windowed_traceback=True,   # traceback 走自定义弹窗，避免黑框一闪而过
    icon=_icon_arg,
    version=None,
    contents_directory="runtime",  # ← 关键：_internal/ 改名为 runtime/
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)
