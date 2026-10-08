"""阈值重算测试：RecomputeWorker 用低阈值重算，验证异常被触发。"""
import os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, r"D:\EGOCourier")
import matplotlib; matplotlib.use("QtAgg")
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from ego_relay import config, db
from ego_relay import parsers, analysis
from ego_gui.import_tab import RecomputeWorker

REAL = r"D:\Orbbec\测试项目\EGO数采\EGO2615双目\数据\9.15\EGO_AK8896100BJ_20260915_143309"
config.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")
db._local.conn = None
db.init_db()
app = QApplication(sys.argv)

ps = parsers.import_path(REAL)
r = analysis.align_and_analyze(ps)
sid = r["session_id"]
before = db.query_one("SELECT COUNT(*) AS n FROM anomalies WHERE session_id=?", (sid,))["n"]
print("anomalies before (default thr):", before)

res = {}
def run():
    # 用很低的 binocular 阈值触发双目异常
    w = RecomputeWorker(sid, {"binocular_ptp_diff": 50})
    w.done.connect(done)
    w.failed.connect(lambda e: (print("FAIL", e), app.quit()))
    w.start()
def done(new_sid):
    after = db.query_one("SELECT COUNT(*) AS n FROM anomalies WHERE session_id=?", (new_sid,))["n"]
    res["after"] = after
    app.quit()
QTimer.singleShot(200, run)
app.exec()
print("anomalies after (low thr):", res.get("after"))
print("RECOMPUTE OK" if res.get("after", 0) > 0 else "RECOMPUTE FAILED")
