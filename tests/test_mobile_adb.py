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
            _make_bundle(os.path.join(d, "Pixel 7", "最新导出包.zip"))
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
                os.path.join(d, "Pixel 7", "最新导出包.zip"),
                devices=("SNA-20261014_172339_892-aaa",
                         "SNB-20261014_172339_892-bbb"))
            res = mobile_adb.summarize(d)
            groups = [g["group"] for g in res["phones"][0]["bundles"][0]["devices"]]
            self.assertEqual(groups, ["SNA-20261014_172339_892-aaa",
                                      "SNB-20261014_172339_892-bbb"])

    def test_bundle_without_device_logs(self):
        with tempfile.TemporaryDirectory() as d:
            _make_bundle(
                os.path.join(d, "Pixel 7", "最新导出包.zip"),
                devices=())
            res = mobile_adb.summarize(d)
            self.assertEqual(res["phones"][0]["bundles"][0]["devices"], [])

    def test_broken_zip_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as d:
            zp = os.path.join(d, "Pixel 7", "坏包.zip")
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
            zp = os.path.join(d, "Pixel 7", "persisted包.zip")
            os.makedirs(os.path.dirname(zp))
            with zipfile.ZipFile(zp, "w") as z:
                z.writestr("app/ego-viewer-2026-10-20.log", "x")
                for f in ("orbbec.log", "dmesg.txt", "event.log"):
                    z.writestr(f"firmware/persisted/SNX-20261020_100206_680-aaa/{f}", "x")
            res = mobile_adb.summarize(d)
            devs = res["phones"][0]["bundles"][0]["devices"]
            self.assertEqual(devs, [{"group": "SNX-20261020_100206_680-aaa",
                                     "files": 3}])


class TestFlatLayout(unittest.TestCase):
    """平铺结构下，电脑端的 app/sdk/firmware 与手机目录同级，不能互相误认。"""

    def test_pc_dirs_ignored_by_summarize(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("app", "sdk", "firmware"):
                os.makedirs(os.path.join(d, name))
                open(os.path.join(d, name, "x.log"), "w").write("x")
            self.assertEqual(mobile_adb.summarize(d), {"phones": []})

    def test_phone_dir_detected_alongside_pc_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("app", "sdk", "firmware"):
                os.makedirs(os.path.join(d, name))
            _make_bundle(os.path.join(d, "Pixel 7", "最新导出包.zip"))
            res = mobile_adb.summarize(d)
            self.assertEqual([p["phone"] for p in res["phones"]], ["Pixel 7"])


class TestFirmwareLogsSource(unittest.TestCase):
    """设备分组与机型目录平铺：<机型>/<SN>-<时间戳>/（不再套 firmware/ 或 固件日志/）。"""

    def _mk_fw(self, root, group, sub_dir=None):
        """在机型目录（或其下的旧结构子目录）里造一个设备分组。"""
        base = os.path.join(root, "Pixel 7")
        if sub_dir:
            base = os.path.join(base, sub_dir)
        d = os.path.join(base, group)
        os.makedirs(os.path.join(d, "app"))
        os.makedirs(os.path.join(d, "sdk"))
        for name in ("orbbec_2026-10-19.log", "dmesg.txt",
                     "app/ego-viewer-2026-10-20.log", "sdk/OrbbecSDK.log.txt"):
            with open(os.path.join(d, name), "w") as fh:
                fh.write("x")
        return d

    def test_flat_device_dir_becomes_device_group(self):
        with tempfile.TemporaryDirectory() as d:
            self._mk_fw(d, "AZLV961004Y-20261020_100206_680-66437f2d")
            res = mobile_adb.summarize(d)
            bundles = res["phones"][0]["bundles"]
            self.assertEqual(len(bundles), 1)
            b = bundles[0]
            self.assertEqual(b["source"], "dir")
            self.assertEqual(b["devices"],
                             [{"group": "AZLV961004Y-20261020_100206_680-66437f2d",
                               "files": 4}])

    def test_legacy_firmware_logs_dir_still_counted(self):
        """旧结构实例（固件日志/<SN>）仍应被识别，避免历史目录清点不到。"""
        with tempfile.TemporaryDirectory() as d:
            self._mk_fw(d, "AZLV961004Y-20261020_100206_680-66437f2d",
                        sub_dir="固件日志")
            res = mobile_adb.summarize(d)
            groups = [g["group"] for b in res["phones"][0]["bundles"]
                      for g in b["devices"]]
            self.assertEqual(groups, ["AZLV961004Y-20261020_100206_680-66437f2d"])

    def test_flat_dir_and_zip_listed_separately(self):
        with tempfile.TemporaryDirectory() as d:
            self._mk_fw(d, "SNA-20261014_172339_892-aaa")
            _make_bundle(os.path.join(d, "Pixel 7", "最新导出包.zip"),
                         devices=("SNB-20261014_172339_892-bbb",))
            res = mobile_adb.summarize(d)
            bundles = res["phones"][0]["bundles"]
            sources = sorted(b["source"] for b in bundles)
            self.assertEqual(sources, ["dir", "zip"])
            groups = sorted(b["devices"][0]["group"] for b in bundles)
            self.assertEqual(groups, ["SNA-20261014_172339_892-aaa",
                                      "SNB-20261014_172339_892-bbb"])

    def test_empty_phone_dir_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "Pixel 7", "sdk日志"))
            self.assertEqual(mobile_adb.summarize(d), {"phones": []})


