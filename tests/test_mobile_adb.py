"""mobile_adb：多设备分组清点、目录结构整理、异常提示（不依赖真机）。"""
import io
import os
import sys
import tempfile
import unittest
import unittest.mock
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ego_relay import mobile_adb  # noqa: E402


def _make_bundle(path, devices=("OBC168DEFAULT-20261014_172339_892-71e10790",),
                 app_files=("app/ego-viewer-2026-10-14.log",)):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name in app_files:
            z.writestr(name, "x")
        z.writestr("sdk/OrbbecSDK.log.txt", "x")
        z.writestr("diagnostics/crash-diagnostics.zip", "x")
        for dev in devices:
            z.writestr(f"firmware/{dev}/orbbec.log", "x")
            z.writestr(f"firmware/{dev}/messages.log", "x")
            z.writestr(f"firmware/{dev}/app/ego-viewer-2026-10-14.log", "x")
            z.writestr(f"firmware/{dev}/sdk/OrbbecSDK.log.txt", "x")


class TestSummarize(unittest.TestCase):
    def test_groups_by_device_and_counts_app_files(self):
        with tempfile.TemporaryDirectory() as d:
            _make_bundle(os.path.join(
                d, "手机端", "Pixel 7", "最新导出包.zip"))
            res = mobile_adb.summarize(d)
            self.assertEqual(len(res["phones"]), 1)
            phone = res["phones"][0]
            self.assertEqual(phone["phone"], "Pixel 7")
            self.assertEqual(len(phone["bundles"]), 1)
            bundle = phone["bundles"][0]
            self.assertEqual(bundle["app_files"], 1)     # 只数顶层 app/，不数 firmware/ 里的
            self.assertEqual(len(bundle["devices"]), 1)
            self.assertEqual(bundle["devices"][0]["files"], 4)

    def test_multi_device_bundle_reports_every_group(self):
        with tempfile.TemporaryDirectory() as d:
            _make_bundle(
                os.path.join(d, "手机端", "Pixel 7", "最新导出包.zip"),
                devices=("SNA-20261014_172339_892-aaa",
                         "SNB-20261014_172339_892-bbb"))
            res = mobile_adb.summarize(d)
            groups = [g["group"] for g in res["phones"][0]["bundles"][0]["devices"]]
            self.assertEqual(groups, ["SNA-20261014_172339_892-aaa",
                                      "SNB-20261014_172339_892-bbb"])

    def test_bundle_without_device_logs(self):
        with tempfile.TemporaryDirectory() as d:
            _make_bundle(
                os.path.join(d, "手机端", "Pixel 7", "最新导出包.zip"),
                devices=())
            res = mobile_adb.summarize(d)
            self.assertEqual(res["phones"][0]["bundles"][0]["devices"], [])

    def test_broken_zip_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as d:
            zp = os.path.join(d, "手机端", "Pixel 7", "坏包.zip")
            os.makedirs(os.path.dirname(zp))
            with open(zp, "wb") as fh:
                fh.write(b"not a zip")
            res = mobile_adb.summarize(d)
            self.assertEqual(res["phones"][0]["bundles"][0]["error"], "不是有效的 zip")

    def test_missing_root_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(mobile_adb.summarize(d), {"phones": []})


class TestFirmwareGroupNaming(unittest.TestCase):
    def test_direct_device_group(self):
        self.assertEqual(
            mobile_adb._device_group_of(
                "firmware/AZLV961004Y-20261020_100206_680-66437f2d/orbbec.log"),
            "AZLV961004Y-20261020_100206_680-66437f2d")

    def test_persisted_is_source_not_device(self):
        # 实测包结构：firmware/persisted/<SN>-.../，不能把 persisted 当成设备
        self.assertEqual(
            mobile_adb._device_group_of(
                "firmware/persisted/AZLV961004Y-20261020_100206_680-66437f2d/"
                "orbbec_2026-10-19.log"),
            "AZLV961004Y-20261020_100206_680-66437f2d")

    def test_persisted_bundle_groups_by_real_device(self):
        with tempfile.TemporaryDirectory() as d:
            zp = os.path.join(d, "手机端", "Pixel 7", "persisted包.zip")
            os.makedirs(os.path.dirname(zp))
            with zipfile.ZipFile(zp, "w") as z:
                z.writestr("app/ego-viewer-2026-10-20.log", "x")
                for f in ("orbbec.log", "dmesg.txt", "event.log"):
                    z.writestr(f"firmware/persisted/SNX-20261020_100206_680-aaa/{f}", "x")
            res = mobile_adb.summarize(d)
            devs = res["phones"][0]["bundles"][0]["devices"]
            self.assertEqual(devs, [{"group": "SNX-20261020_100206_680-aaa",
                                     "files": 3}])


