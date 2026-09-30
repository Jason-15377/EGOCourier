import os, sys, tempfile
sys.path.insert(0, r"D:\EGOCourier")
from ego_relay import config, db
config.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")
db._local.conn = None
db.init_db()
from ego_relay import parsers, analysis

REAL = r"D:\Orbbec\测试项目\EGO数采\EGO2615双目\数据\9.15\EGO_AK8896100BJ_20260915_143309"
ps = parsers.import_path(REAL)
r = analysis.align_and_analyze(ps)
sid = r["session_id"]
before = db.query_one("SELECT COUNT(*) AS n FROM anomalies WHERE session_id=?", (sid,))["n"]
print("before:", before)
r2 = analysis.recompute_session(sid, {"binocular_ptp_diff": 50})
after = db.query_one("SELECT COUNT(*) AS n FROM anomalies WHERE session_id=?", (sid,))["n"]
print("after:", after, "| new frames:", r2["frames"], "| anomalies:", r2["anomalies"])
