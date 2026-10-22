# -*- coding: utf-8 -*-
"""Orbbec SDK adapter tests that do not require a connected device."""
import ctypes
import os
import pathlib
import tempfile
import unittest

from ego_relay.orbbec_sdk import (
    FirmwareLogWriter,
    OrbbecSdkError,
    PACKET_BEGIN,
    PACKET_DATA,
    PACKET_END,
    find_sdk_dll,
)


class TestFirmwareLogWriter(unittest.TestCase):
    def test_assembles_callback_packets(self):
        with tempfile.TemporaryDirectory() as tmp:
            progress = []
            writer = FirmwareLogWriter(tmp, lambda pct, name: progress.append((pct, name)))
            payload = ctypes.create_string_buffer(b"firmware")
            writer.packet(b"orbbec.log", PACKET_BEGIN, None, 0, 1, 0)
            writer.packet(b"orbbec.log", PACKET_DATA, payload, 4, 1, 0)
            writer.packet(b"orbbec.log", PACKET_END, ctypes.byref(payload, 4), 4, 1, 1)
            self.assertEqual(pathlib.Path(tmp, "orbbec.log").read_bytes(), b"firmware")
            self.assertEqual(progress[-1][0], 100)

    def test_assembles_multiple_files_in_one_batch(self):
        """一次导出上报多个文件时，全部都要落盘（回归：曾因批完成判定早退而丢文件）。"""
        with tempfile.TemporaryDirectory() as tmp:
            writer = FirmwareLogWriter(tmp)
            def _w(name, payload, total, completed):
                buf = ctypes.create_string_buffer(payload, len(payload))
                writer.packet(name, PACKET_BEGIN, None, 0, total, completed)
                writer.packet(name, PACKET_DATA, buf, len(payload), total, completed)
                writer.packet(name, PACKET_END, ctypes.byref(buf, len(payload)),
                              0, total, completed)
            _w(b"orbbec.log", b"ORBBEC", 3, 1)
            _w(b"messages.log", b"SYSLOG", 3, 2)
            _w(b"observice.console.log", b"AOE", 3, 3)
            self.assertEqual(pathlib.Path(tmp, "orbbec.log").read_bytes(), b"ORBBEC")
            self.assertEqual(pathlib.Path(tmp, "messages.log").read_bytes(), b"SYSLOG")
            self.assertEqual(pathlib.Path(tmp, "observice.console.log").read_bytes(), b"AOE")
            self.assertEqual(len(writer.paths), 3)
            self.assertEqual(writer.total, 3)
            self.assertEqual(writer.completed, 3)

    def test_interleaved_files_do_not_overwrite(self):
        """两个文件交错回传时，各自内容不得互相覆盖。"""
        with tempfile.TemporaryDirectory() as tmp:
            writer = FirmwareLogWriter(tmp)
            a = ctypes.create_string_buffer(b"AAAA", 4)
            b = ctypes.create_string_buffer(b"BBBB", 4)
            writer.packet(b"messages.log", PACKET_BEGIN, None, 0, 2, 0)
            writer.packet(b"observice.console.log", PACKET_BEGIN, None, 0, 2, 0)
            writer.packet(b"messages.log", PACKET_DATA, a, 4, 2, 0)
            writer.packet(b"observice.console.log", PACKET_DATA, b, 4, 2, 0)
            writer.packet(b"messages.log", PACKET_END, a, 0, 2, 1)
            writer.packet(b"observice.console.log", PACKET_END, b, 0, 2, 2)
            self.assertEqual(pathlib.Path(tmp, "messages.log").read_bytes(), b"AAAA")
            self.assertEqual(pathlib.Path(tmp, "observice.console.log").read_bytes(), b"BBBB")

    def test_data_without_begin_is_tolerated(self):
        """未收到 BEGIN 直接来 DATA 时按新文件容错，内容不丢。"""
        with tempfile.TemporaryDirectory() as tmp:
            writer = FirmwareLogWriter(tmp)
            buf = ctypes.create_string_buffer(b"tail", 4)
            writer.packet(b"messages.log", PACKET_DATA, buf, 4, 1, 1)
            writer.packet(b"messages.log", PACKET_END, buf, 0, 1, 1)
            self.assertEqual(pathlib.Path(tmp, "messages.log").read_bytes(), b"tail")

    def test_rejects_path_and_records_callback_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = FirmwareLogWriter(tmp)
            writer.packet(b"../escape.log", PACKET_BEGIN, None, 0, 1, 0)
            self.assertIsInstance(writer.error, OrbbecSdkError)
            self.assertFalse(os.path.exists(os.path.join(tmp, "escape.log")))

    def test_tracks_files_missing_end_packet(self):
        """开了包但没收到 PACKET_END 的文件要记下来，供收尾时报警。"""
        with tempfile.TemporaryDirectory() as tmp:
            writer = FirmwareLogWriter(tmp)
            done = ctypes.create_string_buffer(b"done", 4)
            cut = ctypes.create_string_buffer(b"cut", 3)
            writer.packet(b"ok.log", PACKET_BEGIN, None, 0, 2, 0)
            writer.packet(b"ok.log", PACKET_DATA, done, 4, 2, 0)
            writer.packet(b"ok.log", PACKET_END, done, 0, 2, 1)
            writer.packet(b"cut.log", PACKET_BEGIN, None, 0, 2, 1)
            writer.packet(b"cut.log", PACKET_DATA, cut, 3, 2, 1)
            self.assertEqual(writer.unfinished, {"cut.log"})
            writer.close()      # Windows 上不关句柄，临时目录删不掉
            w2 = FirmwareLogWriter(tmp)
            w2.packet(b"a.log", PACKET_BEGIN, None, 0, 1, 0)
            w2.packet(b"a.log", PACKET_END, None, 0, 1, 1)
            self.assertEqual(w2.unfinished, set())
            w2.close()

    def test_default_sdk_package_is_discoverable_when_present(self):
        path = find_sdk_dll()
        if path:
            self.assertTrue(path.lower().endswith("orbbecsdk.dll"))


