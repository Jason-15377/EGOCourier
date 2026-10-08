# -*- mode: python ; coding: utf-8 -*-
"""EGO Courier 桌面版打包配置（PyInstaller）。

用法：
    python -m PyInstaller --noconfirm --clean ego_courier.spec

产出（dist/ 下）：
    EGO Courier/EGO Courier.exe    —— onedir（默认，启动快、推荐分发）
    EGO Courier.exe                —— onefile（若 BUILD_ONEFILE=1）

说明：
* 入口用 ``packaging/entry.py``（而非 run_desktop.py），它会在冻结环境下把
  ``sys._MEIPASS`` 加进 sys.path，保证 ``ego_gui`` / ``ego_relay`` 可导入。
* ``assets/`` 一并打进包内（供运行时 QIcon 读取 .ico / PNG）。
* ffmpeg/ffprobe 不打进包：由 ``ego_relay.deps`` 在启动时从 PATH / winget 目录探测，
  视频分析才需要，缺失时 GUI 会给出安装提示而不是崩溃。
* ``lib/EgoLowBle.dll`` 是 BLE 配网的本地依赖，必须随包携带。
* OrbbecSDK.dll 不打包：程序运行时从用户机器上已安装的 EGOViewer 目录里查找
  （见 ``ego_relay/orbbec_sdk.py``），随包分发会造成版本错配。
"""

import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# ---------------------------------------------------------------- 开关

# 设 EGO_ONEFILE=1 出单文件 exe；默认 onedir（启动更快、杀软误报更少）
ONEFILE = os.environ.get("EGO_ONEFILE", "0") == "1"

APP_NAME = "EGO Courier"

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
    [os.path.join("packaging", "entry.py")],
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

if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name=APP_NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,           # UPX 压缩会显著提高杀软误报率，保持关闭
        runtime_tmpdir=None,
        console=False,       # GUI 程序，不要控制台窗口
        disable_windowed_traceback=False,
        icon=_icon_arg,
        version=None,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=APP_NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        icon=_icon_arg,
        version=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name=APP_NAME,
    )
