"""端到端验证：attach-log（真实会话 + 真实 app 日志）。临时 DB + 临时附件区，不污染主库。"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ego_relay import config, db
from ego_relay import parsers, analysis

REAL = r"D:\Orbbec\测试项目\EGO数采\EGO2615双目\数据\9.15\EGO_AK8896100BJ_20260915_143309"
APP_LOG = r"C:\temp_ego_zips2\app\ego-viewer-2026-09-15.log"

def main():
    tmp = tempfile.mkdtemp()
    config.DB_PATH = os.path.join(tmp, "db.sqlite")
    config.DATA_ROOT = os.path.join(tmp, "data")
    db._local.conn = None
    db.init_db()

    ps = parsers.import_path(REAL)
    r = analysis.align_and_analyze(ps)
    sid = r["session_id"]
    print(f"导入会话 #{sid}: 帧={r['frames']} 异常={r['anomalies']} trace={ps.trace_id or '-'}")

    print(f"\n附加 app 日志: {os.path.basename(APP_LOG)}")
    r2 = analysis.attach_app_log(sid, APP_LOG)
    s = db.get_session(sid)
    print(f"重算后: 帧={r2['frames']} 异常={r2['anomalies']}")
    print(f"会话 trace_id 现为: {s['trace_id'] or '-'}")
    print(f"log_files 数: {len(db.query('SELECT * FROM log_files WHERE session_id=?',(sid,)))}")
    anoms = db.query("SELECT rule,frame_index,side,delta_us FROM anomalies")
    print(f"异常明细({len(anoms)}):")
    for a in anoms[:20]:
        print(f"   {a['rule']} frame={a['frame_index']} {a['side']} delta={a['delta_us']}us")

    if __name__ != "__main__":
        return
    # 幂等：再附一次不应重复加文件
    r3 = analysis.attach_app_log(sid, APP_LOG)
    n = len(db.query("SELECT * FROM log_files WHERE session_id=?", (sid,)))
    print(f"\n再次附加后 log_files 数: {n}（应为上一次相同，幂等）")

if __name__ == "__main__":
    main()
