"""BLE 扫描后台线程：设备与日志工作台复用（扫描期间不阻塞界面）。"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal


class BleScanWorker(QThread):
    result = Signal(list)
    failed = Signal(str)
    note = Signal(str)   # 扫描过程中的说明（如放宽条件重试）

    def run(self):
        try:
            from ego_relay import ble_provision
            if not ble_provision.dll_available():
                self.failed.emit(
                    "未找到 EgoLowBle.dll。便携版应自带 lib\\EgoLowBle.dll——"
                    "请确认解压完整（不是只拷了 exe），或安装 EGOViewer 后重试。")
                return
            self.result.emit(
                ble_provision.scan_devices_robust(status_cb=self.note.emit))
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))
