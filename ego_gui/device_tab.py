"""设备与日志工作台。

① 蓝牙配网 + ② 单一合并分组「设备实时拉取 · 本地打包」：
- SSH 设备实时拉取 app 日志（SSH/SFTP，勾选「app 日志」后填写 SSH 账户）
- EGOViewer 三类日志（app/sdk/firmware）一键导出 / 打包
底部「开始一键导出」：识别 EGOViewer 根目录 → 全新时间戳目录收集勾选分类，
勾选 firmware 时通过 Orbbec SDK 从设备实时拉取最新固件日志（等价 EGOviewer
「导出设备日志」）→ 导入分析 + 报告；「打包导出」：按筛选/手动勾选打包成 zip。
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime

from PySide6.QtCore import Qt, QThread, Signal, QSettings, QPoint, QRect, QEvent, QDateTime, QTime
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QPushButton, QFileDialog,
    QLineEdit, QLabel, QGroupBox,
    QCheckBox, QMessageBox, QProgressBar, QPlainTextEdit,
    QComboBox, QFrame,
    QScrollArea, QListWidget, QListWidgetItem, QToolButton,
    QDateTimeEdit, QDialog, QDialogButtonBox,
    QTreeWidget, QTreeWidgetItem, QAbstractItemView,
)

from .export_tab import BleScanWorker  # 复用：BLE 扫描后台线程
from ego_relay import config as relay_config
from ego_relay import evlog_pack  # 本地 EGOViewer 三类日志收集与打包


# ---------------------------------------------------------------------------
# 本机 WiFi 扫描后台线程（配网时用来挑选/核对电脑所在网络）
# ---------------------------------------------------------------------------
class WifiScanWorker(QThread):
    result = Signal(dict)    # scan_with_status() 结果
    failed = Signal(str)

    def run(self):
        try:
            from ego_relay import wifi
            self.result.emit(wifi.scan_with_status())
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


# ---------------------------------------------------------------------------
# 配网后台线程（逐步上报状态，供"配网状态"标签展示）
# ---------------------------------------------------------------------------
class DeviceBleProvisionWorker(QThread):
    status = Signal(str)     # 逐步状态
    done = Signal(str)       # 设备 IP（可能为空串 = 未在超时内拿到）
    failed = Signal(str)

    def __init__(self, address, ssid, password):
        super().__init__()
        self.address, self.ssid, self.password = address, ssid, password

    def run(self):
        try:
            from ego_relay import config, egolowble
            join_timeout = config.BLE_NETWORK_JOIN_TIMEOUT_S
            with egolowble.EgoLowBle() as ble:
                self.status.emit(f"连接设备 {self.address} …")
                ble.connect(self.address)
                self.status.emit("已建立连接，校验中…")
                if not ble.is_connected():
                    raise egolowble.BleError("连接后 is_connected 返回假，配网前置条件不满足")
                self.status.emit("下发 Wi-Fi 配置…")
                ble.configure_wifi(self.ssid, self.password)
                self.status.emit("已下发，等待设备加入局域网并上报 IP…")
                deadline = time.time() + join_timeout
                ip = ""
                while time.time() < deadline:
                    try:
                        ip = ble.get_ip()
                    except egolowble.BleError:
                        ip = ""
                    if ip and ip not in ("0.0.0.0", "255.255.255.255"):
                        self.status.emit(f"设备已联网，IP = {ip}")
                        self.done.emit(ip)
                        return
                    time.sleep(2.0)
                self.status.emit("等待超时，未拿到设备 IP")
                self.done.emit("")
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


# ---------------------------------------------------------------------------
# 远端一键导出后台线程：SFTP 拉设备日志 + 自动导入分析 + 自动出报告
# ---------------------------------------------------------------------------
class DeviceExportWorker(QThread):
    progress = Signal(int)     # 0..100
    status = Signal(str)       # 文本行（写入底部日志框）
    done = Signal(int, str)    # session_id(可能 None), report_path(可能 "")
    failed = Signal(str)

    def __init__(self, host, user, pwd, remote_dirs, local,
                 zip_first, auto_import, auto_report):
        super().__init__()
        self.host, self.user, self.pwd = host, user, pwd
        self.remote_dirs = remote_dirs
        self.local = local
        self.zip_first = zip_first
        self.auto_import = auto_import
        self.auto_report = auto_report
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def _cb(self, local_path, got, total):
        if self.cancel_event.is_set():
            raise RuntimeError("导出已取消")
        pct = int(got * 50 / total) if total else 0
        self.progress.emit(50 + pct)
        self.status.emit(f"下载 {os.path.basename(local_path)}  {got}/{total} B ({pct}%)")

    def run(self):
        try:
            from ego_relay import sftp_pull, export as export_mod, db
            run_dir = os.path.join(
                self.local, "device_export_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
            os.makedirs(run_dir, exist_ok=True)
            if self.cancel_event.is_set():
                raise RuntimeError("导出已取消")
            files = []
            if self.remote_dirs:
                self.status.emit(f"连接 {self.user}@{self.host} 拉取远端日志…")
                files = sftp_pull.pull_logs(self.host, self.user, self.pwd,
                                            self.remote_dirs, run_dir,
                                            zip_first=self.zip_first,
                                            progress_cb=self._cb)
                self.status.emit(f"远端日志拉取完成：{len(files)} 个文件")
            sid, report_path = None, ""
            if self.auto_import and files:
                self.status.emit("导入 EGO Courier 并对齐分析中…")
                single_archive = (
                    len(files) == 1 and os.path.isfile(files[0])
                    and files[0].lower().endswith((".tar.gz", ".tgz", ".zip"))
                )
                target = files[0] if single_archive else run_dir
                imp = sftp_pull.import_pulled(target)
                sid = (imp.get("session") or {}).get("id")
                if sid:
                    self.status.emit(f"导入完成：会话 #{sid}（对齐 + 四类异常检测）")
                    if self.auto_report:
                        self.status.emit("自动生成 HTML 报告…")
                        s = db.get_session(sid)
                        stats = db.session_stats(sid)
                        html = export_mod._html_report(sid, s, stats)
                        name = (s or {}).get("name") or f"session_{sid}"
                        report_path = os.path.join(
                            run_dir, f"report_{name}_{sid}.html")
                        os.makedirs(self.local, exist_ok=True)
                        with open(report_path, "w", encoding="utf-8") as fh:
                            fh.write(html)
                        self.status.emit(f"报告已生成：{report_path}")
                else:
                    self.status.emit("导入未产生会话，可能目录内无有效日志")
            self.progress.emit(100)
            self.done.emit(sid, report_path)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


# ---------------------------------------------------------------------------
# 本地 EGOViewer 三类日志打包后台线程：收集本机日志 → 日期时间命名 zip
# ---------------------------------------------------------------------------
class EvLogPackWorker(QThread):
    progress = Signal(int)              # 0..100
    status = Signal(str)                # 文本行（写入底部日志框）
    done = Signal(str, object, str)     # zip 绝对路径, session_id(可能 None), report_path(可能 "")
    failed = Signal(str)

    def __init__(self, root, out_dir, keys, stage_kwargs=None, auto_import=False,
                 auto_report=False, prefix="EGOViewer_logs"):
        super().__init__()
        self.root, self.out_dir, self.keys = root, out_dir, keys
        self.stage_kwargs = dict(stage_kwargs or {})
        self.auto_import = auto_import
        self.auto_report = auto_report
        self.prefix = prefix

    def run(self):
        import shutil
        import tempfile
        work = tempfile.mkdtemp(prefix="evlog_")
        try:
            self.status.emit("收集并筛选日志…")
            _staged, collected = evlog_pack.stage(
                self.root, work, self.keys,
                progress_cb=lambda pct: self.progress.emit(pct),
                **self.stage_kwargs)
            n = sum(len(v) for v in collected.values())
            if n == 0:
                raise RuntimeError("按当前筛选未找到任何日志，请调整筛选范围")
            self.status.emit(f"共收集 {n} 个日志文件，开始压缩…")
            zip_path = evlog_pack.pack_staged(
                work, self.out_dir, self.root, collected, prefix=self.prefix,
                max_age_days=self.stage_kwargs.get("max_age_days"),
                progress_cb=lambda pct: self.progress.emit(pct))
            self.status.emit(f"打包完成：{zip_path}")

            sid, report_path = None, ""
            has_analysis = bool(collected.get("app") or collected.get("firmware"))
            if self.auto_import and has_analysis:
                self.status.emit("导入 EGO Courier 并对齐分析中…")
                from ego_relay import sftp_pull, export as export_mod, db
                imp = sftp_pull.import_pulled(work)
                sid = (imp.get("session") or {}).get("id")
                if sid:
                    self.status.emit(f"导入完成：会话 #{sid}（对齐 + 四类异常检测）")
                    if self.auto_report:
                        self.status.emit("自动生成 HTML 报告…")
                        s = db.get_session(sid)
                        stats = db.session_stats(sid)
                        html = export_mod._html_report(sid, s, stats)
                        name = (s or {}).get("name") or f"session_{sid}"
                        report_path = os.path.join(
                            self.out_dir, f"report_{name}_{sid}.html")
                        with open(report_path, "w", encoding="utf-8") as fh:
                            fh.write(html)
                        self.status.emit(f"报告已生成：{report_path}")
                else:
                    self.status.emit("导入未产生会话，可能所选日志不含可对齐的 app/固件日志")
            elif self.auto_import and not has_analysis:
                self.status.emit("所选日志不含可分析来源（app/固件），跳过导入")
            self.progress.emit(100)
            self.done.emit(zip_path, sid, report_path)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))
        finally:
            shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# 一键导出后台线程：识别 EGOViewer 根目录 → 全新时间戳目录收集所选分类日志 → 导入分析 + 报告
#   每次都在输出目录下新建 EGO_export_<时间戳>/，从源目录现拉最新日志，不复用本地旧日志。
#   勾选 firmware 时通过 Orbbec SDK 从设备实时拉取最新固件日志（等价 EGOviewer「导出设备日志」）。
# ---------------------------------------------------------------------------
class EgoOneClickExportWorker(QThread):
    progress = Signal(int)              # 0..100
    status = Signal(str)                # 文本行（写入底部日志框）
    done = Signal(str, object, str)     # export_dir, session_id(可能 None), report_path(可能 "")
    failed = Signal(str)

    def __init__(self, root, out_dir, keys, auto_import=False, auto_report=False):
        super().__init__()
        self.root, self.out_dir, self.keys = root, out_dir, keys
        self.auto_import = auto_import
        self.auto_report = auto_report

    def run(self):
        from ego_relay import orbbec_sdk, sftp_pull, export as export_mod, db, config
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        export_dir = os.path.join(self.out_dir, f"EGO_export_{ts}")
        try:
            self.status.emit(f"识别 EGOViewer 根目录：{self.root}")
            os.makedirs(export_dir, exist_ok=True)
            collected = {}
            # app/sdk：从本机 EGOViewer 根目录收集（进度 0–50）
            app_sdk = [k for k in self.keys if k != "firmware"]
            if app_sdk:
                self.status.emit("收集 app/SDK 日志…")
                _staged, c = evlog_pack.stage(
                    self.root, export_dir, app_sdk,
                    progress_cb=lambda pct: self.progress.emit(int(pct * 0.5)))
                collected.update(c)
            # firmware：像 EGOviewer「导出设备日志」一样，通过 Orbbec SDK 从设备实时拉取最新固件日志
            if "firmware" in self.keys:
                self.status.emit("通过 Orbbec SDK 从设备实时拉取固件日志…（保持设备连接）")
                fw_dir = os.path.join(export_dir, "firmware")
                try:
                    fw_paths = orbbec_sdk.export_firmware_logs(
                        fw_dir, sdk_root=self.root, device_ip="",
                        device_port=config.ORBBEC_SDK_PORT,
                        progress_cb=lambda pct, name: self.progress.emit(50 + int(pct * 0.5)))
                except orbbec_sdk.OrbbecSdkError as e:
                    raise RuntimeError(
                        f"固件日志导出失败，请确认设备已连接、EGOviewer 未占用设备：{e}") from e
                collected["firmware"] = [
                    {"abs": p, "rel": os.path.basename(p)} for p in fw_paths]
            n = sum(len(v) for v in collected.values())
            if n == 0:
                raise RuntimeError("按所选分类未找到任何日志，请检查 EGOViewer 安装目录")
            self.status.emit(f"已导出 {n} 个日志文件到：{export_dir}")

            sid, report_path = None, ""
            has_analysis = bool(collected.get("app") or collected.get("firmware"))
            if self.auto_import and has_analysis:
                self.status.emit("导入 EGO Courier 并对齐分析中…")
                imp = sftp_pull.import_pulled(export_dir)
                sid = (imp.get("session") or {}).get("id")
                if sid:
                    self.status.emit(f"导入完成：会话 #{sid}（对齐 + 四类异常检测）")
                    if self.auto_report:
                        self.status.emit("自动生成 HTML 报告…")
                        s = db.get_session(sid)
                        stats = db.session_stats(sid)
                        html = export_mod._html_report(sid, s, stats)
                        name = (s or {}).get("name") or f"session_{sid}"
                        report_path = os.path.join(
                            export_dir, f"report_{name}_{sid}.html")
                        with open(report_path, "w", encoding="utf-8") as fh:
                            fh.write(html)
                        self.status.emit(f"报告已生成：{report_path}")
                else:
                    self.status.emit("导入未产生会话，可能所选日志不含可对齐的 app/固件日志")
            elif self.auto_import and not has_analysis:
                self.status.emit("所选日志不含可分析来源（app/固件），跳过导入")
            self.progress.emit(100)
            self.done.emit(export_dir, sid, report_path)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


# ---------------------------------------------------------------------------
# 悬浮下拉选择器：带本地搜索的浮层列表（非 table/固定列表）
#   收起时只显示单行已选内容；点箭头弹出浮层，浮层覆盖在界面上层，
#   顶部搜索框本地过滤，选中条目高亮，弹窗带边框，扫描后可动态刷新。
# ---------------------------------------------------------------------------
class SearchableDeviceCombo(QWidget):
    selectedDevice = Signal(object)   # dict {name,address}，未选为 None
    popupOpened = Signal()            # 下拉展开时发出，供外部触发自动扫描

    def __init__(self, parent=None):
        super().__init__(parent)
        self._devices = []
        self._selected = None
        self._popup = None
        self._popup_search = None
        self._popup_list = None
        self._build_display()

    # ------------------------------------------------------ 收起态（单行显示）
    def _build_display(self):
        # 外层框统一为表单控件样式（白底/浅灰边框/圆角），内部文字与箭头透明无边框。
        # 必须设置 WA_StyledBackground，否则 QWidget 背景 QSS 不生效、整框透明。
        self.setObjectName("searchComboBox")
        self.setAttribute(Qt.WA_StyledBackground, True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(7, 0, 2, 0)
        lay.setSpacing(0)
        self._display = QLineEdit()
        self._display.setReadOnly(True)
        self._display.setPlaceholderText("选择蓝牙设备…")
        self._display.setObjectName("searchComboDisplay")
        self._arrow = QToolButton()
        self._arrow.setText("▾")
        self._arrow.setFixedWidth(22)
        self._arrow.setToolTip("展开设备列表")
        self._arrow.setCursor(Qt.PointingHandCursor)
        self._arrow.setFocusPolicy(Qt.NoFocus)
        self._arrow.setObjectName("searchComboArrow")
        self._arrow.clicked.connect(self._toggle_popup)
        lay.addWidget(self._display, 1)
        lay.addWidget(self._arrow)
        # 点击框体任意位置（文字区）即可展开，不限于右侧箭头
        self.installEventFilter(self)
        self._display.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.MouseButtonRelease and obj in (self, self._display):
            self._toggle_popup()
            return True
        return super().eventFilter(obj, event)

    # ------------------------------------------------------ 浮层
    def _build_popup(self):
        self._popup = QFrame(None, Qt.Popup | Qt.FramelessWindowHint)
        self._popup.setObjectName("devicePopup")
        v = QVBoxLayout(self._popup)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(4)
        self._popup_search = QLineEdit()
        self._popup_search.setPlaceholderText("搜索设备名称 / MAC…")
        self._popup_search.textChanged.connect(self._filter_devices)
        self._popup_list = QListWidget()
        self._popup_list.setObjectName("devicePopupList")
        self._popup_list.setFocusPolicy(Qt.NoFocus)
        self._popup_list.itemClicked.connect(self._on_pick)
        v.addWidget(self._popup_search)
        v.addWidget(self._popup_list, 1)

    def _toggle_popup(self):
        if self._popup and self._popup.isVisible():
            self._popup.close()
        else:
            self._open_popup()
            self.popupOpened.emit()   # 通知外部：下拉已展开，可自动触发扫描

    def _open_popup(self):
        if not self._popup:
            self._build_popup()
        if not self._devices:
            # 尚未扫描到设备：先显示扫描提示，等待自动扫描结果回填
            self._popup_list.clear()
            it = QListWidgetItem("正在扫描蓝牙设备…")
            it.setFlags(Qt.NoItemFlags)
            self._popup_list.addItem(it)
            self._popup_search.setText("")
        else:
            self._filter_devices(self._popup_search.text() if self._popup_search else "")
        pos = self.mapToGlobal(QPoint(0, self.height() + 2))
        width = max(self.width(), 300)
        n = max(self._popup_list.count(), 1)
        height = 40 + min(n, 8) * 32
        self._popup.setGeometry(QRect(pos.x(), pos.y(), width, height))
        self._popup.show()
        self._popup.raise_()
        if self._popup_search:
            self._popup_search.setFocus()
            self._popup_search.selectAll()

    # ------------------------------------------------------ 数据
    def set_devices(self, devices):
        """扫描结果到达后刷新内部列表；浮层打开时即时刷新显示。"""
        self._devices = list(devices or [])
        if self._popup and self._popup.isVisible():
            self._filter_devices(self._popup_search.text())

    def set_selected(self, dev):
        self._selected = dict(dev) if dev else None
        if self._selected:
            name = self._selected.get("name") or "(未命名)"
            addr = self._selected.get("address") or ""
            self._display.setText(f"{name}   {addr}")
        else:
            self._display.setText("")
        self.selectedDevice.emit(self._selected)

    def selected_device(self):
        return self._selected

    # ------------------------------------------------------ 本地搜索 / 选中
    def _filter_devices(self, text):
        if not self._popup_list:
            return
        self._popup_list.clear()
        text = (text or "").strip().lower()
        cur_addr = (self._selected or {}).get("address")
        for d in self._devices:
            name = d.get("name") or "(未命名)"
            addr = d.get("address") or ""
            if text and text not in f"{name} {addr}".lower():
                continue
            it = QListWidgetItem(f"{name}   {addr}")
            it.setData(Qt.UserRole, d)
            it.setToolTip(addr)
            self._popup_list.addItem(it)
            if addr == cur_addr:
                self._popup_list.setCurrentItem(it)
        if self._popup_list.count() == 0:
            it = QListWidgetItem("未发现匹配设备")
            it.setFlags(Qt.NoItemFlags)
            self._popup_list.addItem(it)

    def _on_pick(self, item):
        dev = item.data(Qt.UserRole)
        if not dev:
            return
        self.set_selected(dev)
        if self._popup:
            self._popup.close()


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
class DeviceTab(QWidget):
    imported = Signal(int)   # 拉取并导入成功后发出 session_id（供会话记录页刷新）

    def __init__(self, log, log_box=None):
        super().__init__()
        self.log = log
        self.log_box = log_box
        self._scan_worker: BleScanWorker | None = None
        self._wifi_scan_worker: WifiScanWorker | None = None
        self._provision_worker: DeviceBleProvisionWorker | None = None
        self._dp_worker: DeviceExportWorker | None = None  # 远端一键导出
        self._export_worker: EgoOneClickExportWorker | None = None  # EGOviewer 一键导出（SDK 拉固件）
        self._pack_worker: EvLogPackWorker | None = None  # 本地勾选打包
        self._manual_selection: dict | None = None  # 手动勾选：{分类key: [绝对路径,...]}
        self._devices = []            # [{name,address}]
        self._selected_address = ""
        self._settings = QSettings("EGO", "Relay")
        self._build_ui()
        self._rescan_ev_roots()

    # ------------------------------------------------------------- 界面构建
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 6)
        root.setSpacing(8)

        # 整页套滚动区：空间不足时滚动而不是把分组行高压成 0。
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(10)
        bl.addWidget(self._build_ble_group())
        bl.addWidget(self._build_merge_group())
        bl.addStretch(1)
        scroll.setWidget(body)
        root.addWidget(scroll, stretch=1)

    # ------------------------------------------------------------- ① 蓝牙配网
    def _build_ble_group(self):
        """① 蓝牙配网：悬浮下拉选设备，下发 Wi-Fi 接入局域网。"""
        ble = QGroupBox("① 蓝牙配网 · 发现设备并接入局域网")
        g = QGridLayout(ble)
        g.setHorizontalSpacing(8)
        g.setVerticalSpacing(8)

        # 行0：扫描按钮（左）与 Wi-Fi 名称标签同一列对齐；设备下拉与 Wi-Fi 下拉同一列
        self.btn_scan = QPushButton("扫描 BLE 设备")
        self.btn_scan.setObjectName("scanBtn")
        self.btn_scan.setFixedHeight(32)
        self.btn_scan.clicked.connect(self._scan)
        g.addWidget(self.btn_scan, 0, 0)
        self.dev_combo = SearchableDeviceCombo()
        self.dev_combo.setFixedHeight(32)
        self.dev_combo.selectedDevice.connect(self._on_dev_pick)
        self.dev_combo.popupOpened.connect(self._auto_scan_on_popup)
        # 向右拉长：跨第 1~4 列，右边缘与下方「Wi-Fi 密码」输入框对齐
        g.addWidget(self.dev_combo, 0, 1, 1, 4)
        self.lbl_ble_state = QLabel("空闲")
        self.lbl_ble_state.setStyleSheet("color:#6b7280;")
        g.addWidget(self.lbl_ble_state, 0, 5, Qt.AlignRight | Qt.AlignVCenter)

        # 行1：Wi-Fi 名称 + Wi-Fi 下拉 + 搜索 + Wi-Fi 密码 + 密码输入
        g.addWidget(self._field_label("Wi-Fi 名称"), 1, 0)
        self.cmb_wifi = QComboBox()
        self.cmb_wifi.setMinimumWidth(200)
        self.cmb_wifi.setFixedHeight(32)
        self.cmb_wifi.setToolTip("电脑可见的 Wi-Fi（电脑当前连接的会标注）。可直接选，也可点「搜索 WiFi」刷新")
        self.cmb_wifi.currentIndexChanged.connect(self._on_wifi_pick)
        g.addWidget(self.cmb_wifi, 1, 1)
        self.btn_wifi_scan = QPushButton("搜索 WiFi")
        self.btn_wifi_scan.setProperty("role", "primary")
        self.btn_wifi_scan.setFixedHeight(32)
        self.btn_wifi_scan.setToolTip("扫描电脑当前可见的 Wi-Fi 列表")
        self.btn_wifi_scan.clicked.connect(self._pick_wifi)
        g.addWidget(self.btn_wifi_scan, 1, 2)
        g.addWidget(self._field_label("Wi-Fi 密码"), 1, 3)
        self.edit_pwd = QLineEdit()
        self.edit_pwd.setEchoMode(QLineEdit.Password)
        self.edit_pwd.setFixedHeight(32)
        self.edit_pwd.setPlaceholderText("开放网络留空")
        g.addWidget(self.edit_pwd, 1, 4)
        g.setColumnStretch(1, 1)
        g.setColumnStretch(4, 1)

        # 底部：提示 + 配网按钮（整行铺满）
        self.lbl_prov_hint = QLabel("选择设备并填写 Wi-Fi 后，点击下方按钮下发配置")
        self.lbl_prov_hint.setStyleSheet("color:#6b7280; font-size:12px;")
        g.addWidget(self.lbl_prov_hint, 2, 0, 1, 6)
        self.btn_provision = QPushButton("配置 Wi-Fi 并添加设备")
        self.btn_provision.setProperty("role", "primary")
        self.btn_provision.clicked.connect(self._provision)
        g.addWidget(self.btn_provision, 3, 0, 1, 6)
        return ble

    # ------------------------------------------------------------- ② 远端一键导出日志
    def _build_merge_group(self):
        """② 设备实时拉取 + 本地 EGOViewer 打包，合并为一个分组（去掉中间分割线）。"""
        gb = QGroupBox("② 设备实时拉取 · 本地打包")
        v = QGridLayout(gb)
        v.setHorizontalSpacing(10)
        v.setVerticalSpacing(8)

        # --- SSH 凭证 ---
        v.addWidget(self._field_label("设备 IP"), 0, 0)
        self.edit_ip = QLineEdit()
        self.edit_ip.setPlaceholderText("配网成功后自动回填，或手动填写")
        if relay_config.DEVICE_IP:
            self.edit_ip.setText(relay_config.DEVICE_IP)
        elif relay_config.EXAMPLE_DEVICE_IP:
            self.edit_ip.setText(relay_config.EXAMPLE_DEVICE_IP)
        v.addWidget(self.edit_ip, 0, 1)
        v.addWidget(self._field_label("用户名"), 0, 2)
        self.edit_user = QLineEdit("root")
        self.edit_user.setPlaceholderText("ssh 用户名（常见为 root）")
        v.addWidget(self.edit_user, 0, 3)
        v.addWidget(self._field_label("密码"), 0, 4)
        self.edit_pwd_ssh = QLineEdit()
        self.edit_pwd_ssh.setEchoMode(QLineEdit.Password)
        self.edit_pwd_ssh.setPlaceholderText("ssh 密码")
        v.addWidget(self.edit_pwd_ssh, 0, 5)

        # --- EGOViewer 根目录（本地，识别版本安装目录） ---
        v.addWidget(self._field_label("EGOViewer 根目录"), 1, 0)
        rhl = QHBoxLayout()
        self.cmb_ev_root = QComboBox()
        self.cmb_ev_root.setToolTip("自动检测到的 EGOViewer 安装目录，可手动切换")
        self.cmb_ev_root.currentIndexChanged.connect(self._update_pack_counts)
        rhl.addWidget(self.cmb_ev_root, 1)
        btn_rescan = QPushButton("重新检测")
        btn_rescan.setToolTip("重新扫描常见安装路径下的 EGOViewer 目录")
        btn_rescan.clicked.connect(self._rescan_ev_roots)
        btn_browse_root = QPushButton("浏览…")
        btn_browse_root.clicked.connect(self._pick_ev_root)
        rhl.addWidget(btn_rescan)
        rhl.addWidget(btn_browse_root)
        rhl_w = QWidget(); rhl_w.setLayout(rhl)
        v.addWidget(rhl_w, 1, 1, 1, 5)

        # --- app 日志（远程 SSH 拉取，默认不勾选） ---
        v.addWidget(self._field_label("app 日志"), 2, 0)
        self.chk_app = QCheckBox("SSH/SFTP 拉取")
        self.chk_app.setChecked(False)
        self.edit_app_dir = QLineEdit("/ego/logs/app")
        self.edit_app_dir.setToolTip("设备端 app 日志目录，多个目录用 & 分隔")
        self.chk_app.toggled.connect(self._sync_ssh_enabled)
        ah = QHBoxLayout()
        ah.addWidget(self.chk_app)
        ah.addWidget(self.edit_app_dir, 1)
        ah_w = QWidget(); ah_w.setLayout(ah)
        v.addWidget(ah_w, 2, 1, 1, 5)

        # --- 本地日志分类（合并为「日志类型」一行；来源路径/数量进 Tooltip） ---
        self._pack_checks = {}
        self._pack_counts = {}
        self._pack_sources = {}
        r = 3
        v.addWidget(self._field_label("日志类型"), r, 0)
        # 灰底容器，内部每个日志选项一张白色圆角容器，横向分隔
        gray = QFrame()
        gray.setObjectName("logTypeRow")
        gray.setAttribute(Qt.WA_StyledBackground, True)
        gl = QHBoxLayout(gray)
        gl.setContentsMargins(10, 8, 10, 8)
        gl.setSpacing(10)
        for cat in evlog_pack.CATEGORIES:
            chk, cnt = self._build_pack_card(cat)
            self._pack_checks[cat["key"]] = chk
            self._pack_counts[cat["key"]] = cnt
            self._pack_sources[cat["key"]] = "、".join(cat["candidate_rel_dirs"])
            item = QFrame()
            item.setObjectName("logTypeCard")
            item.setAttribute(Qt.WA_StyledBackground, True)
            il = QHBoxLayout(item)
            il.setContentsMargins(12, 6, 12, 6)
            il.setSpacing(8)
            il.addWidget(chk)
            il.addWidget(cnt)
            gl.addWidget(item)
        gl.addStretch(1)      # 右侧留灰底
        gray_w = QWidget(); gray_w.setLayout(gl)
        v.addWidget(gray_w, r, 1, 1, 5)
        r += 1

        # --- 筛选方式：与整张表单对齐，左侧字段标签 + 右侧下拉/时间段同一行，切换自动显隐 ---
        # 时间段默认当天（起=今天 00:00，止=当前时刻），需要之前日期可自行选择，无需勾选。
        v.addWidget(self._field_label("筛选方式"), r, 0)
        fr = QHBoxLayout()
        fr.setSpacing(6)
        # 统一控件高度，避免下拉框与时间框对不齐
        CTRL_H = 30
        self.cmb_filter = QComboBox()
        self.cmb_filter.setFixedHeight(CTRL_H)
        fr.addWidget(self.cmb_filter)
        self.cmb_filter.addItem("时间段", "range")
        self.cmb_filter.addItem("手动勾选文件", "manual")
        self.cmb_filter.currentIndexChanged.connect(self._on_filter_mode)
        # 时间段范围：用「～」分隔起止，默认当天
        now = QDateTime.currentDateTime()
        today_start = QDateTime(now.date(), QTime(0, 0))
        self.edit_range_start = QDateTimeEdit(today_start)
        self.lbl_range_sep = QLabel("～")
        self.edit_range_end = QDateTimeEdit(now)
        for e in (self.edit_range_start, self.edit_range_end):
            e.setFixedHeight(CTRL_H)
            e.setDisplayFormat("yyyy-MM-dd HH:mm")
            e.setCalendarPopup(True)
            e.setMinimumWidth(150)
        self.lbl_range_sep.setFixedHeight(CTRL_H)
        self.edit_range_start.dateTimeChanged.connect(self._update_pack_counts)
        self.edit_range_end.dateTimeChanged.connect(self._update_pack_counts)
        # 手动模式控件
        self.btn_select_files = QPushButton("选择日志文件…")
        # 全局 QSS 给按钮 9px 垂直内边距，若再锁死 30px 高度会把文字裁掉、点击区被压扁，
        # 这里改用最小高度并去掉垂直内边距，保证文字完整且整块可点。
        self.btn_select_files.setStyleSheet("padding:0 16px;")
        self.btn_select_files.setMinimumHeight(CTRL_H)
        self.btn_select_files.setVisible(False)
        self.btn_select_files.clicked.connect(self._pick_log_files)
        self.lbl_manual = QLabel("点击按钮选择要打包的日志文件")
        self.lbl_manual.setStyleSheet("color:#6b7280;")
        self.lbl_manual.setVisible(False)
        # 排列：下拉 → 时间段 / 手动控件；弹性放行尾，避免被推散
        for w in (self.edit_range_start, self.lbl_range_sep, self.edit_range_end):
            w.setVisible(False)
        fr.addWidget(self.edit_range_start)
        fr.addWidget(self.lbl_range_sep)
        fr.addWidget(self.edit_range_end)
        fr.addWidget(self.btn_select_files)
        fr.addWidget(self.lbl_manual)
        fr.addStretch(1)
        fr_w = QWidget(); fr_w.setLayout(fr)
        v.addWidget(fr_w, r, 1, 1, 5)
        r += 1

        # 同步各筛选模式下内联参数 / 时间段的显隐
        self._on_filter_mode(self.cmb_filter.currentIndex())

        # --- 输出目录（设备导出与本地打包共用） ---
        v.addWidget(self._field_label("输出目录"), r, 0)
        ohl = QHBoxLayout()
        self.edit_local = QLineEdit()
        self.edit_local.setPlaceholderText("下载与报告保存目录")
        self.edit_local.setText(str(relay_config.EXPORT_DIR))
        btn_local = QPushButton("浏览…")
        btn_local.clicked.connect(self._pick_local)
        ohl.addWidget(self.edit_local, 1)
        ohl.addWidget(btn_local)
        ow = QWidget(); ow.setLayout(ohl)
        v.addWidget(ow, r, 1, 1, 5)
        r += 1

        # --- 选项（设备实时拉取与本地打包共用） ---
        self.chk_zip = QCheckBox("先压缩再导出")
        self.chk_zip.setChecked(True)
        self.chk_import = QCheckBox("拉取后自动导入分析")
        self.chk_import.setChecked(True)
        self.chk_report = QCheckBox("自动生成 HTML 报告")
        self.chk_report.setChecked(True)
        pk_chk = QHBoxLayout()
        for w in (self.chk_zip, self.chk_import, self.chk_report):
            pk_chk.addWidget(w)
        pk_chk.addStretch(1)
        pk_chk_w = QWidget(); pk_chk_w.setLayout(pk_chk)
        v.addWidget(self._field_label("选项"), r, 0)
        v.addWidget(pk_chk_w, r, 1, 1, 5)
        r += 1

        # --- 任务进度 + 执行按钮（同一行：进度条居左，按钮靠右） ---
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFixedHeight(34)
        self.btn_export = QPushButton("开始一键导出")
        self.btn_export.setProperty("role", "primary")
        self.btn_export.setMinimumHeight(34)
        self.btn_export.clicked.connect(self._start_export)
        self.btn_pack = QPushButton("打包导出")
        self.btn_pack.setMinimumHeight(34)
        self.btn_pack.clicked.connect(self._start_pack)
        self.btn_stop = QPushButton("取消任务")
        self.btn_stop.setProperty("role", "stop")
        self.btn_stop.setMinimumHeight(34)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop)
        pr_btn = QHBoxLayout()
        pr_btn.addWidget(self.progress, 1)
        pr_btn.addWidget(self.btn_export)
        pr_btn.addWidget(self.btn_pack)
        pr_btn.addWidget(self.btn_stop)
        pr_btn_w = QWidget(); pr_btn_w.setLayout(pr_btn)
        v.addWidget(self._field_label("任务进度"), r, 0)
        v.addWidget(pr_btn_w, r, 1, 1, 5)
        r += 1

        # --- 运行日志框（位于「选项」下方，紧随进度条；来自主窗口底部日志区） ---
        if self.log_box is None:
            self.log_box = QPlainTextEdit()
            self.log_box.setReadOnly(True)
        self.log_box.setPlaceholderText("[HH:MM:SS] 级别｜工具自身运行事件日志")
        self.log_box.setFixedHeight(120)
        v.addWidget(self._field_label("运行日志"), r, 0)
        v.addWidget(self.log_box, r, 1, 1, 5)
        r += 1

        v.setColumnStretch(1, 1)
        v.setColumnStretch(3, 1)
        v.setColumnStretch(5, 1)
        # 初始同步 SSH 凭证可用状态（默认未勾选 app 日志 → 置灰）
        self._sync_ssh_enabled(self.chk_app.isChecked())
        return gb

    def _build_pack_card(self, cat):
        """构建单个日志类型行内条目：勾选框 + 日志名称 + 状态标签。

        无独立背景/边框，直接与表单同底横排；来源路径与文件数量放入 hover Tooltip。
        """
        chk = QCheckBox(cat["label"])
        chk.setChecked(True)
        cnt = QLabel()
        tip = f"来源路径：{'、'.join(cat['candidate_rel_dirs'])}\n文件数量：未找到"
        chk.setToolTip(tip)   # 仅复选框带 Tooltip，数量标签不弹提示
        return chk, cnt

    def _sync_ssh_enabled(self, checked):
        """勾选「app 日志(SSH/SFTP 拉取)」时才启用 SSH 凭证输入，避免误填。"""
        for w in (self.edit_ip, self.edit_user, self.edit_pwd_ssh):
            w.setEnabled(checked)

    # ------------------------------------------------------------- BLE
    def _auto_scan_on_popup(self):
        """点击设备下拉框展开时自动触发扫描（未在扫描中才发起）。"""
        if self._scan_worker and self._scan_worker.isRunning():
            return
        self._scan()

    def _scan(self):
        self.btn_scan.setEnabled(False)
        self._set_ble_state("扫描中…")
        self.log("扫描蓝牙设备…（需电脑蓝牙已开启）")
        self._scan_worker = BleScanWorker()
        self._scan_worker.result.connect(self._on_scan_result)
        self._scan_worker.failed.connect(self._on_scan_fail)
        self._scan_worker.finished.connect(lambda: self.btn_scan.setEnabled(True))
        self._scan_worker.start()

    def _on_scan_result(self, devices):
        self._devices = devices
        self.dev_combo.set_devices(devices)
        self._set_ble_state(f"发现 {len(devices)} 台设备，请选择")
        self.log(f"蓝牙扫描完成: 发现 {len(devices)} 台设备")

    def _on_scan_fail(self, msg):
        self._set_ble_state("扫描失败")
        self._append(f"[错误] 蓝牙扫描失败: {msg}")
        self.log(f"蓝牙扫描失败: {msg}")
        QMessageBox.warning(
            self, "扫描失败",
            f"蓝牙扫描失败：{msg}\n\n若提示 EgoLowBle.dll 相关，说明 ABI 尚未与真机校准，"
            "拿到 EgoLowBle.h 头文件后即可修复。")

    def _on_dev_pick(self, dev):
        if not dev:
            self._selected_address = ""
            return
        self._selected_address = dev.get("address", "")
        self._set_ble_state(f"已选择：{dev.get('name') or '(未命名)'}")
        self._append(f"已选择设备: {dev.get('name')}  {self._selected_address}")

    def _set_ble_state(self, text):
        self.lbl_ble_state.setText(text)
        low = text.lower()
        if "失败" in text or "error" in low:
            color = "#dc2626"
        elif "成功" in text or "已联网" in text or "ip" in low:
            color = "#16a34a"
        elif "中" in text:
            color = "#2563eb"
        else:
            color = "#6b7280"
        self.lbl_ble_state.setStyleSheet(f"color:{color};")

    @staticmethod
    def _field_label(text):
        """表单列标签：统一宽度 + 居中，让各输入框/标签大小对齐。"""
        lbl = QLabel(text)
        lbl.setMinimumWidth(104)
        lbl.setAlignment(Qt.AlignCenter)
        return lbl

    # ------------------------------------------------------------- WiFi 搜索（配网选网）
    def _pick_wifi(self):
        self.log("扫描本机可见 WiFi…")
        self._wifi_scan_worker = WifiScanWorker()
        self._wifi_scan_worker.result.connect(self._on_wifi_scan_result)
        self._wifi_scan_worker.failed.connect(self._on_wifi_scan_fail)
        self._wifi_scan_worker.start()

    def _on_wifi_scan_result(self, data):
        nets = data.get("networks") or []
        cur = (data.get("interface") or {}).get("ssid", "")
        cur_lower = cur.lower()
        self.cmb_wifi.blockSignals(True)
        self.cmb_wifi.clear()
        self.cmb_wifi.addItem("— 选择 WiFi —", "")
        for n in nets:
            ssid = n.get("ssid") or "(隐藏网络)"
            is_cur = bool(cur) and ssid.lower() == cur_lower
            mark = "  [电脑已连接]" if is_cur else ""
            self.cmb_wifi.addItem(f"{ssid}{mark}（信号{n.get('signal', 0)}%）", ssid)
            if is_cur:
                self.cmb_wifi.setCurrentIndex(self.cmb_wifi.count() - 1)
        self.cmb_wifi.blockSignals(False)
        if not nets:
            QMessageBox.information(
                self, "WiFi 扫描", "未发现可见 WiFi，请确认电脑无线网卡已开启并靠近路由器。")
            return
        if cur:
            self.log(f"已扫描，自动选中电脑当前连接的 Wi-Fi：{cur}（也可从下拉另选）")
        else:
            self.log(f"扫描完成，发现 {len(nets)} 台可见 Wi-Fi（电脑当前未连接，请从下拉选择）")

    def _on_wifi_pick(self):
        ssid = self.cmb_wifi.currentData()
        if ssid:
            self.log(f"已选择 Wi-Fi：{ssid}（请核对密码后下发）")

    def _on_wifi_scan_fail(self, msg):
        self._append(f"[错误] WiFi 扫描失败: {msg}")
        self.log(f"WiFi 扫描失败: {msg}")
        QMessageBox.warning(self, "WiFi 扫描失败", str(msg))

    def _provision(self):
        """配网：下发 Wi-Fi → 等 IP（独立功能，不再联动一键导出）。"""
        if not self._selected_address:
            QMessageBox.warning(self, "提示", "请先在设备列表中选中一台设备")
            return
        ssid = self.cmb_wifi.currentData() or self.cmb_wifi.currentText().strip()
        if not ssid or ssid.startswith("— 选择"):
            # 未选时自动带出电脑当前连接的 WiFi
            try:
                from ego_relay import wifi
                cur = (wifi.status() or {}).get("ssid", "")
                if cur:
                    idx = self.cmb_wifi.findData(cur)
                    if idx >= 0:
                        self.cmb_wifi.setCurrentIndex(idx)
                    else:
                        self.cmb_wifi.addItem(f"{cur}  [电脑已连接]", cur)
                        self.cmb_wifi.setCurrentIndex(self.cmb_wifi.count() - 1)
                    ssid = cur
                    self.log(f"自动选中电脑当前连接的 Wi-Fi：{cur}")
            except Exception:  # noqa: BLE001
                ssid = ""
        if not ssid:
            QMessageBox.warning(self, "提示", "Wi-Fi 名称不能为空")
            return
        self._set_ble_state("配网中…")
        self._append(f"开始配网：设备 {self._selected_address}，Wi-Fi {ssid}")
        self.log(f"配网：设备 {self._selected_address}，Wi-Fi {ssid}")
        self._provision_worker = DeviceBleProvisionWorker(
            self._selected_address, ssid, self.edit_pwd.text())
        self._provision_worker.status.connect(self._on_provision_status)
        self._provision_worker.done.connect(self._on_provision_done)
        self._provision_worker.failed.connect(self._on_provision_fail)
        self._provision_worker.start()

    def _on_provision_status(self, text):
        self._set_ble_state(text)
        self._append(text)

    def _on_provision_done(self, ip):
        if ip:
            self._set_ble_state(f"已联网 · IP = {ip}")
            self._append(f"配网成功，设备 IP = {ip}")
            self.log(f"配网成功: 设备 IP = {ip}")
            # 自动回填到② 合并分组的设备 IP，方便直接使用
            self.edit_ip.setText(ip)
        else:
            self._set_ble_state("未拿到 IP")
            self._append("配网已下发，但未在超时内拿到 IP")

    def _on_provision_fail(self, msg):
        self._set_ble_state("配网失败")
        self._append(f"[错误] 配网失败: {msg}")
        self.log(f"配网失败: {msg}")

    # ------------------------------------------------------------- 远端一键导出
    def _pick_local(self):
        d = QFileDialog.getExistingDirectory(self, "选择本地保存目录")
        if d:
            self.edit_local.setText(d)

    def _start_export(self):
        """开始一键导出：识别 EGOViewer 根目录 → 全新时间戳目录收集所选日志 → 导入分析 + 报告。"""
        if self._export_worker and self._export_worker.isRunning():
            QMessageBox.information(self, "提示", "已有导出任务进行中")
            return
        if self._pack_worker and self._pack_worker.isRunning():
            QMessageBox.information(self, "提示", "已有打包任务进行中")
            return
        if self._dp_worker and self._dp_worker.isRunning():
            QMessageBox.information(self, "提示", "已有 SSH 拉取任务进行中")
            return
        root = self._current_ev_root()
        if not root or not os.path.isdir(root):
            QMessageBox.warning(self, "提示", "请先选择有效的 EGOViewer 安装根目录")
            return
        keys = [k for k, chk in self._pack_checks.items() if chk.isChecked()]
        if not keys:
            QMessageBox.warning(self, "提示", "请至少勾选一种日志")
            return
        out_dir = self.edit_local.text().strip() or str(relay_config.EXPORT_DIR)
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            QMessageBox.warning(self, "提示", f"输出目录不可写：{e}")
            return

        self.progress.setValue(0)
        self.btn_export.setEnabled(False)
        self.btn_pack.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.log(f"开始一键导出：{root} → {out_dir}（勾选 {'/'.join(keys)}）")
        self._export_worker = EgoOneClickExportWorker(
            root, out_dir, keys,
            auto_import=self.chk_import.isChecked(),
            auto_report=self.chk_report.isChecked())
        self._export_worker.progress.connect(self.progress.setValue)
        self._export_worker.status.connect(self._append)
        self._export_worker.done.connect(self._on_export_done)
        self._export_worker.failed.connect(self._on_export_fail)
        self._export_worker.finished.connect(self._on_export_finished)
        self._export_worker.start()

    def _on_export_done(self, export_dir, sid, report_path):
        if sid:
            self._append(f"完成：会话 #{sid} 已入库")
            self.log(f"一键导出并导入成功: 会话 #{sid}")
            self.imported.emit(sid)
        else:
            self._append("导出完成，但未产生可分析会话")
        if report_path:
            self._append(f"报告已生成：{report_path}")
        ret = QMessageBox.question(
            self, "导出完成",
            f"已导出日志到：\n{export_dir}\n"
            + (f"\n分析会话：#{sid}\n报告：{report_path}" if report_path else "")
            + "\n\n是否打开所在文件夹？",
            QMessageBox.Yes | QMessageBox.No)
        if ret == QMessageBox.Yes:
            import subprocess
            subprocess.Popen(["explorer", "/select,", export_dir])

    def _on_export_fail(self, err):
        self._append(f"[错误] 一键导出失败: {err}")
        self.log(f"一键导出失败: {err}")

    def _on_export_finished(self):
        self.btn_export.setEnabled(True)
        self.btn_pack.setEnabled(True)
        self.btn_stop.setEnabled(False)

    def _rescan_ev_roots(self):
        try:
            roots = evlog_pack.find_ego_viewer_roots()
        except Exception as e:  # noqa: BLE001
            self._append(f"[错误] 检测 EGOViewer 目录失败: {e}")
            self.log(f"检测 EGOViewer 目录失败: {e}")
            return
        self.cmb_ev_root.blockSignals(True)
        self.cmb_ev_root.clear()
        for rt in roots:
            self.cmb_ev_root.addItem(rt, rt)
        if roots:
            self.cmb_ev_root.setCurrentIndex(0)
        self.cmb_ev_root.blockSignals(False)
        self._update_pack_counts()
        if not roots:
            self._append("未自动检测到 EGOViewer 安装目录，可点「浏览…」手动指定")
            self.log("未自动检测到本机 EGOViewer 安装目录")

    def _pick_ev_root(self):
        d = QFileDialog.getExistingDirectory(self, "选择 EGOViewer 安装根目录")
        if d:
            idx = self.cmb_ev_root.findData(d)
            if idx < 0:
                self.cmb_ev_root.addItem(d, d)
                idx = self.cmb_ev_root.findData(d)
            self.cmb_ev_root.setCurrentIndex(idx)

    def _current_ev_root(self):
        return self.cmb_ev_root.currentData() or self.cmb_ev_root.currentText()

    def _current_filters(self):
        """按当前筛选方式返回传给 evlog_pack.stage/collect 的关键字参数。"""
        mode = self.cmb_filter.currentData()
        if mode == "range":
            return {
                "start": self.edit_range_start.dateTime().toSecsSinceEpoch(),
                "end": self.edit_range_end.dateTime().toSecsSinceEpoch(),
            }
        if mode == "manual":
            return {"manual": self._manual_selection or {}}
        return {}

    def _update_pack_counts(self):
        """刷新各分类勾选框旁的日志计数。"""
        root = self._current_ev_root()
        filters = self._current_filters()
        counts = {}
        if root and os.path.isdir(root):
            try:
                c = evlog_pack.collect(root, **filters)
                counts = {k: len(v) for k, v in c.items()}
            except Exception:  # noqa: BLE001
                counts = {}
        for key, lbl in self._pack_counts.items():
            n = counts.get(key, 0)
            txt = f"[{n} 个]" if n else "[未找到]"
            lbl.setText(txt)
            lbl.setStyleSheet("color:#2563eb;" if n else "color:#9ca3af;")
            # 同步更新 hover Tooltip 里的文件数量
            chk = self._pack_checks.get(key)
            if chk is not None:
                tip = f"来源路径：{self._pack_sources.get(key, '')}\n文件数量：{txt}"
                chk.setToolTip(tip)   # 仅复选框带 Tooltip，数量标签不弹提示

    def _on_filter_mode(self, _idx):
        if not hasattr(self, "cmb_filter"):
            return
        mode = self.cmb_filter.currentData()
        is_manual = (mode == "manual")
        is_range = (mode == "range")
        for w in (self.edit_range_start, self.lbl_range_sep, self.edit_range_end):
            w.setVisible(is_range)
        self.btn_select_files.setVisible(is_manual)
        self.lbl_manual.setVisible(is_manual)
        if is_manual:
            # 切换模式后按钮刚从隐藏变为显示，强制刷新一次几何布局，
            # 否则固定高度被重算为 0，按钮只剩一条缝、点不中。
            self.btn_select_files.updateGeometry()
        self._update_pack_counts()

    def _pick_log_files(self):
        """打开对话框，按分类勾选要打包的具体日志文件（可精确控制打包范围，防止不同步）。"""
        root = self._current_ev_root()
        if not root or not os.path.isdir(root):
            QMessageBox.warning(self, "提示", "请先选择有效的 EGOViewer 安装根目录")
            return
        keys = [k for k, chk in self._pack_checks.items() if chk.isChecked()]
        if not keys:
            QMessageBox.warning(self, "提示", "请至少勾选一种日志分类")
            return
        disc = evlog_pack.discover(root, keys)

        dlg = QDialog(self)
        dlg.setWindowTitle("选择要打包的日志文件（精确控制打包范围）")
        dlg.resize(700, 540)
        lay = QVBoxLayout(dlg)
        hint = QLabel("按分类勾选要打包的日志。若要取某次测试会话，勾选时间相近的日志即可；"
                      "顶部分类项可整类全选/全不选。打包范围越精确，导入分析越不易不同步。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#6b7280;")
        lay.addWidget(hint)
        tree = QTreeWidget()
        tree.setHeaderLabels(["日志文件", "时间"])
        tree.setSelectionMode(QAbstractItemView.NoSelection)
        cur = self._manual_selection
        for cat in evlog_pack.CATEGORIES:
            key = cat["key"]
            files = disc.get(key, [])
            if not files:
                continue
            top = QTreeWidgetItem([f"{cat['label']}（{len(files)} 个）", ""])
            top.setFlags(top.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate)
            tree.addTopLevelItem(top)
            for f in files:
                stamp = datetime.fromtimestamp(f["stamp"]).strftime("%Y-%m-%d %H:%M:%S")
                it = QTreeWidgetItem([os.path.basename(f["abs"]), stamp])
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setData(0, Qt.UserRole, (key, f["abs"]))
                it.setToolTip(0, f["abs"])
                checked = (cur is None) or (f["abs"] in cur.get(key, []))
                it.setCheckState(0, Qt.Checked if checked else Qt.Unchecked)
                top.addChild(it)
            n_on = sum(1 for j in range(top.childCount())
                       if top.child(j).checkState(0) == Qt.Checked)
            if n_on == top.childCount():
                top.setCheckState(0, Qt.Checked)
            elif n_on == 0:
                top.setCheckState(0, Qt.Unchecked)
            else:
                top.setCheckState(0, Qt.PartiallyChecked)
        tree.expandAll()
        lay.addWidget(tree, 1)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)

        if dlg.exec() != QDialog.Accepted:
            return
        sel = {}
        for i in range(tree.topLevelItemCount()):
            top = tree.topLevelItem(i)
            for j in range(top.childCount()):
                it = top.child(j)
                if it.checkState(0) == Qt.Checked:
                    key, abs_path = it.data(0, Qt.UserRole)
                    sel.setdefault(key, []).append(abs_path)
        self._manual_selection = sel
        total = sum(len(v) for v in sel.values())
        self.lbl_manual.setText(f"已选 {total} 个文件（可再次点击调整）" if total
                                else "未选择任何文件")
        self.lbl_manual.setStyleSheet("color:#16a34a;" if total else "color:#d97706;")
        self._update_pack_counts()

    def _start_pack(self):
        if self._pack_worker and self._pack_worker.isRunning():
            QMessageBox.information(self, "提示", "已有打包任务进行中")
            return
        if self._export_worker and self._export_worker.isRunning():
            QMessageBox.information(self, "提示", "已有导出任务进行中")
            return
        if self._dp_worker and self._dp_worker.isRunning():
            QMessageBox.information(self, "提示", "已有 SSH 拉取任务进行中")
            return
        root = self._current_ev_root()
        if not root or not os.path.isdir(root):
            QMessageBox.warning(self, "提示", "请先选择有效的 EGOViewer 安装根目录")
            return
        keys = [k for k, chk in self._pack_checks.items() if chk.isChecked()]
        if not keys:
            QMessageBox.warning(self, "提示", "请至少勾选一种日志")
            return
        filters = self._current_filters()
        if self.cmb_filter.currentData() == "manual" and not (self._manual_selection or {}):
            QMessageBox.warning(self, "提示", "请先点击「选择日志文件…」勾选要打包的日志")
            return
        out_dir = self.edit_local.text().strip() or str(relay_config.EXPORT_DIR)
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            QMessageBox.warning(self, "提示", f"输出目录不可写：{e}")
            return

        self.progress.setValue(0)
        self.btn_pack.setEnabled(False)
        self.btn_export.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.log(f"开始打包本地 EGOViewer 日志：{root} → {out_dir}")
        self._pack_worker = EvLogPackWorker(
            root, out_dir, keys,
            stage_kwargs=filters,
            auto_import=self.chk_import.isChecked(),
            auto_report=self.chk_report.isChecked())
        self._pack_worker.progress.connect(self.progress.setValue)
        self._pack_worker.status.connect(self._append)
        self._pack_worker.done.connect(self._on_pack_done)
        self._pack_worker.failed.connect(self._on_pack_fail)
        self._pack_worker.finished.connect(self._on_pack_finished)
        self._pack_worker.start()

    def _on_pack_done(self, zip_path, sid, report_path):
        self._append(f"打包完成：{zip_path}")
        self.log(f"本地 EGOViewer 日志打包完成: {zip_path}")
        if sid:
            self._append(f"分析完成：会话 #{sid} 已入库")
            self.log(f"本地日志分析入库成功: 会话 #{sid}")
            self.imported.emit(sid)
        if report_path:
            self._append(f"报告已生成：{report_path}")
        ret = QMessageBox.question(
            self, "打包完成", f"已生成日志包：\n{zip_path}\n"
            + (f"\n分析会话：#{sid}\n报告：{report_path}" if report_path else "")
            + "\n\n是否打开所在文件夹？",
            QMessageBox.Yes | QMessageBox.No)
        if ret == QMessageBox.Yes:
            import subprocess
            subprocess.Popen(["explorer", "/select,", zip_path])

    def _on_pack_fail(self, err):
        self._append(f"[错误] 打包失败: {err}")
        self.log(f"本地日志打包失败: {err}")

    def _on_pack_finished(self):
        self.btn_pack.setEnabled(True)
        self.btn_export.setEnabled(True)
        self.btn_stop.setEnabled(False)

    def _stop(self):
        if self._export_worker and self._export_worker.isRunning():
            self._append("导出进行中，暂不支持中途取消（可等待完成或关闭窗口）")
            self.log("导出进行中，无法中途取消")
        elif self._pack_worker and self._pack_worker.isRunning():
            self._append("打包进行中，暂不支持中途取消（可等待完成或关闭窗口）")
            self.log("打包进行中，无法中途取消")
        elif self._dp_worker and self._dp_worker.isRunning():
            self._dp_worker.cancel()
            self.progress.setValue(0)
            self.btn_stop.setEnabled(False)
            self._append("已请求取消 SSH 拉取任务")
            self.log("已取消 SSH 拉取任务")
        else:
            self.log("当前没有运行中的导出任务")

    # ------------------------------------------------------------- 日志
    def _append(self, text):
        """过程日志统一走底部全局日志框（不再有本页重复的任务日志框）。"""
        self.log(text)