class TestExportFirmwareLogsFinishConditions(unittest.TestCase):
    """收尾条件回归：EGO-4B（固件 1.1.1）传完大部分文件后不再回包，且上报的 total
    与实际文件数对不上，旧逻辑只认 completed>=total，会一直卡住（现场表现为进度停在
    84% 不结束）。这里用假的 SDK 绑定复现该场景。
    """

    def _run(self, packets, idle_s=1.0, timeout_s=20.0, incomplete=()):
        """用假绑定跑 export_firmware_logs：回调一次性吐完 packets，此后设备静默。"""
        from ego_relay import orbbec_sdk as sdk

        class _FakeBindings:
            def __init__(self, *_a, **_k):
                pass

            def call(self, fn, *args):
                return fn(*args)

            def close(self):
                pass

            def error_text(self, err):
                return "err"

        def _make_api():
            """返回一个名字 → 可调用对象的映射，模拟 SDK 的 ob_* 函数。"""
            def _str(*_a):
                return b""
            api = {
                "ob_create_context": lambda: 1,
                "ob_enable_net_device_enumeration": lambda *a: 0,
                "ob_query_device_list": lambda *a: 2,
                "ob_device_list_get_count": lambda *a: 1,
                "ob_device_list_get_device_ip_address": _str,
                "ob_device_list_get_device_name": lambda *a: b"EGO-4B",
                "ob_device_list_get_device_serial_number": lambda *a: b"AZLV961004Y",
                "ob_delete_context": lambda *a: 0,
                "ob_delete_device_list": lambda *a: 0,
                "ob_delete_device": lambda *a: 0,
                "ob_error_get_message": _str,
                "ob_delete_error": lambda *a: 0,
                "ob_device_stop_firmware_log_export": lambda *a: 0,
            }

            def _get_device(*_a):
                return 3

            def _start(_dev, cb, _user, *_rest):
                for (fname, payload, total, done) in packets:
                    buf = ctypes.create_string_buffer(payload, len(payload))
                    cb(fname, PACKET_BEGIN, None, 0, total, done - 1, None)
                    cb(fname, PACKET_DATA, buf, len(payload), total, done - 1, None)
                    if fname in incomplete:
                        continue      # 模拟传到一半就断包（无 PACKET_END）
                    cb(fname, PACKET_END, ctypes.byref(buf, len(payload)),
                       0, total, done, None)
                return 0

            api.update({
                "ob_device_list_get_device": _get_device,
                "ob_device_start_firmware_log_export": _start,
            })
            return api

        api = _make_api()
        notes = []
        orig_bind, orig_find = sdk._SdkBindings, sdk.find_sdk_dll
        sdk._SdkBindings = _FakeBindings
        sdk.find_sdk_dll = lambda *a, **k: "fake.dll"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                # 把 ob_* 注入到绑定的 __getattr__
                _FakeBindings.__getattr__ = lambda self, name: api[name]
                paths = sdk.export_firmware_logs(
                    tmp, firmware_idle_s=idle_s, timeout_s=timeout_s,
                    note_cb=notes.append)
                return paths, notes, tmp
        finally:
            sdk._SdkBindings, sdk.find_sdk_dll = orig_bind, orig_find

    def test_silent_device_finishes_without_total_match(self):
        """total=99 但只回了 2 个文件后静默：应在静默阈值后收尾，而不是等到超时。"""
        import time
        packets = [(b"orbbec_2026-10-13.log", b"AAAA", 99, 1),
                   (b"orbbec_2026-10-14.log", b"BBBB", 99, 2)]
        t0 = time.monotonic()
        paths, notes, tmp = self._run(packets, idle_s=0.5)
        elapsed = time.monotonic() - t0
        self.assertEqual(len(paths), 2)
        self.assertLess(elapsed, 10.0)
        self.assertTrue(any("静默超时收尾" in n for n in notes), notes)

    def test_complete_batch_finishes_immediately(self):
        """completed 追平 total 时正常结束，不当作异常。"""
        packets = [(b"a.log", b"AAA", 2, 1), (b"b.log", b"BBB", 2, 2)]
        paths, notes, _tmp = self._run(packets, idle_s=30.0)
        self.assertEqual(len(paths), 2)
        self.assertFalse(any("静默超时收尾" in n for n in notes), notes)
        self.assertFalse(any("不完整" in n for n in notes), notes)

    def test_warns_about_file_missing_end_packet(self):
        """文件传到一半断包（未收到 PACKET_END）时要明确报警，而不是当成正常结果。"""
        packets = [(b"ok.log", b"OK", 2, 1), (b"cut.log", b"CUT", 2, 2)]
        _paths, notes, _tmp = self._run(packets, idle_s=0.5, incomplete={b"cut.log"})
        warn = [n for n in notes if "不完整" in n]
        self.assertTrue(warn, notes)
        self.assertIn("cut.log", warn[0])
        self.assertNotIn("ok.log（", warn[0])

    def test_default_idle_threshold_is_30s(self):
        """大文件包间隔可达 20 秒以上，默认静默阈值必须是 30 秒而非 15 秒。"""
        import inspect
        from ego_relay import orbbec_sdk as sdk
        sig = inspect.signature(sdk.export_firmware_logs)
        self.assertEqual(sig.parameters["firmware_idle_s"].default, 30.0)


