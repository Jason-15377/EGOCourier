import os, sys, tempfile, json
sys.path.insert(0, r"D:\EGOCourier")
from ego_relay import config, db
config.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")
db._local.conn = None
db.init_db()
from ego_relay import parsers, analysis

REAL = r"D:\Orbbec\测试项目\EGO数采\EGO2615双目\数据\9.17\EGO_AK8896100BJ_20260917_140556"
ps = parsers.import_path(REAL)
r = analysis.align_and_analyze(ps)
s = db.get_session(r["session_id"])
sc = s["meta"]["stats"]["script"]
print("== 脚本对齐指标 ==")
print("SEI/CSV:")
for side, v in sc["sei_vs_csv"].items():
    print(f"  {side}: SEI={v['sei_count']} CSV={v['csv_count']} 一致={v['match']} 数量一致={v['count_match']} 不匹配={v['mismatch']}")
print("视频流:")
for side, v in sc["streams"].items():
    print(f"  {side}: 帧={v['total_frames']} 预期={v['expected_hz']:.0f}Hz 实际={v['actual_hz']:.2f}Hz "
          f"时长={v['duration_s']:.2f}s 丢帧事件={v['gaps']} 丢失={v['lost_frames']} 率={v['drop_rate']*100:.4f}%")
print("IMU:", sc["imu"])
print("左右同步:", sc["stereo_sync"])
print("IMU-视频同步:", sc["imu_video_sync"])
