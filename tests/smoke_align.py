"""对齐功能验证：导入 9.17 → 会话列表/帧表/异常表/trace/zip 导出。"""
import os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, r"D:\EGOCourier")
import matplotlib; matplotlib.use("QtAgg")
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from ego_gui.app import MainWindow
from ego_relay import config, db

REAL = r"D:\Orbbec\测试项目\EGO数采\EGO2615双目\数据\9.17\EGO_AK8896100BJ_20260917_140556"
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
    QTimer.singleShot(9000, check)
def check():
    it = w.import_tab
    res["sessions_combo"] = it.combo_sessions.count()
    res["frames_rows"] = it.frame_table.rowCount()
    res["anomaly_rows"] = it.anom_table.rowCount()
    res["sub_tabs"] = [it.sub.tabText(i) for i in range(it.sub.count())]
    # trace 搜索（该会话无 trace，应 0）
    it.trace_input.setText("abc")
    it._trace_search()
    res["trace_rows"] = it.trace_table.rowCount()
    # zip 导出
    out = os.path.join(tempfile.mkdtemp(), "s.zip")
    from ego_relay import export as E
    E.export_session(it.session_id, out_path=out)
    res["zip_kb"] = os.path.getsize(out)//1024
    app.quit()
QTimer.singleShot(300, do_import)
app.exec()
print("sub tabs:", res.get("sub_tabs"))
print("sessions combo:", res.get("sessions_combo"))
print("frames rows:", res.get("frames_rows"), "| anomaly rows:", res.get("anomaly_rows"))
print("trace rows (no trace):", res.get("trace_rows"), "| zip KB:", res.get("zip_kb"))
print("ALIGNMENT GUI OK")
