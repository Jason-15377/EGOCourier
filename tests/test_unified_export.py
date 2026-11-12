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
    """模拟 adb pull：按新结构把设备分组平铺在 <机型>/ 下（无 firmware 层）。

    真实手机导出包内 firmware/<SN>-<ts>/ 会在 pull_all 里被展平到机型目录；
    这里直接写出展平后的结果，模拟「主设备 + 从设备」两组都在。
    """
    phone = os.path.join(dest_root, "Pixel 7")
    os.makedirs(phone)
    with zipfile.ZipFile(os.path.join(phone, "手机导出包.zip"), "w") as z:
        z.writestr("app/ego-viewer-2026-10-20.log", "x")
        z.writestr("firmware/SNX-20261020_100206_680-aaa/orbbec.log", "x")
        z.writestr("firmware/SNY-20261020_100206_680-bbb/orbbec.log", "x")
    for grp in ("SNX-20261020_100206_680-aaa", "SNY-20261020_100206_680-bbb"):
        fw = os.path.join(phone, grp)
        os.makedirs(os.path.join(fw, "app"))
        for name in ("orbbec_2026-10-19.log", "dmesg.txt", "app/ego-viewer-2026-10-20.log"):
            with open(os.path.join(fw, name), "w") as fh:
                fh.write("x")
    if progress:
        progress(100)