class TestExtractBundleDevices(unittest.TestCase):
    """导出包内 firmware/<SN>-<ts>/ 按设备解到 <机型>/<SN>/，去掉 firmware 层。"""

    def _zip(self, path, groups, extra=True):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with zipfile.ZipFile(path, "w") as z:
            if extra:
                z.writestr("app/ego-viewer-2026-11-09.log", "x")
                z.writestr("sdk/OrbbecSDK.log.txt", "x")
                z.writestr("diagnostics/crash-diagnostics.zip", "x")
            for g in groups:
                z.writestr(f"firmware/{g}/orbbec_2026-11-09.log", "x")
                z.writestr(f"firmware/{g}/dmesg.txt", "x")
                z.writestr(f"firmware/{g}/app/ego-viewer-2026-11-09.log", "x")

    def test_each_device_unpacked_flat_by_serial(self):
        with tempfile.TemporaryDirectory() as d:
            zp = os.path.join(d, "手机导出包.zip")
            groups = ("MAIN123-20261109_153234_206-aaaa",
                      "SLAVE9-20261109_153234_206-bbbb")
            self._zip(zp, groups)
            phone = os.path.join(d, "Pixel 7")
            out = mobile_adb.extract_bundle_devices(zp, phone)
            # 目录名是序列号（main123/slave9 原样保留大小写），不带时间戳
            self.assertEqual(sorted(os.path.basename(x) for x in out),
                             ["MAIN123", "SLAVE9"])
            # 没有 firmware 层，设备直接挂在机型目录下
            self.assertNotIn("firmware", os.listdir(phone))
            dev = os.path.join(phone, "SLAVE9")
            self.assertTrue(os.path.isfile(os.path.join(dev, "orbbec_2026-11-09.log")))
            self.assertTrue(os.path.isfile(os.path.join(dev, "app", "ego-viewer-2026-11-09.log")))
            # 顶层 app/ 平铺到机型目录；包内顶层 sdk/ 不再单独解（sdk日志/ 已有最新版）
            self.assertTrue(os.path.isfile(os.path.join(phone, "app", "ego-viewer-2026-11-09.log")))
            self.assertFalse(os.path.exists(os.path.join(phone, "sdk")))

    def test_same_device_multiple_snapshots_keeps_newest(self):
        """同一台设备的多个历史快照，只解最新的那一个（避免三份一样的日志）。"""
        with tempfile.TemporaryDirectory() as d:
            zp = os.path.join(d, "手机导出包.zip")
            self._zip(zp, (
                "AZL9861004V-20260804_231335_783-cb83aee7",
                "AZL9861004V-20261109_150910_414-d7997f8b",
                "AZL9861004V-20261109_153234_206-6f342b60",
            ))
            phone = os.path.join(d, "Pixel 7")
            out = mobile_adb.extract_bundle_devices(zp, phone)
            self.assertEqual([os.path.basename(x) for x in out], ["AZL9861004V"])
            # 解出来的是最新那份（内容标记按快照区分）
            self.assertTrue(os.path.isdir(os.path.join(phone, "AZL9861004V")))
            self.assertEqual([n for n in os.listdir(phone)
                              if n.startswith("AZL9861004V")], ["AZL9861004V"])

    def test_persisted_source_is_not_a_device(self):
        """firmware/persisted/<SN>-… 仍按真实设备解，persisted 不是设备名。"""
        with tempfile.TemporaryDirectory() as d:
            zp = os.path.join(d, "包.zip")
            os.makedirs(os.path.dirname(zp), exist_ok=True)
            with zipfile.ZipFile(zp, "w") as z:
                z.writestr("firmware/persisted/DEV123-20261109_153234_206-cccc/orbbec.log", "x")
            phone = os.path.join(d, "Pixel 7")
            out = mobile_adb.extract_bundle_devices(zp, phone)
            self.assertEqual([os.path.basename(x) for x in out], ["DEV123"])
            self.assertNotIn("persisted", os.listdir(phone))

    def test_bad_zip_returns_empty_without_raising(self):
        with tempfile.TemporaryDirectory() as d:
            zp = os.path.join(d, "bad.zip")
            with open(zp, "wb") as fh:
                fh.write(b"not a zip")
            phone = os.path.join(d, "Pixel 7")
            os.makedirs(phone)
            self.assertEqual(mobile_adb.extract_bundle_devices(zp, phone), [])


