"""GUI 冒烟测试：offscreen 平台构造主窗口，验证无异常后退出。"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import sys
sys.path.insert(0, r"D:\EGOCourier")

from PySide6.QtWidgets import QApplication
from ego_gui.app import MainWindow

app = QApplication(sys.argv)
w = MainWindow()
print("MainWindow constructed OK")
print("  tabs:", w.tabs.count(), [w.tabs.tabText(i) for i in range(w.tabs.count())])
print("  device workbench:",
      w.device_tab.dev_table.columnCount(), w.device_tab._layout_splitter.count(),
      w.device_tab.lbl_ble_state.text())
w.close()
print("GUI smoke OK")
