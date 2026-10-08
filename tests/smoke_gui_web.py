# -*- coding: utf-8 -*-
"""GUI 冒烟：离屏加载 MainWindow，断言合并后的工作台和关键控件。"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import sys
sys.path.insert(0, r"D:\EGOCourier")

from PySide6.QtWidgets import QApplication
from ego_gui.app import MainWindow, STYLE

app = QApplication.instance() or QApplication(sys.argv)
w = MainWindow()

tabs = [w.tabs.tabText(i) for i in range(w.tabs.count())]
expected = ["设备与日志", "会话记录", "日志分析", "Trace 查询", "WiFi", "阈值"]
print("Tab 顺序:", tabs)
assert tabs == expected, tabs

# 合并设备与日志工作台控件
et = w.device_tab
for n in ("dev_table", "btn_scan", "edit_ssid", "edit_pwd", "btn_provision",
          "edit_ip", "edit_app_dir", "chk_firmware", "edit_sdk_root", "edit_sdk_port",
          "btn_export", "task_log", "_layout_splitter"):
    assert hasattr(et, n), f"设备工作台缺 {n}"
assert et._layout_splitter.count() == 3
# 会话记录页：会话表 + 详情
st = w.sessions_tab
for n in ("sess_table", "btn_open", "btn_back", "_detail"):
    assert hasattr(st, n), f"会话记录页缺 {n}"
assert st._detail is not None and hasattr(st._detail, "anom_table")
# 日志分析页
it = w.analysis_tab
for n in ("btn_pick_dir", "btn_pick_zip", "btn_analyze", "file_table"):
    assert hasattr(it, n), f"日志分析页缺 {n}"
# Trace 页
tt = w.trace_tab
for n in ("trace_input", "btn_trace", "sess_table", "trace_table"):
    assert hasattr(tt, n), f"Trace 页缺 {n}"
# WiFi 页
wt = w.wifi_tab
for n in ("btn_scan", "wifi_table", "kpi_ssid", "kpi_signal", "kpi_state"):
    assert hasattr(wt, n), f"WiFi 页缺 {n}"
# 阈值页
th = w.thresholds_tab
for n in ("btn_save", "spins"):
    assert hasattr(th, n), f"阈值页缺 {n}"

# QSS 关键 token（浅色顶栏 + 蓝色激活下划线，去掉黑色；大按钮/大卡片数字）
assert "QTabBar { background:#ffffff" in STYLE, "Tab 栏应改为浅色"
assert "#1f2937" not in STYLE, "不应再出现黑色顶栏 #1f2937"
for token in ("border-bottom:2px solid #3b82f6", "padding:9px 16px", "font-size:22px"):
    assert token in STYLE, f"QSS 缺 {token}"

print("关键控件齐全，QSS token 齐全")
print("GUI_WEB_SMOKE_OK")
