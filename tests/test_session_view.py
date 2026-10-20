"""会话列表展示推导：来源分类、批次命名、设备识别、0 帧标注。"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ego_gui import session_view as sv  # noqa: E402


def _s(**kw):
    base = {"id": 1, "name": "", "source_path": "", "device_serial": None,
            "frames": 0, "anomalies": 0, "trace_id": None, "meta": "{}",
            "created_at": None, "start_us": None}
    base.update(kw)
    return base


class TestSourceKind(unittest.TestCase):
    def test_ego_export_dir_is_pc(self):
        self.assertEqual(sv.source_kind(_s(
            source_path=r"D:\out\EGO_export_20261020_103936")), "电脑端导出")

    def test_mobile_dir_is_phone(self):
        self.assertEqual(sv.source_kind(_s(
            source_path=r"D:\out\EGO_export_20261020_103936\mobile\Pixel 7")), "手机日志")

    def test_phone_zip_is_phone(self):
        self.assertEqual(sv.source_kind(_s(
            source_path=r"D:\tmp\app-logs-zips\ego-logs-app-20261020_092621_490.zip")),
            "手机日志")

    def test_manual_dir_is_analysis(self):
        self.assertEqual(sv.source_kind(_s(
            source_path=r"D:\some\local\folder")), "分析目录")

    def test_files_meta_can_indicate_mobile(self):
        meta = json.dumps({"files": [{"file": "mobile/Pixel 7/firmware_logs/x.log"}]})
        self.assertEqual(sv.source_kind(_s(source_path=r"D:\a\b", meta=meta)), "手机日志")

    def test_files_meta_can_indicate_firmware(self):
        meta = json.dumps({"files": [{"file": "firmware/EGOPro/orbbec.log"}]})
        self.assertEqual(sv.source_kind(_s(source_path=r"D:\a\b", meta=meta)), "电脑端导出")


class TestBatchLabel(unittest.TestCase):
    def test_ego_export_gets_human_label(self):
        self.assertEqual(
            sv.batch_label(_s(source_path=r"D:\o\EGO_export_20261020_103936")),
            "导出批次 2026-10-20 10:39:36")

    def test_phone_zip_label(self):
        self.assertEqual(
            sv.batch_label(_s(source_path=r"D:\t\ego-logs-app-20261020_092621_490.zip")),
            "手机日志包 2026-10-20 09:26:21")

    def test_merged_dir_is_not_shown_raw(self):
        label = sv.batch_label(_s(name="merged", source_path=r"D:\t\merged"))
        self.assertTrue(label.startswith("本地导入"))
        self.assertNotEqual(label, "merged")


class TestDeviceLabel(unittest.TestCase):
    def test_real_serial_kept(self):
        self.assertEqual(sv.device_label(_s(device_serial="AZLV961004Y", name="x")),
                         "AZLV961004Y")

    def test_serial_equal_name_is_unidentified(self):
        self.assertEqual(sv.device_label(_s(device_serial="merged", name="merged")),
                         "未识别")

    def test_path_like_serial_is_unidentified(self):
        self.assertEqual(sv.device_label(_s(device_serial="export", name="EGO_export_1")),
                         "未识别")
        self.assertEqual(sv.device_label(_s(device_serial="a/b", name="x")), "未识别")

    def test_missing_serial(self):
        self.assertEqual(sv.device_label(_s()), "未识别")


class TestDataState(unittest.TestCase):
    def test_zero_frames_marked_as_log_only(self):
        v = sv.row_view(_s(frames=0))
        self.assertEqual(v["state"], "仅日志")
        self.assertEqual(v["frames"], "无可对齐数据")
        self.assertEqual(v["anomalies"], "—")

    def test_frames_present(self):
        v = sv.row_view(_s(frames=1200, anomalies=3))
        self.assertEqual(v["state"], "可对齐")
        self.assertEqual(v["frames"], "1200")
        self.assertEqual(v["anomalies"], "3")


class TestBatchTime(unittest.TestCase):
    def test_parsed_from_dir_name(self):
        self.assertEqual(
            sv.batch_time(_s(source_path=r"D:\o\EGO_export_20261020_103936")),
            "2026-10-20 10:39:36")

    def test_fallback_to_created_at(self):
        # created_at 存的是微秒
        t = sv.batch_time(_s(source_path=r"D:\x\plain", created_at=1792000000000000))
        self.assertRegex(t, r"^20\d\d-\d\d-\d\d \d\d:\d\d:\d\d$")

    def test_no_time(self):
        self.assertEqual(sv.batch_time(_s()), "—")


if __name__ == "__main__":
    unittest.main()