class TestFirmwareLogsSource(unittest.TestCase):
    """firmware_logs/<SN>-<时间戳>/ 是 zip 读不到时唯一可靠的固件日志副本。"""

    def _mk_fw(self, root, group, n_files=4):
        d = os.path.join(root, "手机端", "Pixel 7", "固件日志", group)
        os.makedirs(os.path.join(d, "app"))
        os.makedirs(os.path.join(d, "sdk"))
        with open(os.path.join(d, "orbbec_2026-10-19.log"), "w") as fh:
            fh.write("x")
        with open(os.path.join(d, "dmesg.txt"), "w") as fh:
            fh.write("x")
        with open(os.path.join(d, "app", "ego-viewer-2026-10-20.log"), "w") as fh:
            fh.write("x")
        with open(os.path.join(d, "sdk", "OrbbecSDK.log.txt"), "w") as fh:
            fh.write("x")
        return d

    def test_firmware_logs_dir_becomes_device_group(self):
        with tempfile.TemporaryDirectory() as d:
            self._mk_fw(d, "AZLV961004Y-20261020_100206_680-66437f2d")
            res = mobile_adb.summarize(d)
            bundles = res["phones"][0]["bundles"]
            self.assertEqual(len(bundles), 1)
            b = bundles[0]
            self.assertEqual(b["source"], "firmware_logs")
            self.assertEqual(b["devices"],
                             [{"group": "AZLV961004Y-20261020_100206_680-66437f2d",
                               "files": 4}])

    def test_both_sources_listed_separately(self):
        with tempfile.TemporaryDirectory() as d:
            self._mk_fw(d, "SNA-20261014_172339_892-aaa")
            _make_bundle(os.path.join(d, "手机端", "Pixel 7", "最新导出包.zip"),
                         devices=("SNB-20261014_172339_892-bbb",))
            res = mobile_adb.summarize(d)
            bundles = res["phones"][0]["bundles"]
            sources = sorted(b["source"] for b in bundles)
            self.assertEqual(sources, ["firmware_logs", "zip"])
            groups = sorted(b["devices"][0]["group"] for b in bundles)
            self.assertEqual(groups, ["SNA-20261014_172339_892-aaa",
                                      "SNB-20261014_172339_892-bbb"])

    def test_empty_firmware_logs_dir_adds_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "手机端", "Pixel 7", "固件日志"))
            res = mobile_adb.summarize(d)
            self.assertEqual(res["phones"][0]["bundles"], [])


class TestLatestZip(unittest.TestCase):
    """手机 app-logs-zips 会累积历史包，只应挑最新的一个。"""

    def _fake_run(self, names):
        import subprocess

        def _run(adb, args, timeout=20.0):
            if args[-2:] == ["ls", mobile_adb.REMOTE_DIRS["app-logs-zips"]]:
                return subprocess.CompletedProcess(
                    args, 0, chr(10).join(names).encode(), b"")
            return subprocess.CompletedProcess(args, 1, b"", b"no")
        return _run

    def test_picks_newest_by_timestamp(self):
        names = ["ego-logs-app-20261014_172139_115.zip",
                 "ego-logs-app-20261020_114243_322.zip",
                 "ego-logs-app-20261020_100422_962.zip"]
        with unittest.mock.patch.object(mobile_adb, "_run", self._fake_run(names)):
            got = mobile_adb.latest_zip("adb", "SN")
        self.assertTrue(got.endswith("ego-logs-app-20261020_114243_322.zip"))

    def test_empty_dir_returns_blank(self):
        with unittest.mock.patch.object(mobile_adb, "_run", self._fake_run([])):
            self.assertEqual(mobile_adb.latest_zip("adb", "SN"), "")

    def test_ignores_non_zip_files(self):
        names = ["notes.txt", "ego-logs-app-20261014_172139_115.zip"]
        with unittest.mock.patch.object(mobile_adb, "_run", self._fake_run(names)):
            got = mobile_adb.latest_zip("adb", "SN")
        self.assertTrue(got.endswith("ego-logs-app-20261014_172139_115.zip"))


class TestFlatten(unittest.TestCase):
    def test_removes_extra_nesting_layer(self):
        with tempfile.TemporaryDirectory() as d:
            # 模拟 adb pull 的结果：dest/x/x/file，剥掉那层与源目录同名的壳
            inner = os.path.join(d, "x", "x")
            os.makedirs(inner)
            with open(os.path.join(inner, "a.zip"), "w") as fh:
                fh.write("1")
            files = mobile_adb._flatten(os.path.join(d, "x"), "x")
            self.assertEqual(len(files), 1)
            self.assertTrue(files[0].endswith(os.path.join("x", "a.zip")))
            self.assertFalse(os.path.isdir(inner))

    def test_keeps_device_group_dir(self):
        """设备分组目录不是 adb 套的壳，不能被压平。"""
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "固件日志")
            grp = os.path.join(root, "AZLV961004Y-20261020_100206_680-x")
            os.makedirs(os.path.join(grp, "app"))
            with open(os.path.join(grp, "orbbec.log"), "w") as fh:
                fh.write("1")
            files = mobile_adb._flatten(root, "firmware_logs")
            self.assertTrue(os.path.isdir(grp), "设备分组目录应保留")
            self.assertEqual(len(files), 1)

    def test_flat_directory_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "y")
            os.makedirs(root)
            with open(os.path.join(root, "a.zip"), "w") as fh:
                fh.write("1")
            self.assertEqual(len(mobile_adb._flatten(root)), 1)


class TestSafe(unittest.TestCase):
    def test_device_model_used_as_folder_name(self):
        self.assertEqual(mobile_adb._safe("Pixel 7", "sn"), "Pixel 7")
        self.assertEqual(mobile_adb._safe("a/b:c", "sn"), "a_b_c")
        self.assertEqual(mobile_adb._safe("", "sn"), "sn")


if __name__ == "__main__":
    unittest.main()