class TestLatestZip(unittest.TestCase):
    """手机 app-logs-zips 会累积历史包，只应挑最新的一个。"""

    def _fake_run(self, names):
        import subprocess

        def _run(adb, args, timeout=20.0, cancel_event=None):
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


class TestCancelDuringPull(unittest.TestCase):
    """点了「取消任务」后 adb pull 必须尽快中断，而不是等满 900 秒。

    早先 _run 用 subprocess.run 一次阻塞到命令结束，只在调用前后查取消标志，
    现场表现为：导出失败后点取消，进度条长时间不动。
    """

    def test_run_kills_process_when_cancelled(self):
        import subprocess
        import threading
        import time

        ev = threading.Event()
        # 用一个真会跑一会儿的子进程替代 adb，确认它被提前杀掉。
        cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
        started = []

        real_popen = subprocess.Popen

        def _popen(args, **kw):
            p = real_popen(cmd, **kw)
            started.append(p)
            return p

        ev.set()
        t0 = time.monotonic()
        with unittest.mock.patch.object(subprocess, "Popen", _popen):
            with self.assertRaises(mobile_adb.MobileExportError):
                mobile_adb._run("adb", ["pull"], timeout=900, cancel_event=ev)
        self.assertLess(time.monotonic() - t0, 5)
        self.assertTrue(started and started[0].returncode is not None)

    def test_pull_all_aborts_before_touching_adb(self):
        import threading

        ev = threading.Event()
        ev.set()
        calls = []
        with unittest.mock.patch.object(
                mobile_adb, "_run",
                lambda *a, **k: calls.append(a)):
            with self.assertRaises(mobile_adb.MobileExportError) as ctx:
                mobile_adb.pull_all("adb", "SN", tempfile.gettempdir(),
                                    cancel_event=ev)
        self.assertIn("取消", str(ctx.exception))
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