def _fake_pull_all_mobile_azl(adb, serial, dest_root, note=None, progress=None,
                              cancel_event=None):
    """模拟 adb pull，且手机里正好连着 USB 也会拉到的那台（SN = AZL）。"""
    phone = os.path.join(dest_root, "Pixel 7")
    grp = os.path.join(phone, "AZL-20261020_100206_680-aaa")
    os.makedirs(grp)
    with open(os.path.join(grp, "orbbec_phone.log"), "w") as fh:
        fh.write("phone")
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

    def test_mobile_and_firmware_both_pulled(self):
        """同时勾手机与固件日志：两条来源都拉（手机覆盖手机端设备，USB 覆盖从设备）。"""
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
        self.assertEqual(len(calls), 1, "勾了手机日志也要连 USB 拉一遍，补从设备日志")
        export_dir, _sid, _report, summary = done[0]
        self.assertTrue(export_dir.startswith(out))
        # 手机机型目录平铺在根；电脑端固件统一收进 firmware/
        top = sorted(os.listdir(export_dir))
        self.assertIn("Pixel 7", top)
        self.assertIn("firmware", top)
        groups = sorted(d["group"] for ph in summary["phones"]
                        for b in ph["bundles"] for d in b["devices"])
        self.assertEqual(groups, ["SNX-20261020_100206_680-aaa",
                                  "SNY-20261020_100206_680-bbb"])

    def test_firmware_only_still_uses_usb(self):
        """没勾手机日志时，固件日志从电脑 USB 拉取，设备目录按序列号挂在导出目录下。"""
        out, done, fail = self._run(["firmware"], mobile_serials=[])
        self.assertFalse(fail, f"不应失败：{fail}")
        export_dir = done[0][0]
        # 统一收进 firmware/，设备目录按序列号（AZL）命名，不再套拉取时刻
        self.assertIn("firmware", sorted(os.listdir(export_dir)))
        self.assertEqual(sorted(os.listdir(os.path.join(export_dir, "firmware"))),
                         ["AZL"])

    def test_mobile_only_needs_no_ego_viewer_root(self):
        out, done, fail = self._run(["mobile"], mobile_serials=["SN123"])
        self.assertFalse(fail, f"只勾手机不应要求 EGOViewer 根目录：{fail}")
        export_dir = done[0][0]
        self.assertEqual(sorted(os.listdir(export_dir)), ["Pixel 7"])

    def test_mobile_skipped_when_no_serial(self):
        out, done, fail = self._run(["mobile"], mobile_serials=[])
        self.assertTrue(fail, "没有手机序列号时应报错，而不是静默成功")

    def test_firmware_ignores_stale_mobile_dirs_when_mobile_not_selected(self):
        """没勾手机日志时，输出目录里的历史手机目录不能把 USB 设备吞进去。

        现场：输出目录就是上一次的手机导出目录，里面已有 <机型>/<SN>/。
        USB 拉到同一 SN 若并进去，三台设备就从根目录消失、只剩一个机型文件夹，
        看起来像被删掉了。这里要求设备目录一律新建在导出目录根下。
        """
        out = tempfile.mkdtemp(prefix="uni_")
        root = tempfile.mkdtemp(prefix="ev_")
        stale = os.path.join(out, "Pixel 7", "AZL")
        os.makedirs(stale)
        with open(os.path.join(stale, "orbbec_old.log"), "w") as fh:
            fh.write("old")
        w = EgoOneClickExportWorker(root, out, ["firmware"],
                                    stage_kwargs={"start": None, "end": None})
        done, fail = [], []
        w.done.connect(lambda *a: done.append(a))
        w.failed.connect(fail.append)
        with mock.patch("ego_relay.orbbec_sdk.export_all_firmware_logs", _fake_firmware):
            w.run()
        self.assertFalse(fail, f"不应失败：{fail}")
        export_dir = done[0][0]
        self.assertIn("AZL", sorted(os.listdir(os.path.join(export_dir, "firmware"))),
                      "USB 设备目录必须建在 firmware/ 下")
        self.assertTrue(os.path.isfile(os.path.join(
            export_dir, "firmware", "AZL", "orbbec_2026-10-19.log")))
        self.assertEqual(sorted(os.listdir(stale)), ["orbbec_old.log"],
                         "历史手机目录应原样保留，不写入本次 USB 产物")

    def test_firmware_merges_into_mobile_dir_when_mobile_selected(self):
        """勾了手机日志时反过来：USB 产物并进手机的 <机型>/<SN>/，同一台不出现两次。"""
        out = tempfile.mkdtemp(prefix="uni_")
        root = tempfile.mkdtemp(prefix="ev_")
        w = EgoOneClickExportWorker(
            root, out, ["firmware", "mobile"], mobile_serials=["SN123"],
            stage_kwargs={"start": None, "end": None})
        done, fail = [], []
        w.done.connect(lambda *a: done.append(a))
        w.failed.connect(fail.append)
        with mock.patch("ego_relay.mobile_adb.pull_all", _fake_pull_all_mobile_azl), \
             mock.patch("ego_relay.orbbec_sdk.export_all_firmware_logs", _fake_firmware), \
             mock.patch("ego_relay.mobile_adb.find_adb", return_value="adb"):
            w.run()
        self.assertFalse(fail, f"不应失败：{fail}")
        export_dir = done[0][0]
        fw_dir = os.path.join(export_dir, "firmware")
        self.assertFalse(os.path.isdir(os.path.join(fw_dir, "AZL")),
                         "勾了手机时同一台设备的 USB 产物应并入手机目录，"
                         "不在 firmware/ 另起一份")
        merged = os.path.join(export_dir, "Pixel 7", "AZL-20261020_100206_680-aaa")
        self.assertTrue(os.path.isfile(os.path.join(merged, "orbbec_2026-10-19.log")))


    def test_all_firmware_devices_grouped_under_firmware_dir(self):
        """三台设备的固件日志统一进 firmware/<SN>/，方便整包交给开发。

        勾了手机日志时尤其要确认：手机机型目录仍然平铺在根下（会话来源判定依赖
        它），电脑端固件则全部收进 firmware/，根目录不会散落一堆 SN 文件夹。
        """
        def _three_devices(root_dir, sdk_root="", **kw):
            ok = {}
            for name, sn in (("EGOPro", "AZL"), ("UMI Finger", "AZR"),
                             ("EGO-4B", "BZL")):
                d = os.path.join(root_dir, name)
                os.makedirs(d)
                f = os.path.join(d, "orbbec_2026-10-19.log")
                with open(f, "w") as fh:
                    fh.write("x")
                ok[name] = {"name": name, "serial": sn, "connection": "USB3.2",
                            "paths": [f]}
            return {"ok": ok, "errors": []}

        out = tempfile.mkdtemp(prefix="uni_")
        root = tempfile.mkdtemp(prefix="ev_")
        w = EgoOneClickExportWorker(
            root, out, ["firmware", "mobile"], mobile_serials=["SN123"],
            stage_kwargs={"start": None, "end": None})
        done, fail = [], []
        w.done.connect(lambda *a: done.append(a))
        w.failed.connect(fail.append)
        with mock.patch("ego_relay.mobile_adb.pull_all", _fake_pull_all),              mock.patch("ego_relay.orbbec_sdk.export_all_firmware_logs", _three_devices),              mock.patch("ego_relay.mobile_adb.find_adb", return_value="adb"):
            w.run()

        self.assertFalse(fail, f"不应失败：{fail}")
        export_dir = done[0][0]
        top = sorted(os.listdir(export_dir))
        self.assertIn("Pixel 7", top, "手机机型目录仍平铺在导出根下")
        for sn in ("AZL", "AZR", "BZL"):
            self.assertNotIn(sn, top, f"{sn} 不应散落在导出根目录")
        self.assertEqual(sorted(os.listdir(os.path.join(export_dir, "firmware"))),
                         ["AZL", "AZR", "BZL"])
        self.assertTrue(os.path.isfile(os.path.join(
            export_dir, "firmware", "BZL", "orbbec_2026-10-19.log")))

    def test_firmware_dir_reused_on_rerun_into_same_export_dir(self):
        """同一个导出目录里重复拉取时复用 firmware/<SN>/，不再套一层同名目录。

        正常流程每次会新建 EGO_export_<ts>/，所以这里直接把本轮导出目录指向一个
        已有 firmware/<SN>/ 的目录（等价于就地重跑），验证不会出现
        firmware/AZL/AZL/ 这种层级，且上一轮的文件保留。
        """
        out = tempfile.mkdtemp(prefix="uni_")
        export_dir = os.path.join(out, "EGO_export_20261020_100000")
        existing = os.path.join(export_dir, "firmware", "AZL")
        os.makedirs(existing)
        with open(os.path.join(existing, "orbbec_prev.log"), "w") as fh:
            fh.write("prev")

        root = tempfile.mkdtemp(prefix="ev_")
        w = EgoOneClickExportWorker(root, out, ["firmware"],
                                    stage_kwargs={"start": None, "end": None})
        fail = []
        w.failed.connect(fail.append)
        from ego_relay import orbbec_sdk
        with mock.patch("ego_relay.orbbec_sdk.export_all_firmware_logs", _fake_firmware):
            items = w._pull_firmware(export_dir, __import__("time").time(),
                                     orbbec_sdk, __import__("ego_relay.config",
                                                            fromlist=["x"]))
        self.assertEqual(sorted(os.listdir(os.path.join(export_dir, "firmware"))),
                         ["AZL"], "不应出现 firmware/AZL/AZL 这类重复层级")
        self.assertTrue(os.path.isfile(os.path.join(
            export_dir, "firmware", "AZL", "orbbec_prev.log")),
                        "上一轮的文件应保留")
        self.assertTrue(all(rel.startswith("firmware/AZL/") for _p, rel in items),
                        f"相对路径应为 firmware/<SN>/…：{items}")


if __name__ == "__main__":
    unittest.main()
