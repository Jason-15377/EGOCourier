# -*- coding: utf-8 -*-
"""v1.0.6 缺陷修复的回归测试（不依赖真机、不依赖本机 EGOViewer 安装）。

覆盖：
  T1  EGOViewer 根目录按内容识别（不再依赖 data/logs 与 "win_x64" 字样），
      两层目录布局（<盘>\\Soft&tools\\软件工具\\EGOViewer_*）也能被扫到；
  T1  app 日志能从 Log/ 收到（v2.0.17 布局），且不会与 sdk 抢同一个文件；
  T5  固件日志落盘为 firmware/<设备>/<时间戳>/，同目录重复导出不覆盖；
  T6  OrbbecSdkError 带 kind，上层据此给出不同提示。
"""
import os
import tempfile
import unittest
from unittest import mock

from ego_relay import evlog_pack as ep
from ego_relay import orbbec_sdk as sdk


def _touch(path, content="x", mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


class TestViewerRootDetection(unittest.TestCase):
    """安装根目录判据必须基于内容，而不是旧版目录名与 data/logs。"""

    def test_new_layout_without_data_logs(self):
        """v2.0.17：日志在 Log/，没有 data/logs —— 修复前这种目录被判为无效。"""
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "EGOViewer_v2.0.17_202610152231_6f55ace5_win_x64")
            _touch(os.path.join(root, "EGOViewer.exe"), "")
            _touch(os.path.join(root, "Log", "EGOViewer_20261021000559.log"), "a")
            self.assertFalse(os.path.isdir(os.path.join(root, "data", "logs")))
            self.assertTrue(ep.is_ego_viewer_root(root))

    def test_name_without_win_x64_still_detected(self):
        """目录名不一定含 win_x64，改名后也要能认出来。"""
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "EGOViewer-renamed")
            _touch(os.path.join(root, "EGOViewer.exe"), "")
            self.assertTrue(ep.is_ego_viewer_root(root))

    def test_empty_dir_is_not_root(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(ep.is_ego_viewer_root(d))
            self.assertFalse(ep.is_ego_viewer_root(""))
            self.assertFalse(ep.is_ego_viewer_root(os.path.join(d, "nope")))

    def test_two_level_tool_dir_layout(self):
        """现场实际布局：<盘>\\Soft&tools\\软件工具\\EGOViewer_v*\（两层）。"""
        with tempfile.TemporaryDirectory() as d:
            tool = os.path.join(d, "Soft&tools", "软件工具")
            root = os.path.join(tool, "EGOViewer_v2.0.17_202610152231_6f55ace5_win_x64")
            _touch(os.path.join(root, "EGOViewer.exe"), "")
            with mock.patch.object(ep, "_candidate_search_roots",
                                   return_value=[os.path.join(d, "Soft&tools")]):
                roots = ep.find_ego_viewer_roots()
            self.assertEqual(roots, [root])

    def test_custom_root_that_is_install_dir(self):
        """用户把安装目录本身填进自定义列表时也要命中。"""
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "EGOViewer.exe"), "")
            with mock.patch.object(ep, "_candidate_search_roots", return_value=[d]):
                self.assertEqual(ep.find_ego_viewer_roots(), [d])


class TestAppLogCandidates(unittest.TestCase):
    def test_app_category_has_new_and_old_dirs(self):
        app = next(c for c in ep.CATEGORIES if c["key"] == "app")
        self.assertIn("Log", app["candidate_rel_dirs"])
        self.assertIn("data/logs", app["candidate_rel_dirs"])
        # 回退模式必须显式包含 EGOViewer_*.log，否则整根递归捞不到 Log/ 下的日志
        self.assertIn("EGOViewer_*.log", app["fallback_patterns"])

    def test_app_and_sdk_do_not_share_files(self):
        """app 与 sdk 都在 Log/ 下，同一个文件只能归属一个分类。"""
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "Log", "EGOViewer_20261021000559.log"), "a")
            _touch(os.path.join(d, "Log", "OrbbecSDK.20261021000560.log.txt"), "b")
            res = ep.discover(d)
            app = [os.path.basename(f["abs"]) for f in res["app"]]
            sdk_files = [os.path.basename(f["abs"]) for f in res["sdk"]]
            self.assertEqual(app, ["EGOViewer_20261021000559.log"])
            self.assertEqual(sdk_files, ["OrbbecSDK.20261021000560.log.txt"])

    def test_firmware_skips_non_log_artifacts(self):
        """firmware 目录里的 .lock / dmesg.txt / device_status.txt 不该被收进来。"""
        with tempfile.TemporaryDirectory() as d:
            base = os.path.join(d, "data", "firmware-logs", "20261019-114100")
            _touch(os.path.join(base, "orbbec_2026-10-19.log"), "a")
            _touch(os.path.join(base, "messages.log"), "b")
            _touch(os.path.join(base, "observice.console.log"), "c")
            _touch(os.path.join(base, ".event.log.lock"), "")
            _touch(os.path.join(base, "dmesg.txt"), "d")
            _touch(os.path.join(base, "device_status.txt"), "e")
            names = sorted(os.path.basename(f["abs"])
                           for f in ep.discover(d, ["firmware"])["firmware"])
            self.assertEqual(names, ["messages.log", "observice.console.log",
                                     "orbbec_2026-10-19.log"])


