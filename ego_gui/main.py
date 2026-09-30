"""EGO Courier 桌面版入口。

用法: python -m ego_gui  或  python run_gui.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap, QColor, QBrush, QPen
from PySide6.QtWidgets import QApplication

from .app import MainWindow


def make_icon(size: int = 64) -> QIcon:
    """绘制科技感摄像头/传感器图标：圆角机身 + 镜头 + 指示灯。"""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    # 机身（深蓝圆角矩形）
    p.setBrush(QBrush(QColor("#2563eb")))
    p.setPen(QPen(QColor("#1d4ed8"), 2))
    p.drawRoundedRect(int(size*0.08), int(size*0.18), int(size*0.84), int(size*0.64),
                      int(size*0.12), int(size*0.12))
    # 镜头外圈
    p.setBrush(QBrush(QColor("#0f172a")))
    p.setPen(QPen(QColor("#38bdf8"), 3))
    p.drawEllipse(int(size*0.28), int(size*0.32), int(size*0.44), int(size*0.44))
    # 镜头内圈（蓝）
    p.setBrush(QBrush(QColor("#38bdf8")))
    p.setPen(Qt.NoPen)
    p.drawEllipse(int(size*0.40), int(size*0.44), int(size*0.20), int(size*0.20))
    # 顶部指示灯（绿）
    p.setBrush(QBrush(QColor("#22c55e")))
    p.drawEllipse(int(size*0.16), int(size*0.12), int(size*0.10), int(size*0.10))
    p.end()
    return QIcon(pm)


def main() -> int:
    # 启动时确保 ffmpeg/ffprobe 在当前进程 PATH 中（新装 ffmpeg / 旧终端 PATH 也能识别）
    from ego_relay import deps
    deps.ensure_ffmpeg_on_path()

    app = QApplication(sys.argv)
    app.setApplicationName("EGO Courier")
    app.setOrganizationName("Orbbec")
    icon = make_icon()
    app.setWindowIcon(icon)          # 任务栏图标
    w = MainWindow()
    w.setWindowIcon(icon)            # 窗口左上角图标
    w.resize(1260, 860)
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
