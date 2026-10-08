import os, sys, tempfile, json
sys.path.insert(0, r"D:\EGOCourier")
from ego_relay import config, db
config.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")
db._local.conn = None
db.init_db()
from ego_relay import parsers, analysis
ps = parsers.import_path(r"D:\Orbbec\测试项目\EGO数采\EGO2615双目\数据\9.15\EGO_AK8896100BJ_20260915_143309")
r = analysis.align_and_analyze(ps)
s = db.get_session(r["session_id"])
print(json.dumps(s["meta"]["stats"], ensure_ascii=False, indent=2))
