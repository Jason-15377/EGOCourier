"""一键导出把「手机日志」与固件/本地日志收进同一时间戳目录（不依赖真机）。

用假 adb 与假 SDK 跑真实的 EgoOneClickExportWorker.run()，验证：
- keys 同时含 mobile 与 firmware 时，产物落在同一个 EGO_export_<ts>/ 下；
- app/sdk 的筛选只作用于本地，不会把手机日志筛掉；
- 只勾 mobile 时不触碰 EGOViewer 根目录。
"""
import os
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

from ego_gui.device_tab import EgoOneClickExportWorker  # noqa: E402


def _fake_pull_all(adb, serial, dest_root, note=None, progress=None, cancel_event=None):
    """模拟 adb pull：按手机真实结构写出 app-logs-zips / sdk-logs / firmware_logs。"""
    phone = os.path.join(dest_root, "Pixel 7")
    os.makedirs(phone)
    with zipfile.ZipFile(os.path.join(phone, "手机导出包.zip"), "w") as z:
        z.writestr("app/ego-viewer-2026-10-20.log", "x")
        z.writestr("firmware/persisted/SNX-20261020_100206_680-aaa/orbbec.log", "x")
    fw = os.path.join(phone, "固件日志", "SNX-20261020_100206_680-aaa")
    os.makedirs(os.path.join(fw, "app"))
    for name in ("orbbec_2026-10-19.log", "dmesg.txt", "app/ego-viewer-2026-10-20.log"):
        with open(os.path.join(fw, name), "w") as fh:
            fh.write("x")
    if progress:
        progress(100)


def _fake_firmware(root_dir, sdk_root="", **kw):
    """模拟 Orbbec SDK 固件导出：每台设备一个目录。"""
    d = os.path.join(root_dir, "EGOPro")
    os.makedirs(d)
    with open(os.path.join(d, "orbbec_2026-10-19.log"), "w") as fh:
        fh.write("x")
    return {"ok": {"EGOPro": {"name": "EGOPro", "serial": "AZL",
                              "connection": "USB3.2",
                              "paths": [os.path.join(d, "orbbec_2026-10-19.log")]}},
            "errors": []}


class TestUnifiedExport(unittest.TestCase):
    def _run(self, keys, **kw):
        out = tempfile.mkdtemp(prefix="uni_")
        root = tempfile.mkdtemp(prefix="ev_")
        w = EgoOneClickExportWorker(root, out, keys, **kw)
        done = []
        fail = []
        w.done.connect(lambda *a: done.append(a))
        w.failed.connect(fail.append)
        with mock.patch("ego_relay.mobile_adb.pull_all", _fake_pull_all), \
             mock.patch("ego_relay.orbbec_sdk.export_all_firmware_logs", _fake_firmware), \
             mock.patch("ego_relay.mobile_adb.find_adb", return_value="adb"):
            w.run()
        return out, done, fail

    def test_mobile_firmware_replaces_usb_pull(self):
        """同时勾手机与固件日志：只留手机那份，不再从电脑 USB 拉一遍。"""
        calls = []
        orig = _fake_firmware

        def _spy(*a, **k):
            calls.append(a)
            return orig(*a, **k)

        out = tempfile.mkdtemp(prefix="uni_")
        root = tempfile.mkdtemp(prefix="ev_")
        w = EgoOneClickExportWorker(
            root, out, ["firmware", "mobile"], mobile_serials=["SN123"],
            stage_kwargs={"start": None, "end": None})
        done, fail, msgs = [], [], []
        w.done.connect(lambda *a: done.append(a))
        w.failed.connect(fail.append)
        w.status.connect(msgs.append)
        with mock.patch("ego_relay.mobile_adb.pull_all", _fake_pull_all), \
             mock.patch("ego_relay.orbbec_sdk.export_all_firmware_logs", _spy), \
             mock.patch("ego_relay.mobile_adb.find_adb", return_value="adb"):
            w.run()

        self.assertFalse(fail, f"不应失败：{fail}")
        self.assertEqual(calls, [], "勾了手机日志就不该再调 USB 固件拉取")
        export_dir, _sid, _report, summary = done[0]
        self.assertTrue(export_dir.startswith(out))
        # 目录里没有 firmware/，固件日志只在 mobile/ 下
        self.assertEqual(sorted(os.listdir(export_dir)), ["Pixel 7"])
        groups = [d["group"] for ph in summary["phones"]
                  for b in ph["bundles"] for d in b["devices"]]
        self.assertEqual(groups, ["SNX-20261020_100206_680-aaa"] * 2)
        self.assertTrue(any("跳过电脑 USB 直连拉取" in m for m in msgs),
                        f"应说明为何跳过：{msgs}")

    def test_firmware_only_still_uses_usb(self):
        """没勾手机日志时，固件日志照旧从电脑 USB 拉取。"""
        out, done, fail = self._run(["firmware"], mobile_serials=[])
        self.assertFalse(fail, f"不应失败：{fail}")
        export_dir = done[0][0]
        self.assertEqual(sorted(os.listdir(export_dir)), ["firmware"])

    def test_mobile_only_needs_no_ego_viewer_root(self):
        out, done, fail = self._run(["mobile"], mobile_serials=["SN123"])
        self.assertFalse(fail, f"只勾手机不应要求 EGOViewer 根目录：{fail}")
        export_dir = done[0][0]
        self.assertEqual(sorted(os.listdir(export_dir)), ["Pixel 7"])

    def test_mobile_skipped_when_no_serial(self):
        out, done, fail = self._run(["mobile"], mobile_serials=[])
        self.assertTrue(fail, "没有手机序列号时应报错，而不是静默成功")


if __name__ == "__main__":
    unittest.main()
