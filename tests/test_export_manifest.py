"""manifest.txt：把一次导出的来源通道/设备/时间写清楚。

重点是电脑端 USB 与手机端 Wi-Fi 两条固件通道并存时，要明确说明它们是
同一设备在不同时间点的快照，不是重复项。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ego_relay import export_manifest as em  # noqa: E402


def _mobile_summary():
    return {"phones": [{
        "phone": "Pixel 7",
        "bundles": [
            {"source": "zip", "zip": "x/ego-logs-app-20261020_100422_962.zip",
             "app_files": 2,
             "devices": [{"group": "SNX-20261020_100206_680-aaa", "files": 18}]},
            {"source": "firmware_logs",
             "dir": "x/mobile/Pixel 7/firmware_logs/SNX-20261020_100206_680-aaa",
             "app_files": 0,
             "devices": [{"group": "SNX-20261020_100206_680-aaa", "files": 18}]},
        ]}]}


class TestManifest(unittest.TestCase):
    def _write(self, **kw):
        d = tempfile.mkdtemp(prefix="mani_")
        p = em.write_manifest(d, keys=kw.pop("keys", ["firmware", "mobile"]),
                              export_ts="2026-10-20 10:39:36", **kw)
        with open(p, encoding="utf-8") as fh:
            return fh.read()

    def test_mobile_firmware_source_stated(self):
        text = self._write(pc_firmware=None, mobile=_mobile_summary())
        self.assertIn("固件日志来源：手机 EGOViewer 导出", text)
        self.assertIn("未重复从电脑 USB 拉取", text)
        self.assertIn("Pixel 7", text)
        self.assertIn("不解包重排", text)
        self.assertIn("SNX-20261020_100206_680-aaa", text)

    def test_pc_firmware_source_stated(self):
        text = self._write(pc_firmware={"at": 1792000000, "devices": {"EGOPro": 5}},
                           mobile=None)
        self.assertIn("固件日志来源：电脑 USB 直连设备", text)
        self.assertIn("EGOPro", text)

    def test_no_duplicate_warning_text_anywhere(self):
        for kw in ({"pc_firmware": {"at": 1792000000, "devices": {"EGOPro": 5}},
                    "mobile": _mobile_summary()},
                   {"pc_firmware": None, "mobile": _mobile_summary()},
                   {"pc_firmware": {"at": 1792000000, "devices": {"EGOPro": 5}},
                    "mobile": None}):
            text = self._write(**kw)
            self.assertNotIn("各有一份固件日志", text)
            self.assertNotIn("请勿当作重复项", text)

    def test_app_sdk_counts_listed(self):
        text = self._write(pc_firmware=None, mobile=None,
                           app_sdk_counts={"app": 5, "sdk": 2},
                           sdk_root=r"D:\EGOViewer")
        self.assertIn("app/：5 个文件", text)
        self.assertIn("sdk/：2 个文件", text)
        self.assertIn(r"D:\EGOViewer", text)

    def test_group_time_parsed_from_sn_dir(self):
        self.assertEqual(em._group_time("SNX-20261020_100206_680-aaa"),
                         "2026-10-20 10:02:06")
        self.assertEqual(em._group_time("garbage"), "未记录")

    def test_manifest_written_into_export_dir(self):
        d = tempfile.mkdtemp(prefix="mani_")
        p = em.write_manifest(d, keys=["mobile"], export_ts="2026-10-20 10:00:00")
        self.assertEqual(os.path.dirname(p), d)
        self.assertEqual(os.path.basename(p), "manifest.txt")
        self.assertTrue(os.path.isfile(p))


if __name__ == "__main__":
    unittest.main()
