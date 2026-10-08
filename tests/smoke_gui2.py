# -*- coding: utf-8 -*-
"""GUI 冒烟：离屏加载 MainWindow，校验主 Tab 顺序与 BLE 配网控件存在。"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import sys
sys.path.insert(0, r"D:\EGOCourier")

from PySide6.QtWidgets import QApplication
from ego_gui.app import MainWindow

app = QApplication.instance() or QApplication(sys.argv)
w = MainWindow()

tabs = [w.tabs.tabText(i) for i in range(w.tabs.count())]
print("主 Tab 顺序:", tabs)
assert tabs[0] == "设备与日志", tabs

et = w.device_tab
for name in ("dev_table", "btn_scan", "edit_ssid", "edit_pwd", "btn_provision",
             "btn_export", "edit_sdk_root", "task_log"):
    assert hasattr(et, name), f"缺少控件 {name}"
print("BLE 配网控件齐全")

from ego_relay import ble_provision
print("EgoLowBle.dll 可用:", ble_provision.dll_available())

print("GUI_SMOKE_OK")
