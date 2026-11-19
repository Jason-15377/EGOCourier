# -*- coding: utf-8 -*-
"""勾选「EGOViewer 手机日志」后默认拉起「固件设备」选择框。

手机导出只带手机上当时能导出的设备，指环/手表等从设备要靠 PC 端固件日志
补齐；现场要求勾选手机日志后直接打开这一行的「选择设备…」，不用再点一次。
"""
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class _FakeCheck:
    def __init__(self, checked):
        self._checked = checked

    def isChecked(self):
        return self._checked


class TestAutoOpenFirmwarePicker(unittest.TestCase):
    def test_only_opens_while_mobile_log_is_checked(self):
        from ego_gui.device_tab import DeviceTab

        class Fake:
            def __init__(self, checked):
                self.chk_mobile = _FakeCheck(checked)
                self.calls = []

            def _pick_firmware_devices(self, auto=False):
                self.calls.append(auto)

        on = Fake(True)
        DeviceTab._auto_open_fw_picker(on)
        self.assertEqual(on.calls, [True], "勾选手机日志后应自动打开设备选择框")

        off = Fake(False)
        DeviceTab._auto_open_fw_picker(off)
        self.assertEqual(off.calls, [], "未勾选时不能自动弹框")

    def test_checking_mobile_box_schedules_auto_open(self):
        """真实控件链路：toggled → _sync_mobile_enabled → 自动选择框。"""
        from PySide6.QtWidgets import QApplication

        from ego_gui import device_tab as dt

        app = QApplication.instance() or QApplication([])
        # 启动时不做全盘 EGOViewer 检测，测试只关心勾选后的动作
        with mock.patch.object(
                dt.evlog_pack, "find_ego_viewer_roots", return_value=[]):
            tab = dt.DeviceTab(lambda _m: None)
        try:
            calls = []
            tab._pick_firmware_devices = lambda auto=False: calls.append(auto)
            tab._scan_mobile = lambda: None
            tab.chk_mobile.setChecked(True)
            app.processEvents()
            self.assertEqual(calls, [True])

            tab.chk_mobile.setChecked(False)
            app.processEvents()
            self.assertEqual(calls, [True], "取消勾选不应再次弹框")
        finally:
            tab.deleteLater()


if __name__ == "__main__":
    unittest.main(verbosity=2)
