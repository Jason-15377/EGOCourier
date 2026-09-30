# -*- coding: utf-8 -*-
"""渲染 MainWindow 到 PNG，用于目视检查排版。"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import sys
sys.path.insert(0, r"D:\EGOCourier")
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPixmap
from ego_gui.app import MainWindow

app = QApplication.instance() or QApplication(sys.argv)
w = MainWindow()
w.resize(1280, 900)
w.tabs.setCurrentWidget(w.export_tab)
w.show()
app.processEvents()

pm = w.grab()
pm.save(r"D:\EGOCourier\tests\_shot_export.png")
print("saved", pm.width(), pm.height())
