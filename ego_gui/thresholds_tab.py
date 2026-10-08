"""阈值 Tab：调整异常阈值（μs）+ 保存（与 Web 阈值页一致）。

保存后发出 saved(vals)，主窗口据此对当前打开的会话详情即时重算。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QFormLayout, QSpinBox, QPushButton, QLabel,
)

from ego_relay import config


class ThresholdsTab(QWidget):
    saved = Signal(dict)   # 保存后的阈值 {key: us}

    def __init__(self, log):
        super().__init__()
        self.log = log
        self.spins = {}
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.addWidget(QLabel("异常阈值（单位：微秒 μs）。超过阈值即标记异常，可随时调整后保存。"))
        form = QFormLayout()
        cur = dict(config.DEFAULT_THRESHOLDS_US)
        try:
            cur.update(config.load_thresholds())
        except Exception:
            pass
        labels = {
            "app_receive_delay": "app_receive_delay (应用接收延迟)",
            "sei_copy_error": "sei_copy_error (SEI 拷贝错误)",
            "binocular_ptp_diff": "binocular_ptp_diff (双目 PTP 同步差)",
            "imu_video_sync": "imu_video_sync (IMU-视频同步差)",
        }
        for k in ("app_receive_delay", "sei_copy_error", "binocular_ptp_diff", "imu_video_sync"):
            sp = QSpinBox()
            sp.setRange(0, 10_000_000)
            sp.setValue(cur.get(k, 0))
            sp.setSingleStep(100)
            self.spins[k] = sp
            form.addRow(labels.get(k, k), sp)
        root.addLayout(form)
        self.btn_save = QPushButton("保存阈值")
        self.btn_save.setProperty("role", "primary")
        self.btn_save.clicked.connect(self._save)
        root.addWidget(self.btn_save)
        self.lbl_msg = QLabel("")
        root.addWidget(self.lbl_msg)
        root.addStretch(1)

    def _save(self):
        vals = {k: sp.value() for k, sp in self.spins.items()}
        try:
            config.save_thresholds(vals)
        except Exception as e:  # noqa: BLE001
            self.lbl_msg.setText(f"保存失败: {e}")
            return
        self.log(f"阈值已保存: {vals}")
        self.lbl_msg.setText("已保存（若已打开会话将即时重算）")
        self.saved.emit(vals)
