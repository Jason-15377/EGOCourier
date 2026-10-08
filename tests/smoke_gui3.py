"""通用导入 Tab 交互测试：经 GUI 线程导入真实会话文件夹。"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import sys
sys.path.insert(0, r"D:\EGOCourier")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from ego_gui.app import MainWindow
from ego_relay import db

REAL = r"D:\Orbbec\测试项目\EGO数采\EGO2615双目\数据\9.15\EGO_AK8896100BJ_20260915_143309"
app = QApplication(sys.argv)
w = MainWindow()

# 用临时库避免污染
import tempfile
from ego_relay import config
config.DB_PATH = os.path.join(tempfile.mkdtemp(), "gui.db")
db._local.conn = None
db.init_db()

state = {}
def do_import():
    w.import_tab._path = REAL
    w.import_tab.btn_import.setEnabled(True)
    w.import_tab._import()
    QTimer.singleShot(6000, check)

def check():
    state["state"] = w.import_tab.lbl_state.text()
    state["total"] = w.import_tab.lbl_total.text()
    state["ok"] = w.import_tab.lbl_ok.text()
    state["fail"] = w.import_tab.lbl_fail.text()
    state["rows"] = w.import_tab.table.rowCount()
    state["sessions"] = db.query_one("SELECT COUNT(*) AS n FROM sessions")["n"]
    print("state:", state)
    app.quit()

QTimer.singleShot(300, do_import)
app.exec()
print("GUI import OK")
