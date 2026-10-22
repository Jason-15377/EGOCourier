"""EGO Courier 桌面版入口。

用法: python -m ego_gui  或  python run_desktop.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import (QIcon, QPainter, QPixmap, QColor, QBrush, QPen,
                           QPolygonF, QPalette)
from PySide6.QtWidgets import QApplication

from . import resources
from .app import MainWindow


def _pin_light_appearance(app: QApplication) -> None:
    """把界面外观钉死在浅色，不跟随 Windows 的深色模式。

    界面用的是一套手写的浅色 QSS（白底、#222 文字）。系统切到深色模式后，
    **没有被 QSS 显式指定 background 的原生控件**（勾选框的方框、下拉列表
    弹层、滚动条槽、右键菜单等）会改用系统深色配色，于是白卡片上冒出一批
    深色/近黑的方框——现场表述就是"勾选完 UI 跟着系统颜色走了"。
    只给 `color` 写规则不够：Qt 认为"背景来自面板"时仍会用深色调色板绘制这些
    子控件。

    因此这里显式声明本应用只支持浅色。Qt 6.5+ 用 styleHints().setColorScheme，
    更老的版本退回调色板 + Fusion。Windows 的"强制深色"清单（注册表
    AppsUseLightTheme=0 且进程未被排除）也会被这一步覆盖。
    """
    hints = app.styleHints()
    set_scheme = getattr(hints, "setColorScheme", None)
    if set_scheme is not None:
        try:
            set_scheme(Qt.ColorScheme.Light)
        except Exception:  # noqa: BLE001 —— 老 Qt 上没有这个接口
            pass
    # 调色板也必须显式重置：setColorScheme 只是告诉系统"我要浅色"，已经带进
    # 应用里的深色 palette 不会被它改回来（深色模式机器上仍然是深底深槽）。
    # 用当前样式的标准浅色调色板覆盖，再把已知的浅色角色钉死，避免个别平台
    # 的 standardPalette() 还带着深色值。
    palette = app.style().standardPalette()
    for name, color in _LIGHT_ROLES:
        role = getattr(QPalette, name, None)
        if role is not None:
            palette.setColor(role, QColor(color))
    app.setPalette(palette)


# 界面 QSS 假设的浅色配色基准：与 app.STYLE 里的白底/深字一致。
_LIGHT_ROLES = (
    ("Window", "#f5f6f8"),
    ("WindowText", "#222222"),
    ("Base", "#ffffff"),
    ("AlternateBase", "#fafbfc"),
    ("Text", "#222222"),
    ("Button", "#ffffff"),
    ("ButtonText", "#222222"),
    ("BrightText", "#ffffff"),
    ("ToolTipBase", "#ffffff"),
    ("ToolTipText", "#222222"),
    ("Highlight", "#dbeafe"),
    ("HighlightedText", "#1e3a8a"),
    ("PlaceholderText", "#9ca3af"),
    ("Light", "#ffffff"),
    ("Midlight", "#f3f4f6"),
    ("Mid", "#d1d5db"),
    ("Dark", "#9ca3af"),
    ("Shadow", "#e5e7eb"),
)


def _fallback_icon(size: int = 64) -> QIcon:
    """矢量兜底图标：蓝底 + 白色快递箭头（与 assets/ 里的标志同构）。

    仅当 assets/ 资源缺失时使用（例如只拷了 .py 文件），保证窗口图标不空。
    """
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    # 蓝底圆角方
    p.setBrush(QBrush(QColor("#2563eb")))
    p.setPen(QPen(QColor("#1d4ed8"), max(1, size // 32)))
    p.drawRoundedRect(1, 1, size - 2, size - 2, int(size * 0.22), int(size * 0.22))
    # 主箭头（白，朝右上）
    pen = QPen(QColor("#ffffff"))
    pen.setWidthF(size * 0.093)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.drawPolyline(QPolygonF([QPointF(size * 0.255, size * 0.785),
                              QPointF(size * 0.455, size * 0.585),
                              QPointF(size * 0.655, size * 0.585)]))
    # 次级箭头（青，右上错位）
    pen.setColor(QColor("#38bdf8"))
    pen.setWidthF(size * 0.078)
    p.setPen(pen)
    p.drawPolyline(QPolygonF([QPointF(size * 0.505, size * 0.505),
                              QPointF(size * 0.635, size * 0.375),
                              QPointF(size * 0.765, size * 0.375)]))
    p.end()
    return QIcon(pm)


def make_icon() -> QIcon:
    """应用图标：优先读 assets/ 里的多分辨率标志，缺失时退回矢量绘制。"""
    path = resources.existing_icon()
    if path:
        icon = QIcon(path)
        if not icon.isNull():
            return icon
    return _fallback_icon()


def _set_app_user_model_id() -> None:
    """给 Windows 进程一个显式 AppUserModelID。

    否则从命令行/批处理启动时，任务栏会把进程归到 python.exe 名下，
    显示 Python 的图标而不是 EGO Courier 的图标。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "Orbbec.EGOCourier.Desktop.1"
        )
    except Exception:
        pass


def _ensure_database() -> None:
    """建库建表（幂等）。

    会话列表等页签在构造时就会查 sessions 表，因此必须在 MainWindow 之前调用；
    否则全新机器 / 打包后首次运行时数据库文件尚不存在，界面会直接抛
    ``no such table: sessions``。
    """
    from ego_relay import config, db

    try:
        config.DATA_ROOT.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    db.init_db()


def main() -> int:
    # 启动时确保 ffmpeg/ffprobe 在当前进程 PATH 中（新装 ffmpeg / 旧终端 PATH 也能识别）
    from ego_relay import deps
    deps.ensure_ffmpeg_on_path()

    _ensure_database()
    _set_app_user_model_id()

    app = QApplication(sys.argv)
    _pin_light_appearance(app)
    app.setApplicationName("EGO Courier")
    app.setApplicationDisplayName("EGO Courier")
    app.setOrganizationName("Orbbec")
    app.setWindowIcon(make_icon())   # 任务栏 / 窗口默认图标
    w = MainWindow()
    w.resize(1260, 860)
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
