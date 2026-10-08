# -*- coding: utf-8 -*-
"""功能冒烟：会话记录页打开会话 → 详情加载 + 阈值重算不崩。"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import sys
sys.path.insert(0, r"D:\EGOCourier")

from PySide6.QtWidgets import QApplication
from ego_gui.app import MainWindow
from ego_relay import db

app = QApplication.instance() or QApplication(sys.argv)
db.init_db()
w = MainWindow()

sessions = db.list_sessions(200)
assert sessions, "测试库无会话"
sid = sessions[0]["id"]
print("打开会话 #", sid)

w.sessions_tab.open_session(sid)
assert w.sessions_tab._detail.session_id == sid
assert w.sessions_tab.stack.currentIndex() == 1, "应切到详情页"
print("详情已加载, stack=", w.sessions_tab.stack.currentIndex())

# 返回列表
w.sessions_tab._back_to_list()
assert w.sessions_tab.stack.currentIndex() == 0
print("返回列表 OK")

# 阈值重算当前会话
w.sessions_tab.open_session(sid)
w.thresholds_tab._save()  # 保存默认阈值并触发 saved -> recompute_current
assert w.sessions_tab._detail.session_id == sid
print("阈值保存+重算 OK")

# 跨页：设备工作台 imported 信号应切到会话记录
w.device_tab.imported.emit(sid)
assert w.tabs.currentWidget() is w.sessions_tab
print("跨页跳转 OK")
print("FUNC_SMOKE_OK")