class TestMultiDeviceExport(unittest.TestCase):
    """多设备：全部导出、按设备名分目录、USB/网络重复枚举去重。"""

    def test_dedupe_prefers_usb_over_network(self):
        from ego_relay.orbbec_sdk import _dedupe_by_serial
        devs = [
            {"name": "EGO-4B", "serial": "AZLV961004Y", "connection": "Ethernet", "index": 3},
            {"name": "EGO-4B", "serial": "AZLV961004Y", "connection": "USB3.2", "index": 0},
            {"name": "UMI Finger", "serial": "OBC168DEFAULT", "connection": "USB2.0", "index": 1},
        ]
        out = _dedupe_by_serial(devs)
        self.assertEqual(len(out), 2)
        ego = [d for d in out if d["name"] == "EGO-4B"][0]
        self.assertIn("USB", ego["connection"])

    def test_safe_folder_strips_illegal_chars(self):
        from ego_relay.orbbec_sdk import _safe_folder
        self.assertEqual(_safe_folder('EGO:4B/车间?'), "EGO_4B_车间_")
        self.assertEqual(_safe_folder("  "), "device")

    def test_export_all_writes_one_folder_per_device(self):
        from ego_relay import orbbec_sdk as sdk

        devices = [
            {"name": "EGO-4B", "serial": "AZLV961004Y", "connection": "USB3.2", "index": 0},
            {"name": "UMI Finger", "serial": "OBC168DEFAULT", "connection": "USB2.0", "index": 1},
            {"name": "UMI Finger", "serial": "BF8M960008D", "connection": "USB2.0", "index": 2},
        ]
        calls = []

        def fake_single(out_dir, **kw):
            calls.append((out_dir, kw.get("device_index")))
            os.makedirs(out_dir, exist_ok=True)
            p = os.path.join(out_dir, "orbbec.log")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("x")
            return [p]

        orig_list, orig_single = sdk.list_devices, sdk.export_firmware_logs
        sdk.list_devices = lambda *a, **k: devices
        sdk.export_firmware_logs = fake_single
        try:
            with tempfile.TemporaryDirectory() as tmp:
                res = sdk.export_all_firmware_logs(tmp)
                folders = sorted(os.listdir(tmp))
                self.assertIn("EGO-4B", folders)
                # 两台同名 UMI 用序列号区分，互不覆盖
                self.assertIn("UMI Finger_OBC168DEFAULT", folders)
                self.assertIn("UMI Finger_BF8M960008D", folders)
                self.assertEqual(len(res["ok"]), 3)
                self.assertEqual(res["errors"], [])
                self.assertEqual(len(calls), 3)
        finally:
            sdk.list_devices, sdk.export_firmware_logs = orig_list, orig_single

    def test_one_device_failure_does_not_block_others(self):
        from ego_relay import orbbec_sdk as sdk

        devices = [
            {"name": "EGO-4B", "serial": "AZLV961004Y", "connection": "USB3.2", "index": 0},
            {"name": "UMI Finger", "serial": "OBC168DEFAULT", "connection": "USB2.0", "index": 1},
        ]

        def fake_single(out_dir, **kw):
            if kw.get("device_index") == 0:
                raise sdk.OrbbecSdkError("device busy")
            os.makedirs(out_dir, exist_ok=True)
            p = os.path.join(out_dir, "messages.log")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("x")
            return [p]

        orig_list, orig_single = sdk.list_devices, sdk.export_firmware_logs
        sdk.list_devices = lambda *a, **k: devices
        sdk.export_firmware_logs = fake_single
        try:
            with tempfile.TemporaryDirectory() as tmp:
                res = sdk.export_all_firmware_logs(tmp)
                self.assertEqual(len(res["ok"]), 1)
                self.assertEqual(len(res["errors"]), 1)
                self.assertIn("busy", res["errors"][0]["error"])
        finally:
            sdk.list_devices, sdk.export_firmware_logs = orig_list, orig_single


