"""验证 charts + report + ImportTab 联动（offscreen）。"""
import os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, r"D:\EGOCourier")
import matplotlib
matplotlib.use("Agg")

from ego_relay import config, db
config.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")
db._local.conn = None
db.init_db()
from ego_relay import parsers, analysis
from ego_gui import charts as C, report as R

REAL = r"D:\Orbbec\测试项目\EGO数采\EGO2615双目\数据\9.15\EGO_AK8896100BJ_20260915_143309"
ps = parsers.import_path(REAL)
r = analysis.align_and_analyze(ps)
sid = r["session_id"]
s = db.get_session(sid)
stats = s["meta"]["stats"]

frames = C.load_left_frames(sid)
print("left frames:", len(frames))
fig = C.build_figure(frames, stats, thr_ms=50)
print("figure built, subplots:", len(fig.axes))

out = os.path.join(tempfile.mkdtemp(), "report.html")
html = R.build_html_report(sid, stats, stats.get("ptp_anomaly_segments", []),
                           figure=fig, out_path=out)
print("report html bytes:", len(html))
print("has kpi:", "概览统计" in html, "| has img:", "<img" in html, "| segments rows:", stats.get("ptp_anomaly_segments"))
print("REPORT OK ->", out)
