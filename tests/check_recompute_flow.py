"""贴近真实 GUI 流程：ImportWorker 导入后 RecomputeWorker 重算。"""
import os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, r"D:\EGOCourier")
import matplotlib; matplotlib.use("QtAgg")
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from ego_gui.app import MainWindow
from ego_relay import config, db

REAL = r"D:\Orbbec\测试项目\EGO数采\EGO2615双目\数据\9.15\EGO_AK8896100BJ_20260915_143309"
config.DB_PATH = os.path.join(tempfile.mkdtemp(), "gui.db")
db._local.conn = None
db.init_db()
app = QApplication(sys.argv)
w = MainWindow()
res = {}
def do_import():
    w.import_tab._path = REAL
    w.import_tab.btn_import.setEnabled(True)
    w.import_tab._import()
    QTimer.singleShot(7000, do_recompute)
def do_recompute():
    sid = w.import_tab.session_id
    w.import_tab._recompute = RecomputeWorker(sid, {"binocular_ptp_diff": 50})
    w.import_tab._recompute.done.connect(on_done)
    w.import_tab._recompute.failed.connect(lambda e: (res.__setitem__("err", e), app.quit()))
    w.import_tab._recompute.start()
def on_done(sid):
    res["ok"] = db.query_one("SELECT COUNT(*) AS n FROM anomalies WHERE session_id=?", (sid,))["n"]
    app.quit()
QTimer.singleShot(300, do_import)
app.exec()
print("recompute result:", res)
print("FLOW OK")
