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
    res["state"] = it.lbl_state.text()
    res["cards"] = {k: v.text() for k, v in it.cards.items()}
    out = os.path.join(tempfile.mkdtemp(), "rep.html")
    from ego_gui import report as R
    R.build_html_report(it.session_id, it._stats, it._segments, figure=it._figure, out_path=out)
    html = open(out, encoding="utf-8").read()
    res["has_sei"] = "SEI 时间戳 vs CSV" in html
    res["has_drop"] = "视频丢帧检测" in html
    res["has_stereo"] = "左右 Color" in html
    res["has_imuv"] = "IMU 与视频" in html
    res["kbytes"] = os.path.getsize(out) // 1024
    app.quit()
QTimer.singleShot(300, do_import)
app.exec()
print("state:", res.get("state"))
print("cards:", res.get("cards"))
print("report sections: SEI=%s drop=%s stereo=%s imuv=%s | %s KB" % (
    res.get("has_sei"), res.get("has_drop"), res.get("has_stereo"), res.get("has_imuv"), res.get("kbytes")))
print("9.17 SCRIPT-PARITY GUI OK")
