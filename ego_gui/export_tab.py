"""日志导出 Tab：Wi-Fi 配网（BLE）+ SSH/SFTP 一键导出导入（与 Web 日志导出页一致）。

- Wi-Fi 配网（BLE）：加载 EGOViewer 的 EgoLowBle.dll，扫描蓝牙 → 选择设备 →
  填 SSID/密码 → 配置并等待设备加入局域网 → 自动回填设备 IP。
- 一键导出：用回填/手填 IP + SSH 凭证，通过 SFTP 拉取 app 日志与固件日志，
  拉完自动导入分析（会话记录在「会话记录」页查看）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QPushButton, QFileDialog,
    QTableWidget, QTableWidgetItem, QHeaderView, QLineEdit, QLabel, QGroupBox,
    QCheckBox, QComboBox, QAbstractItemView, QMessageBox,
)

from ego_relay import config as relay_config


class BleScanWorker(QThread):
    result = Signal(list)
    failed = Signal(str)

    def run(self):
        try:
            from ego_relay import ble_provision
            if not ble_provision.dll_available():
                self.failed.emit("未找到 EgoLowBle.dll，请先安装 EGOViewer")
                return
            self.result.emit(ble_provision.scan_devices())
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


class BleProvisionWorker(QThread):
    done = Signal(str)   # 设备 IP
    failed = Signal(str)

    def __init__(self, address, ssid, password):
        super().__init__()
        self.address, self.ssid, self.password = address, ssid, password

    def run(self):
        try:
            from ego_relay import ble_provision
            r = ble_provision.provision(self.address, self.ssid, self.password)
            self.done.emit(r.get("ip", ""))
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


class ExportWorker(QThread):
    progress = Signal(str, int, int)   # local_path, got, total
    done = Signal(list, dict)          # files, import_result
    failed = Signal(str)

    def __init__(self, host, user, pwd, dirs, local, zip_first, auto_import):
        super().__init__()
        self.host, self.user, self.pwd = host, user, pwd
        self.dirs, self.local = dirs, local
        self.zip_first, self.auto_import = zip_first, auto_import

    def _cb(self, local_path, got, total):
        self.progress.emit(local_path, got, total)

    def run(self):
        try:
            from ego_relay import sftp_pull
            files = sftp_pull.pull_logs(self.host, self.user, self.pwd,
                                        self.dirs, self.local,
                                        zip_first=self.zip_first,
                                        progress_cb=self._cb)
            imp = {}
            if self.auto_import and files:
                imp = sftp_pull.import_pulled(files[0])
            self.done.emit(files, imp)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


class ExportTab(QWidget):
    imported = Signal(int)   # 拉取并导入成功后发出 session_id

    def __init__(self, log):
        super().__init__()
        self.log = log
        self._ble_scan_worker: BleScanWorker | None = None
        self._ble_worker: BleProvisionWorker | None = None
        self._export_worker: ExportWorker | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # ---------- ① Wi-Fi 配网（BLE），紧凑竖排 ----------
        ble_grp = QGroupBox("① Wi-Fi 配网（BLE）")
        bg = QGridLayout(ble_grp)
        bg.setHorizontalSpacing(8)
        bg.setVerticalSpacing(6)
        bg.addWidget(QLabel("蓝牙设备"), 0, 0)
        self.cmb_ble = QComboBox()
        self.cmb_ble.setMinimumWidth(200)
        self.cmb_ble.setToolTip("扫描到的蓝牙设备（名称 + MAC）")
        self.btn_ble_scan = QPushButton("扫描蓝牙设备")
        h = QHBoxLayout(); h.addWidget(self.cmb_ble, 1); h.addWidget(self.btn_ble_scan)
        w = QWidget(); w.setLayout(h)
        bg.addWidget(w, 0, 1, 1, 2)
        bg.addWidget(QLabel("Wi-Fi 名称"), 1, 0)
        self.edit_ble_ssid = QLineEdit()
        self.edit_ble_ssid.setPlaceholderText("Wi-Fi 名称（≤32 字节）")
        bg.addWidget(self.edit_ble_ssid, 1, 1)
        self.edit_ble_pwd = QLineEdit()
        self.edit_ble_pwd.setEchoMode(QLineEdit.Password)
        self.edit_ble_pwd.setPlaceholderText("Wi-Fi 密码（开放网络留空）")
        bg.addWidget(self.edit_ble_pwd, 1, 2)
        self.btn_ble_apply = QPushButton("配置 Wi-Fi 并添加设备")
        self.btn_ble_apply.setProperty("role", "primary")
        self.btn_ble_apply.setEnabled(False)
        bg.addWidget(self.btn_ble_apply, 2, 1, 1, 2)
        bg.setColumnStretch(1, 1)
        root.addWidget(ble_grp)

        # ---------- ② SSH2+SFTP 连接配置（配网成功后自动回填 IP），在配网下方 ----------
        grp = QGroupBox("② SSH2+SFTP 连接配置（配网成功后自动回填 IP）")
        g = QGridLayout(grp)
        g.setHorizontalSpacing(8)
        g.setVerticalSpacing(6)
        self.edit_ip = QLineEdit()
        self.edit_ip.setPlaceholderText("设备 IP，如 192.168.1.118")
        if relay_config.DEVICE_IP:
            self.edit_ip.setText(relay_config.DEVICE_IP)
        elif relay_config.EXAMPLE_DEVICE_IP:
            self.edit_ip.setText(relay_config.EXAMPLE_DEVICE_IP)
        self.edit_ip.setToolTip("配网成功后自动回填；也可手动填 EGOViewer 设备详情/路由器/TTL ifconfig 的 IP")
        self.edit_user = QLineEdit()
        self.edit_user.setPlaceholderText("ssh 用户名")
        self.edit_pwd = QLineEdit()
        self.edit_pwd.setEchoMode(QLineEdit.Password)
        self.edit_pwd.setPlaceholderText("ssh 密码")
        self.edit_dirs = QLineEdit()
        self.edit_dirs.setText("/ego/logs")
        self.edit_dirs.setToolTip("远端日志目录，多个用 & 分隔，如 /ego/logs&/data/")
        self.edit_local = QLineEdit()
        btn_local = QPushButton("浏览…")
        btn_local.clicked.connect(self._pick_local)
        g.addWidget(QLabel("设备 IP"), 0, 0)
        g.addWidget(self.edit_ip, 0, 1)
        g.addWidget(QLabel("用户名"), 0, 2)
        g.addWidget(self.edit_user, 0, 3)
        g.addWidget(QLabel("密码"), 1, 0)
        g.addWidget(self.edit_pwd, 1, 1)
        g.addWidget(QLabel("远端日志目录"), 1, 2)
        g.addWidget(self.edit_dirs, 1, 3)
        g.addWidget(QLabel("本地保存目录"), 2, 0)
        hl = QHBoxLayout(); hl.addWidget(self.edit_local, 1); hl.addWidget(btn_local)
        lw = QWidget(); lw.setLayout(hl)
        g.addWidget(lw, 2, 1, 1, 3)
        self.chk_zip = QCheckBox("先压缩再导出")
        self.chk_zip.setChecked(True)
        self.chk_import = QCheckBox("拉取后自动导入分析")
        self.chk_import.setChecked(True)
        ch = QHBoxLayout()
        ch.addWidget(self.chk_zip); ch.addWidget(self.chk_import); ch.addStretch(1)
        cw = QWidget(); cw.setLayout(ch)
        g.addWidget(cw, 3, 1, 1, 3)
        g.setColumnStretch(1, 1)
        g.setColumnStretch(3, 1)
        root.addWidget(grp)

        # ---------- 按钮行 ----------
        btns = QHBoxLayout()
        self.btn_start = QPushButton("开始导出")
        self.btn_start.setProperty("role", "primary")
        self.btn_stop = QPushButton("停止任务")
        self.btn_stop.setProperty("role", "stop")
        self.btn_clear = QPushButton("清空日志")
        self.btn_clear.setProperty("role", "danger")
        self.btn_select_all = QPushButton("全选")
        self.btn_unselect = QPushButton("取消全选")
        for b in (self.btn_start, self.btn_stop, self.btn_clear,
                  self.btn_select_all, self.btn_unselect):
            btns.addWidget(b)
        btns.addStretch(1)
        root.addLayout(btns)

        # ---------- 任务结果表 ----------
        self.task_table = QTableWidget(0, 9)
        self.task_table.setHorizontalHeaderLabels([
            "WiFi名称", "状态", "下载进度", "下载速度", "开始时间",
            "结束时间", "保存路径", "操作", "错误信息"])
        self.task_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.task_table.setAlternatingRowColors(True)
        self.task_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        root.addWidget(self.task_table, stretch=1)

        # 信号
        self.btn_ble_scan.clicked.connect(self._scan_ble)
        self.btn_ble_apply.clicked.connect(self._apply_ble)
        self.btn_start.clicked.connect(self._start_export)
        self.btn_stop.clicked.connect(self._stop_task)
        self.btn_clear.clicked.connect(self._clear_tasks)
        self.btn_select_all.clicked.connect(lambda: self.task_table.selectAll())
        self.btn_unselect.clicked.connect(lambda: self.task_table.clearSelection())

    # ---------------- BLE 配网 ----------------
    def _scan_ble(self):
        self.btn_ble_scan.setEnabled(False)
        self.log("扫描蓝牙设备…（需电脑蓝牙已开启）")
        self._ble_scan_worker = BleScanWorker()
        self._ble_scan_worker.result.connect(self._on_ble_scan)
        self._ble_scan_worker.failed.connect(self._on_ble_fail)
        self._ble_scan_worker.finished.connect(
            lambda: self.btn_ble_scan.setEnabled(True))
        self._ble_scan_worker.start()

    def _on_ble_scan(self, devices):
        self.cmb_ble.clear()
        for d in devices:
            label = f"{d.get('name') or '(未命名)'}  {d.get('address') or ''}".strip()
            self.cmb_ble.addItem(label, d.get("address", ""))
        self.btn_ble_apply.setEnabled(self.cmb_ble.count() > 0)
        self.log(f"蓝牙扫描完成: 发现 {len(devices)} 台设备，请选择后填写 Wi-Fi 配置")

    def _on_ble_fail(self, msg):
        self.log(f"蓝牙扫描失败: {msg}")

    def _apply_ble(self):
        address = self.cmb_ble.currentData()
        ssid = self.edit_ble_ssid.text().strip()
        pwd = self.edit_ble_pwd.text()
        if not address:
            QMessageBox.warning(self, "提示", "请先扫描并选择蓝牙设备")
            return
        if not ssid:
            QMessageBox.warning(self, "提示", "Wi-Fi 名称不能为空")
            return
        self.btn_ble_apply.setEnabled(False)
        self.log(f"开始配网：设备 {address}，Wi-Fi {ssid}（等待设备加入局域网…）")
        self._ble_worker = BleProvisionWorker(address, ssid, pwd)
        self._ble_worker.done.connect(self._on_ble_done)
        self._ble_worker.failed.connect(self._on_ble_fail)
        self._ble_worker.finished.connect(lambda: self.btn_ble_apply.setEnabled(True))
        self._ble_worker.start()

    def _on_ble_done(self, ip):
        if ip:
            self.edit_ip.setText(ip)
            self.log(f"配网成功：设备 IP = {ip}，已自动填入 SSH 配置")
        else:
            self.log("配网已下发，但未在超时内拿到设备 IP；请到设备信息页确认是否已联网")

    # ---------------- SSH 一键导出 ----------------
    def _pick_local(self):
        d = QFileDialog.getExistingDirectory(self, "选择本地保存目录")
        if d:
            self.edit_local.setText(d)

    def _start_export(self):
        if self._export_worker and self._export_worker.isRunning():
            QMessageBox.information(self, "提示", "已有导出任务进行中")
            return
        host = self.edit_ip.text().strip()
        user = self.edit_user.text().strip()
        pwd = self.edit_pwd.text()
        local = self.edit_local.text().strip() or str(relay_config.EXPORT_DIR)
        dirs = self.edit_dirs.text().strip() or "/ego/logs"
        if not host:
            QMessageBox.warning(self, "提示", "请先通过 BLE 配网拿到设备 IP，或手动填写")
            return
        if not user or not pwd:
            QMessageBox.warning(self, "提示", "请填写设备 SSH 用户名和密码")
            return

        self._export_worker = ExportWorker(
            host, user, pwd, dirs, local,
            self.chk_zip.isChecked(), self.chk_import.isChecked())
        r = self._add_task_row("-", "下载中", 0, "", self._now(), "", local, "…", "")
        self._export_worker.progress.connect(
            lambda path, got, total: self._update_progress(r, path, got, total))
        self._export_worker.done.connect(lambda files, imp: self._on_export_done(r, files, imp))
        self._export_worker.failed.connect(lambda err: self._on_export_fail(r, err))
        self._export_worker.start()
        self.log(f"开始导出：从 {host} 拉取 {dirs} → {local}")

    def _stop_task(self):
        if self._export_worker and self._export_worker.isRunning():
            self._export_worker.terminate()
            self.log("已停止导出任务")
        else:
            self.log("当前没有运行中的导出任务")

    def _clear_tasks(self):
        self.task_table.setRowCount(0)
        self.log("已清空任务结果表")

    def _on_export_done(self, row, files, imp):
        self._set_task(row, "完成", 100, "", self._now(),
                       (imp.get("session") or {}).get("name", ""), "")
        sid = (imp.get("session") or {}).get("id")
        if sid:
            self.log(f"拉取并导入成功: 会话 #{sid}，共 {len(files)} 个文件")
            self.imported.emit(sid)
        else:
            self.log(f"拉取完成（{len(files)} 个文件），未自动导入")

    def _on_export_fail(self, row, err):
        self._set_task(row, "失败", "", "", "", "", err)
        self.log(f"导出失败: {err}")

    @staticmethod
    def _now():
        from datetime import datetime
        return datetime.now().strftime("%H:%M:%S")

    def _add_task_row(self, wifi, status, prog, speed, t0, t1, save, op, err):
        r = self.task_table.rowCount()
        self.task_table.insertRow(r)
        vals = [wifi, status, f"{prog}%", speed, t0, t1, save, op, err]
        for c, v in enumerate(vals):
            self.task_table.setItem(r, c, QTableWidgetItem(str(v)))
        return r

    def _set_task(self, row, status, prog, speed, t1, save, err):
        for c, v in enumerate([None, status, f"{prog}%", speed, None, t1, save, "", err]):
            if v is None:
                continue
            it = QTableWidgetItem(str(v))
            self.task_table.setItem(row, c, it)

    def _update_progress(self, row, path, got, total):
        pct = int(got * 100 / total) if total else 0
        self.task_table.setItem(row, 2, QTableWidgetItem(f"{pct}%"))
        self.task_table.setItem(row, 3, QTableWidgetItem(f"{got}/{total} B"))
