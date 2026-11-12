"""BLE 设备识别：真实产品名（EGO-4B_/UMI_Finger-R_/WristCam-R-R_…）都要认出来。

背景（现场问题「设备在线却扫不到名称，重启设备才出现」）：
- 老实现只比对裸前缀（EGO_/UMIFinger_/WristCam_…），而真实广播是
  EGO-4B_AZL9861004V（连字符 + 型号）、UMI_Finger-R_BF8…（带 -R/-L 方位），
  于是这些设备被过滤掉；
- 老实现还把「名称为空」的广播直接丢弃，而 BLE 广播包里 GAP 名称是可选项，
  刚上电 / 刚断开连接时头几个包往往只有 MAC，没有名称。
两处都会导致设备明明在线、却扫不到名字；重启设备后重新广播完整包才显示。

本测试同时覆盖 scan_devices_robust 的逐级放宽重试。
"""
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ego_relay import ble_provision  # noqa: E402


class TestEgoDeviceRecognition(unittest.TestCase):
    def test_real_product_names_recognized(self):
        cases = [
            ("EGO-4B_AZL9861004V", "54:14:FD:25:C8:62"),
            ("EGO_AZEC8640145", "54:14:FD:25:CB:9C"),
            ("EGO_AK8M96100P3", "54:14:FD:26:8A:D2"),
            ("UMI_Finger-R_BF8LA60006M", "54:14:FD:25:00:01"),
            ("UMI_Finger-L_BF7LA6000KF", "54:14:FD:25:00:02"),
            ("WristCam-R-R_BM1H864001G", "54:14:FD:25:EF:E7"),
            ("WristCam-L_BM2H8640066", "54:14:FD:25:EA:3F"),
            ("EGO-4B", "54:14:FD:25:C8:62"),
        ]
        for name, addr in cases:
            with self.subTest(name=name):
                self.assertTrue(
                    ble_provision.is_ego_ble_device({"name": name, "address": addr}))

    def test_unnamed_ego_broadcast_kept_by_oui(self):
        """名称为空但 OUI 属于 EGO：保留（否则设备在线也看不到）。"""
        self.assertTrue(ble_provision.is_ego_ble_device(
            {"name": "", "address": "54:14:FD:25:00:01"}))
        self.assertTrue(ble_provision.is_ego_ble_device(
            {"name": "N/A", "address": "54:14:fd:25:00:01"}))

    def test_unrelated_devices_excluded(self):
        """不是 EGO 的设备（鼠标/耳机/邻居电视）必须排除，避免列表被淹没。"""
        for name, addr in [
            ("LOCK_eab0", "C5:8D:A7:8C:EA:B0"),
            ("N/A", "6A:84:F5:8F:55:9C"),
            ("客厅的REDMI 电视", "B0:8C:B3:FE:EB:CF"),
            ("EGOBike", "AA:BB:CC:DD:EE:FF"),   # 前缀像但后面不是分隔符
        ]:
            with self.subTest(name=name):
                self.assertFalse(
                    ble_provision.is_ego_ble_device({"name": name, "address": addr}))

    def test_empty_address_always_excluded(self):
        self.assertFalse(ble_provision.is_ego_ble_device({"name": "EGO_XX", "address": ""}))


class TestScanRobust(unittest.TestCase):
    """scan_devices_robust：快出结果，只有一片空白时才补扫 / 放宽。"""

    def test_first_pass_hit_returns_immediately(self):
        """首轮有结果立即返回，不多扫——响应速度是第一诉求。"""
        calls = []

        def _fake(only_ego=True, rssi_floor=None):
            calls.append(rssi_floor)
            return [{"name": "EGO_X", "address": "54:14:FD:00:00:01", "rssi": -50}]

        with unittest.mock.patch.object(ble_provision, "scan_devices", _fake):
            got = ble_provision.scan_devices_robust()
        self.assertEqual(len(got), 1)
        self.assertEqual(len(calls), 1, "首轮有结果就只扫一轮")

    def test_falls_back_to_relaxed_rssi_then_no_filter(self):
        seq = iter([
            [],                          # 默认档第 1 轮为空
            [],                          # 补扫第 2 轮仍为空
            [{"name": "", "address": "54:14:FD:00:00:02", "rssi": -95}],  # 放宽下限命中
        ])

        def _fake(only_ego=True, rssi_floor=None):
            return next(seq, [])

        notes = []
        with unittest.mock.patch.object(ble_provision, "scan_devices", _fake):
            got = ble_provision.scan_devices_robust(status_cb=notes.append)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["address"], "54:14:FD:00:00:02")
        self.assertTrue(any("放宽" in n for n in notes), notes)

    def test_merges_name_seen_on_later_pass(self):
        """首轮一片空白、补扫时带名返回：最终结果保留名称。"""
        passes = iter([
            [],                                                     # 首轮空白
            [{"name": "EGO-4B_AZL", "address": "54:14:FD:00:00:03", "rssi": -60}],
        ])

        def _fake(only_ego=True, rssi_floor=None):
            return next(passes, [])

        with unittest.mock.patch.object(ble_provision, "scan_devices", _fake):
            got = ble_provision.scan_devices_robust()
        self.assertEqual(got[0]["name"], "EGO-4B_AZL")
        self.assertEqual(got[0]["rssi"], -60)


if __name__ == "__main__":
    unittest.main()
