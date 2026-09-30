"""验证：导出 Tab 会话列表 + 同步阈值重算（不弹对话框，直接调重算逻辑）。"""
import os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, r"D:\EGOCourier")
import matplotlib; matplotlib.use("QtAgg")
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from ego_gui.app import MainWindow
from ego_relay import config, db, analysis

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
    QTimer.singleShot(7000, step2)
def step2():
    it = w.import_tab
    sid = it.session_id
    res["sess_rows_export"] = w.export_tab.sess_table.rowCount()
    # 同步重算（复刻 _edit_thresholds 主体，跳过对话框）
    from ego_relay import api as api_mod
    api_mod._save_thresholds({"binocular_ptp_diff": 50})
    it.lbl_state.setText("重算中…")
    r = analysis.recompute_session(sid, {"binocular_ptp_diff": 50})
    it.session_id = r["session_id"]
    it._load_analysis()
    res["anom_after"] = db.query_one("SELECT COUNT(*) AS n FROM anomalies WHERE session_id=?", (sid,))["n"]
    res["frame_rows"] = it.frame_table.rowCount()
    app.quit()
QTimer.singleShot(300, do_import)
app.exec()
print("export tab session rows:", res.get("sess_rows_export"))
print("anomalies after sync recompute:", res.get("anom_after"))
print("frame rows:", res.get("frame_rows"))
print("SYNC RECOMPUTE OK" if res.get("anom_after", 0) > 0 else "FAILED")
