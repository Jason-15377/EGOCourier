# -*- coding: utf-8 -*-
"""BLE 配网 + SFTP 拉取相关单元测试。

- egolowble: DLL 定位 + 加载 + 导出符号齐全性
- ble_provision: 配网编排错误路径
- sftp_pull: 拉取产物自动导入（用本地构造的 tar.gz/目录）
"""
import os
import sys
import tempfile
import tarfile
import unittest

sys.path.insert(0, r"D:\EGOCourier")

from ego_relay import config, ble_provision  # noqa: E402
from ego_relay.egolowble import find_dll, EgoLowBle, BleError  # noqa: E402

EGOVIEWER = r"D:\Soft&tools\软件工具"
DLL_HINT = os.path.join(EGOVIEWER, "EgoLowBle.dll")


class TestFindDll(unittest.TestCase):
    def test_finds_egoviewer_dll(self):
        p = find_dll()
        if not p:
            self.skipTest("未安装 EGOViewer，跳过 DLL 定位测试")
        self.assertTrue(p.lower().endswith("egolowble.dll"), p)


class TestDllBind(unittest.TestCase):
    def test_load_and_symbols(self):
        p = find_dll()
        if not p:
            self.skipTest("未安装 EGOViewer，跳过 DLL 绑定测试")
        ble = EgoLowBle(p)
        for name in ("EgoLowBle_Create", "EgoLowBle_ScanDevices",
                     "EgoLowBle_ConnectByAddress", "EgoLowBle_ConfigureWifi",
                     "EgoLowBle_GetIpAddress", "EgoLowBle_IsConnected",
                     "EgoLowBle_GetLastError"):
            self.assertIn(name, ble._fn, f"缺少导出 {name}")

    def test_ssid_too_long(self):
        # 字节限制校验是纯逻辑，不依赖 DLL/真实设备
        from ego_relay.egolowble import check_wifi_bytes, BleError
        check_wifi_bytes("myssid", "mypwd")  # 合法，不抛
        with self.assertRaises(BleError):
            check_wifi_bytes("中" * 12, "")  # 36 字节 > 32
        with self.assertRaises(BleError):
            check_wifi_bytes("ok", "密" * 12)


class TestBleProvisionErrors(unittest.TestCase):
    def test_empty_ssid_raises(self):
        from ego_relay.egolowble import BleError as BE
        with self.assertRaises(BE):
            ble_provision.provision("AA:BB", "", "")


class TestSftpPullImport(unittest.TestCase):
    def _make_targz(self):
        tmp = tempfile.mkdtemp()
        root = os.path.join(tmp, "logs")
        os.makedirs(root)
        # 一个最小可解析的硬件 PTP CSV（frame_index,timestamp_us）
        with open(os.path.join(root, "L_sei.csv"), "w", encoding="utf-8") as f:
            f.write("frame_index,timestamp_us\n0,1000000\n1,2000000\n")
        arc = os.path.join(tmp, "logs.tar.gz")
        with tarfile.open(arc, "w:gz") as tf:
            tf.add(root, arcname="logs")
        return tmp, arc, root

    def test_import_targz(self):
        from ego_relay import sftp_pull
        tmp, arc, root = self._make_targz()
        try:
            r = sftp_pull.import_pulled(arc)
            self.assertTrue(r["session"]["id"])
            self.assertGreaterEqual(r["stats"].get("frames", 0), 0)
        finally:
            pass  # 临时目录保留便于排查

    def test_import_dir(self):
        from ego_relay import sftp_pull
        tmp, arc, root = self._make_targz()
        try:
            r = sftp_pull.import_pulled(root)
            self.assertTrue(r["session"]["id"])
        finally:
            pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
