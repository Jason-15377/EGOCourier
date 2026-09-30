"""端到端演示：自动截帧（用真实数据 + 放大阈值强制触发双目异常）。
在临时 DB 中运行，不污染主库。
"""
import os, sys, tempfile, zipfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ego_relay import config, db
from ego_relay import parsers, analysis, export

REAL = r"D:\Orbbec\测试项目\EGO数采\EGO2615双目\数据\9.15\EGO_AK8896100BJ_20260915_143309"

def main():
    tmp = tempfile.mkdtemp()
    config.DB_PATH = os.path.join(tmp, "demo.db")
    db._local.conn = None
    db.init_db()

    ps = parsers.import_path(REAL)
    print(f"frame_dumps 采样帧: {len(ps.frame_images)}")
    # 放大阈值强制触发双目 PTP 同步差（真实双目差约 300us）
    r = analysis.align_and_analyze(ps, {"binocular_ptp_diff": 100})
    sid = r["session_id"]
    print(f"会话 #{sid} 帧={r['frames']} 异常={r['anomalies']}")

    anoms = db.query("SELECT id,rule,frame_index,side,delta_us,frame_image FROM anomalies")
    with_img = [a for a in anoms if a["frame_image"]]
    print(f"异常总数: {len(anoms)}  命中截帧: {len(with_img)}")
    for a in with_img[:5]:
        print(f"   anom#{a['id']} {a['rule']} frame={a['frame_index']} {a['side']} delta={a['delta_us']}us -> {os.path.basename(a['frame_image'])}")

    # 导出并检查截帧打包
    out = os.path.join(tmp, "demo_export.zip")
    export.export_session(sid, out_path=out)
    names = zipfile.ZipFile(out).namelist()
    frames = [n for n in names if n.startswith("anomaly_frames/")]
    print(f"导出 zip 内截帧数: {len(frames)}")
    for n in frames[:5]:
        print("   ", n)
    return out

if __name__ == "__main__":
    out = main()
    print("\n示例导出(带截帧):", out)
