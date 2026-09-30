"""完整 GUI 联动测试（offscreen）：导入→卡片/图表/区间表/报告 + 图标。"""
import os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, r"D:\EGOCourier")
import matplotlib
matplotlib.use("QtAgg")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from ego_gui.app import MainWindow
from ego_gui import main as main_mod
from ego_relay import config, db

REAL = r"D:\Orbbec\测试项目\EGO数采\EGO2615双目\数据\9.15\EGO_AK8896100BJ_20260915_143309"
config.DB_PATH = os.path.join(tempfile.mkdtemp(), "gui.db")
db._local.conn = None
db.init_db()

app = QApplication(sys.argv)
w = MainWindow()

# 图标
icon = main_mod.make_icon()
print("icon created:", not icon.isNull())

res = {}
def do_import():
    w.import_tab._path = REAL
    w.import_tab.btn_import.setEnabled(True)
    w.import_tab._import()
    QTimer.singleShot(7000, check)

def check():
    it = w.import_tab
    res["state"] = it.lbl_state.text()
    res["cards"] = {k: v.text() for k, v in it.cards.items()}
    res["seg_rows"] = it.seg_table.rowCount()
    res["file_rows"] = it.table.rowCount()
    res["canvas_ok"] = it._figure is not None and len(it._figure.axes) == 2
    # 报告导出到临时文件
    out = os.path.join(tempfile.mkdtemp(), "rep.html")
    from ego_gui import report as R
    R.build_html_report(it.session_id, it._stats, it._segments, figure=it._figure, out_path=out)
    res["report_exists"] = os.path.exists(out)
    res["report_kb"] = os.path.getsize(out) // 1024 if os.path.exists(out) else 0
    app.quit()

QTimer.singleShot(300, do_import)
app.exec()
print("state:", res.get("state"))
print("cards:", res.get("cards"))
print("seg_rows:", res.get("seg_rows"), "| file_rows:", res.get("file_rows"), "| canvas2subplots:", res.get("canvas_ok"))
print("report:", res.get("report_exists"), res.get("report_kb"), "KB")
print("FULL GUI FLOW OK")
