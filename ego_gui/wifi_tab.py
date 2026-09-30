"""WiFi Tab：本机 WiFi 状态卡片 + 扫描列表（与 Web WiFi 页一致）。"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QTableWidget,
    QTableWidgetItem, QHeaderView, QLabel, QFrame,
)

from ego_relay import wifi as wifi_mod


class WifiScanWorker(QThread):
    result = Signal(list, dict)
    failed = Signal(str)

    def run(self):
        try:
            data = wifi_mod.scan_with_status()
            self.result.emit(data.get("networks", []), data.get("interface", {}))
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


class WifiTab(QWidget):
    def __init__(self, log):
        super().__init__()
        self.log = log
        self._worker: WifiScanWorker | None = None
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("WiFi 扫描（本机周边环境信息）"))
        top.addStretch(1)
        self.btn_scan = QPushButton("重新扫描")
        self.btn_scan.setProperty("role", "primary")
        self.btn_scan.clicked.connect(self.scan)
        top.addWidget(self.btn_scan)
        root.addLayout(top)

        # 状态卡片（当前连接）
        self.kpis = QHBoxLayout()
        self.kpi_ssid = self._card("当前 SSID")
        self.kpi_signal = self._card("信号")
        self.kpi_state = self._card("状态")
        root.addLayout(self.kpis)

        # WiFi 表
        self.wifi_table = QTableWidget(0, 4)
        self.wifi_table.setHorizontalHeaderLabels(["SSID", "信号", "安全类型", "状态"])
        self.wifi_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.wifi_table.setAlternatingRowColors(True)
        root.addWidget(self.wifi_table, stretch=1)

    def _card(self, label):
        card = QFrame(); card.setObjectName("statCard")
        v = QVBoxLayout(card); v.setContentsMargins(12, 8, 12, 8)
        val = QLabel("-"); val.setObjectName("statValue")
        cap = QLabel(label); cap.setObjectName("statLabel")
        v.addWidget(val); v.addWidget(cap)
        self.kpis.addWidget(card)
        return val

    def scan(self):
        self.log("扫描本机周边 WiFi…")
        self._worker = WifiScanWorker()
        self._worker.result.connect(self._on_result)
        self._worker.failed.connect(
            lambda e: (self.log(f"WiFi 扫描失败: {e}"), self.log("确保已开启无线网卡")))
        self._worker.start()

    def _on_result(self, networks, iface):
        self.kpi_ssid.setText(iface.get("ssid") or "-")
        self.kpi_signal.setText(f"{iface.get('signal', 0)}%")
        self.kpi_state.setText("已连接" if iface.get("connected") else "未连接")
        self.wifi_table.setRowCount(0)
        for n in networks:
            r = self.wifi_table.rowCount()
            self.wifi_table.insertRow(r)
            self.wifi_table.setItem(r, 0, QTableWidgetItem(n.get("ssid", "")))
            self.wifi_table.setItem(r, 1, QTableWidgetItem(f"{n.get('signal', 0)}%"))
            self.wifi_table.setItem(r, 2, QTableWidgetItem(n.get("security", "") or "-"))
            self.wifi_table.setItem(r, 3, QTableWidgetItem(
                "已连接" if n.get("connected") else ""))
        self.log(f"WiFi 扫描完成: 发现 {len(networks)} 个网络")
