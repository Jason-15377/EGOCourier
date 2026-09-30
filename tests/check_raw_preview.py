import os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, r"D:\EGOCourier")
import matplotlib
matplotlib.use("QtAgg")

from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv)

from ego_relay import config, db
config.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")
db._local.conn = None
db.init_db()
from ego_relay import parsers, analysis
from ego_gui.import_tab import ImportTab

REAL = r"D:\Orbbec\测试项目\EGO数采\EGO2615双目\数据\9.15\EGO_AK8896100BJ_20260915_143309"
ps = parsers.import_path(REAL)
r = analysis.align_and_analyze(ps)
sid = r["session_id"]
s = db.get_session(sid)
start = s["start_us"]; end = start + 2_000_000

it = ImportTab(lambda m: None)
preview = it._raw_preview(sid, start, end)
print("preview chars:", len(preview))
print(preview[:600])
print("RAW PREVIEW OK")
