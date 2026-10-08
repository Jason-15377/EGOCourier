"""EGO Courier 桌面版入口。

用法: python -m ego_gui  或  python run_desktop.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap, QColor, QBrush, QPen, QPolygonF
from PySide6.QtWidgets import QApplication

from . import resources
from .app import MainWindow


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