class TestFirmwareLayout(unittest.TestCase):
    """T5：磁盘结构与 rel 必须一致，且重复导出不覆盖。"""

    def test_timestamp_dir_under_device(self):
        def fake(root_dir, **kw):
            # SDK 只认一个输出目录；模拟它按设备建子目录写文件
            dev_dir = os.path.join(root_dir, "EGOPro")
            _touch(os.path.join(dev_dir, "orbbec_2026-10-21.log"), "fw")
            return {"ok": {"EGOPro": {"name": "EGOPro", "paths": [
                os.path.join(dev_dir, "orbbec_2026-10-21.log")]}},
                "errors": [], "skipped": []}

        import ego_gui.device_tab as dt
        at = 1770000000.0
        with mock.patch.object(sdk, "export_all_firmware_logs", fake):
            with tempfile.TemporaryDirectory() as tmp:
                fw_root = os.path.join(tmp, "firmware")
                out = dt._pull_firmware_from_device(
                    fw_root, at, "", sdk, object())
                stamp = __import__("datetime").datetime.fromtimestamp(at) \
                    .strftime("%Y%m%d-%H%M%S")
                self.assertEqual(len(out), 1)
                abs_path, rel = out[0]
                self.assertEqual(rel, f"EGOPro/{stamp}/orbbec_2026-10-21.log")
                self.assertEqual(
                    os.path.normcase(abs_path),
                    os.path.normcase(os.path.join(
                        fw_root, "EGOPro", stamp, "orbbec_2026-10-21.log")))

    def test_same_second_does_not_overwrite(self):
        """同一时刻（同一秒）对同一目录导出两次：两份文件都要留下。"""
        import ego_gui.device_tab as dt
        at = 1770000000.0

        def fake(root_dir, **kw):
            dev_dir = os.path.join(root_dir, "EGOPro")
            _touch(os.path.join(dev_dir, "orbbec.log"), "fw")
            return {"ok": {"EGOPro": {"name": "EGOPro", "paths": [
                os.path.join(dev_dir, "orbbec.log")]}},
                "errors": [], "skipped": []}

        with mock.patch.object(sdk, "export_all_firmware_logs", fake):
            with tempfile.TemporaryDirectory() as tmp:
                fw_root = os.path.join(tmp, "firmware")
                first = dt._pull_firmware_from_device(fw_root, at, "", sdk, object())
                second = dt._pull_firmware_from_device(fw_root, at, "", sdk, object())
                self.assertEqual(len(first), 1)
                self.assertEqual(len(second), 1)
                self.assertNotEqual(first[0][0], second[0][0])
                self.assertTrue(os.path.isfile(first[0][0]))
                self.assertTrue(os.path.isfile(second[0][0]))


class TestErrorKinds(unittest.TestCase):
    """T6：不同失败原因必须能区分，提示不能一律说"设备未连接"。"""

    def test_kind_defaults_to_other(self):
        self.assertEqual(sdk.OrbbecSdkError("x").kind, "other")

    def test_kind_is_kept(self):
        self.assertEqual(sdk.OrbbecSdkError("x", kind="dll").kind, "dll")

    def test_missing_dll_is_classified(self):
        with mock.patch.object(sdk, "find_sdk_dll", return_value=""):
            with self.assertRaises(sdk.OrbbecSdkError) as ctx:
                sdk.list_devices()
        self.assertEqual(ctx.exception.kind, "dll")

    def test_busy_and_disabled_detected(self):
        self.assertEqual(sdk._classify_error("device is busy / already open"), "busy")
        self.assertEqual(sdk._classify_error("Firmware log already disabled"),
                         "fw_disabled")
        self.assertEqual(sdk._classify_error("boom"), "other")

    def test_hints_are_distinct(self):
        import ego_gui.device_tab as dt
        hints = dt._FIRMWARE_ERR_HINTS
        self.assertIn("dll", hints)
        self.assertIn("no_device", hints)
        self.assertNotEqual(dt._firmware_error_hint(sdk.OrbbecSdkError("a", kind="dll")),
                            dt._firmware_error_hint(sdk.OrbbecSdkError("a", kind="no_device")))
        self.assertIn("OrbbecSDK.dll",
                      dt._firmware_error_hint(sdk.OrbbecSdkError("a", kind="dll")))


class TestNoHardcodedPaths(unittest.TestCase):
    """config 里不能再写死某台机器的绝对路径与测试网段 IP。"""

    def test_config_has_no_absolute_search_roots(self):
        from ego_relay import config
        self.assertEqual(list(config.EGO_VIEWER_SEARCH_ROOTS), [])

    def test_config_has_no_test_ip_prefilled(self):
        from ego_relay import config
        self.assertEqual(config.DEVICE_IP, "")
        self.assertEqual(config.EXAMPLE_DEVICE_IP, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