class TestUsbFirstSelection(unittest.TestCase):
    """USB 直连优先：局域网里被 GVCP 广播扫出来的其它设备默认不导。"""

    DEVICES = [
        {"name": "UMI", "serial": "OBC168DEFAULT", "connection": "USB2.0", "ip": "0.0.0.0", "index": 0},
        {"name": "UMI", "serial": "BF8M960008D", "connection": "USB2.0", "ip": "0.0.0.0", "index": 1},
        {"name": "EGOPro", "serial": "AZLV961004Y", "connection": "USB3.2", "ip": "0.0.0.0", "index": 2},
        {"name": "EGOPro", "serial": "AZLR86000FH", "connection": "Ethernet", "ip": "192.168.1.102", "index": 5},
        {"name": "EGOPro", "serial": "AZL9861004A", "connection": "Ethernet", "ip": "192.168.1.101", "index": 6},
    ]

    def test_only_usb_devices_selected_by_default(self):
        from ego_relay.orbbec_sdk import select_devices
        sel = select_devices(self.DEVICES)
        self.assertEqual([d["serial"] for d in sel],
                         ["OBC168DEFAULT", "BF8M960008D", "AZLV961004Y"])

    def test_network_devices_included_only_when_asked(self):
        from ego_relay.orbbec_sdk import select_devices
        sel = select_devices(self.DEVICES, include_network=True)
        self.assertEqual(len(sel), 5)

    def test_falls_back_to_network_when_no_usb(self):
        """一台 USB 都没有（只用网线连）时不能返回空，否则没法导出。"""
        from ego_relay.orbbec_sdk import select_devices
        only_net = [d for d in self.DEVICES if d["serial"] == "AZLR86000FH"]
        self.assertEqual(len(select_devices(only_net)), 1)

    def test_export_all_reports_skipped_network_devices(self):
        from ego_relay import orbbec_sdk as sdk

        notes = []
        orig_list, orig_single = sdk.list_devices, sdk.export_firmware_logs
        sdk.list_devices = lambda *a, **k: self.DEVICES
        sdk.export_firmware_logs = lambda out_dir, **kw: []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                res = sdk.export_all_firmware_logs(tmp, note_cb=notes.append)
                self.assertEqual(len(res["skipped"]), 2)
                # 提示要说明"为什么跳过"，否则用户只会看到设备少了两台
                self.assertTrue(any("跳过 2 台设备" in n and "非 USB 直连" in n
                                    for n in notes), notes)
        finally:
            sdk.list_devices, sdk.export_firmware_logs = orig_list, orig_single

    def test_prefer_ip_picks_only_that_device(self):
        """配网后设备只走网络链路：必须能按 IP 精确选中它，而不是被 USB 优先跳过。"""
        from ego_relay import orbbec_sdk as sdk

        pulled = []
        orig_list, orig_single = sdk.list_devices, sdk.export_firmware_logs
        sdk.list_devices = lambda *a, **k: self.DEVICES
        sdk.export_firmware_logs = lambda out_dir, **kw: (
            pulled.append((out_dir, kw.get("device_ip"))) or [])
        try:
            with tempfile.TemporaryDirectory() as tmp:
                res = sdk.export_all_firmware_logs(
                    tmp, prefer_ip="192.168.1.101")
                self.assertEqual(len(res["ok"]), 1, res)
                self.assertEqual(len(pulled), 1)
                self.assertEqual(pulled[0][1], "192.168.1.101")
                # 其余设备（含 USB 直连）都算跳过，并说明原因
                self.assertEqual(len(res["skipped"]), 4)
        finally:
            sdk.list_devices, sdk.export_firmware_logs = orig_list, orig_single

    def test_prefer_ip_falls_back_when_not_found(self):
        """IP 不在枚举列表里（例如刚断电）时回退到 USB 优先，不能整批为空。"""
        from ego_relay import orbbec_sdk as sdk

        notes = []
        orig_list, orig_single = sdk.list_devices, sdk.export_firmware_logs
        sdk.list_devices = lambda *a, **k: self.DEVICES
        sdk.export_firmware_logs = lambda out_dir, **kw: []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                res = sdk.export_all_firmware_logs(
                    tmp, prefer_ip="10.0.0.99", note_cb=notes.append)
                self.assertEqual(len(res["ok"]), 3, res)
                self.assertTrue(any("未找到 IP 10.0.0.99" in n for n in notes), notes)
        finally:
            sdk.list_devices, sdk.export_firmware_logs = orig_list, orig_single


