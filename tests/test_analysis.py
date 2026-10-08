"""对齐与四类异常规则测试（纯标准库 unittest）。

- 正常数据 -> 0 异常
- 构造数据 -> 4 类异常全部命中
"""
import sys, os, unittest, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ego_relay import config, db
from ego_relay.parsers import ParsedSession, ImuData, ViewerLine
from ego_relay import analysis as a


class TestAnalysis(unittest.TestCase):
    def setUp(self):
        # 用临时 DB，避免污染真实数据；并重置线程本地连接缓存
        tmp = tempfile.mkdtemp()
        config.DB_PATH = os.path.join(tmp, "test.db")
        db._local.conn = None
        db.init_db()

    def _base(self):
        ps = ParsedSession(name="t", source_path="synthetic")
        ps.device_serial = "TEST01"
        return ps

    def test_all_four_rules_trigger(self):
        ps = self._base()
        ps.hw_ptp["left"] = {10: 1_000_000, 20: 2_000_000, 30: 3_000_000, 40: 5_000_000}
        ps.hw_ptp["right"] = {20: 2_003_000, 40: 5_000_000}  # frame20 双目差 3000us>1000
        ps.sei_hw_ptp["left"] = {10: 1_004_000, 40: 5_000_000}  # frame10 SEI 差 4000us>2000
        # 密集 IMU：只有 frame30 附近故意错位 5000us>2000，其余贴近
        ps.imu = [ImuData(1_000_500, "accel"), ImuData(2_000_500, "gyro"),
                  ImuData(3_005_000, "accel"), ImuData(5_000_500, "gyro")]
        ps.viewer_lines.append(ViewerLine(wallclock_us=0, recv_hw_frame_idx=40,
                                          app_recv_us=5_100_000, trace_id="a72f901ce24b42189ac350027bc11d45"))
        r = a.align_and_analyze(ps)
        rules = {x["rule"] for x in db.query("SELECT rule FROM anomalies")}
        self.assertEqual(rules, {"sei_copy_error", "app_receive_delay",
                                 "binocular_ptp_diff", "imu_video_sync"})
        # 每类规则至少命中 1 条
        for rule in rules:
            self.assertGreater(
                db.query_one("SELECT COUNT(*) AS n FROM anomalies WHERE rule=?", (rule,))["n"], 0)

    def test_clean_data_no_anomaly(self):
        ps = self._base()
        ps.hw_ptp["left"] = {1: 1_000_000, 2: 1_033_000}
        ps.hw_ptp["right"] = {1: 1_000_100, 2: 1_033_100}   # 双目差 100us < 1ms
        ps.sei_hw_ptp["left"] = {1: 1_000_000, 2: 1_033_000}  # SEI 一致
        ps.imu = [ImuData(1_000_500, "accel"), ImuData(1_033_500, "accel")]
        ps.viewer_lines.append(ViewerLine(wallclock_us=0, recv_hw_frame_idx=1,
                                          app_recv_us=1_020_000))  # 20ms < 50ms
        r = a.align_and_analyze(ps)
        self.assertEqual(r["anomalies"], 0)

    def test_anomaly_frame_capture(self):
        # 异常帧自动截帧：按硬件时间戳匹配 frame_dumps 图片
        import os
        img = os.path.join(tempfile.mkdtemp(), "cam_left_1000000.jpg")
        with open(img, "w") as f:
            f.write("fake-jpg")
        ps = self._base()
        ps.hw_ptp["left"] = {1: 1_000_000}
        ps.sei_hw_ptp["left"] = {1: 1_006_000}              # SEI 差 6000us>2000 -> 异常
        ps.imu = [ImuData(1_000_500, "accel")]
        ps.frame_images[("left", 1_000_000)] = img          # 对应 frame1 的采样帧
        a.align_and_analyze(ps)
        anom = db.query_one("SELECT * FROM anomalies WHERE rule='sei_copy_error'")
        self.assertIsNotNone(anom)
        self.assertEqual(anom["frame_image"], img)


if __name__ == "__main__":
    unittest.main(verbosity=2)