class TestNoImplicitNetworkExport(unittest.TestCase):
    """填了「设备 IP」不等于"把局域网里所有设备都导一遍"。

    现场问题：局域网里有 7 台设备（3 台本机 USB + 4 台别人的 Ethernet），用户在
    「设备 IP」框里留着上一次配网的 IP 后导出，4 台别人的设备也被拉了下来。
    UI 侧因此固定传 include_network=False（见 device_tab._pull_firmware_from_device），
    这里断言的就是这条调用契约。
    """

    DEVICES = TestUsbFirstSelection.DEVICES

    def _pull(self, **kw):
        from ego_relay import orbbec_sdk as sdk

        pulled = []
        orig_list, orig_single = sdk.list_devices, sdk.export_firmware_logs
        sdk.list_devices = lambda *a, **k: self.DEVICES
        sdk.export_firmware_logs = lambda out_dir, **kw2: (
            pulled.append(kw2.get("device_ip")) or [])
        try:
            with tempfile.TemporaryDirectory() as tmp:
                res = sdk.export_all_firmware_logs(tmp, **kw)
        finally:
            sdk.list_devices, sdk.export_firmware_logs = orig_list, orig_single
        return res, pulled

    def test_empty_ip_keeps_only_usb(self):
        """不填 IP：只导本机 3 台 USB，局域网设备一律跳过。"""
        res, pulled = self._pull(include_network=False)
        self.assertEqual(len(res["ok"]), 3, res["ok"])
        # USB 设备枚举到的 IP 是 0.0.0.0，按 IP 匹配时不会误选
        self.assertEqual(pulled, ["0.0.0.0"] * 3)
        self.assertEqual(len(res["skipped"]), 2)

    def test_stale_ip_from_provisioning_does_not_open_network(self):
        """框里留着配网 IP、include_network=False：只拉那一台，其余网络设备仍跳过。"""
        res, pulled = self._pull(include_network=False, prefer_ip="192.168.1.101")
        self.assertEqual(len(res["ok"]), 1, res["ok"])
        self.assertEqual(pulled, ["192.168.1.101"])
        self.assertEqual(len(res["skipped"]), 4)

    def test_ui_never_requests_network_enumeration(self):
        """UI 的固件拉取路径必须固定 include_network=False。"""
        import inspect

        from ego_gui import device_tab

        src = inspect.getsource(device_tab._pull_firmware_from_device)
        self.assertIn("include_network=False", src)
        self.assertNotIn("include_network=bool(", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
