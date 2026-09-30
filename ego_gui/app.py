"""EGO Courier 主窗口：设备与日志工作台 + 分析查询页。"""

from __future__ import annotations

import json
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QTabWidget, QTabBar, QPlainTextEdit,
    QStatusBar, QLabel,
)

from ego_relay import config

from .device_tab import DeviceTab
from .sessions_tab import SessionsTab
from .analysis_tab import AnalysisTab
from .trace_tab import TraceTab

# 全局 QSS：与 Web 版统一（浅色顶栏 + 蓝色激活下划线 + 浅灰内容区 + 白色表格，简洁干净）
STYLE = """
QWidget { font-family:"Microsoft YaHei","Segoe UI"; font-size:13px; color:#222; }
QMainWindow, QWidget { background:#f5f6f8; }
/* ---- 顶栏 Tab（浅色，蓝色激活下划线，去掉黑色） ---- */
QTabWidget::pane { border:0; background:#f5f6f8; top:-1px; }
QTabBar { background:#ffffff; border-bottom:1px solid #e5e7eb; }
QTabBar::tab { background:transparent; color:#6b7280; padding:10px 22px; border:0; }
QTabBar::tab:selected { color:#2563eb; border-bottom:2px solid #3b82f6; font-weight:600; }
QTabBar::tab:hover:!selected { color:#2563eb; }
/* ---- 按钮（仿 Web：白底灰边 / primary 蓝 / danger 橙 / stop 灰） ---- */
QPushButton { padding:9px 16px; border:1px solid #d1d5db; border-radius:4px;
  background:#ffffff; color:#222; }
QPushButton:hover { background:#f3f4f6; }
QPushButton:disabled { background:#f3f4f6; color:#9ca3af; border-color:#e5e7eb; }
QPushButton[role="primary"] { background:#3b82f6; color:#ffffff; border-color:#3b82f6; }
QPushButton[role="primary"]:hover { background:#2563eb; }
QPushButton[role="danger"]  { background:#f97316; color:#ffffff; border-color:#f97316; }
QPushButton[role="danger"]:hover  { background:#ea580c; }
QPushButton[role="stop"]    { background:#e5e7eb; color:#374151; border-color:#d1d5db; }
QPushButton[role="stop"]:hover    { background:#d1d5db; }
/* ---- 输入框 ---- */
QLineEdit, QPlainTextEdit, QComboBox { background:#ffffff; border:1px solid #d1d5db;
  border-radius:4px; padding:5px 7px; }
/* ---- 表格（白底、浅表头、悬停高亮、隔行） ---- */
QTableWidget { background:#ffffff; border:1px solid #e5e7eb; border-radius:8px;
  alternate-background-color:#fafbfc; gridline-color:#eef1f5; selection-background-color:#dbeafe;
  selection-color:#1e3a8a; }
QHeaderView::section { background:#f9fafb; color:#374151; font-weight:600; padding:7px;
  border:0; border-bottom:1px solid #e5e7eb; }
QTableWidget::item:hover { background:#f0f7ff; }
/* ---- 悬浮下拉选择器（配网设备）：外层统一为表单控件样式，内部透明无边框 ---- */
QWidget#searchComboBox { background:#ffffff; border:1px solid #d1d5db;
  border-radius:4px; }
QLineEdit#searchComboDisplay { border:none; background:transparent; padding:0; }
QToolButton#searchComboArrow { border:none; background:transparent;
  color:#6b7280; padding:0 3px; }
QToolButton#searchComboArrow:hover { color:#374151; }
/* 扫描 BLE 设备按钮：灰色次按钮样式，无垂直内边距，高度 32px 由代码固定 */
QPushButton#scanBtn { padding:0 16px; background:#f3f4f6; color:#374151;
  border:1px solid #d1d5db; border-radius:4px; font-size:13px; }
QPushButton#scanBtn:hover { background:#e5e7eb; }
QFrame#devicePopup { background:#ffffff; border:1px solid #94a3b8; border-radius:8px; }
QListWidget#devicePopupList { background:#ffffff; border:1px solid #e2e5ea;
  border-radius:6px; outline:0; }
QListWidget#devicePopupList::item { padding:6px 8px; border-radius:4px; }
QListWidget#devicePopupList::item:selected { background:#dbeafe; color:#1e3a8a; }
QListWidget#devicePopupList::item:hover:!selected { background:#eff6ff; }
/* ---- 分组框 / 卡片 ---- */
QGroupBox { border:1px solid #e2e5ea; border-radius:8px; margin-top:14px; font-weight:600; background:#ffffff; }
QGroupBox::title { subcontrol-origin:margin; left:10px; padding:0 4px; color:#374151; }
QFrame#statCard { background:#ffffff; border:1px solid #e2e5ea; border-radius:8px; }
QLabel#statValue { font-size:22px; font-weight:bold; color:#222; }
QLabel#statLabel { font-size:12px; color:#6b7280; }
QSplitter::handle { background:#e5e7eb; width:2px; }
"""

# 顶部 Tab 顺序持久化：存到 data/tab_order.json（与 thresholds.json 同一目录），
# 下次启动按用户自定义顺序恢复。格式为按显示顺序排列的 Tab 标题列表。
TAB_ORDER_FILE = config.DATA_ROOT / "tab_order.json"


def _load_tab_order() -> list:
    if TAB_ORDER_FILE.exists():
        try:
            data = TAB_ORDER_FILE.read_text("utf-8").strip()
            if data:
                order = json.loads(data)
                if isinstance(order, list) and all(isinstance(x, str) for x in order):
                    return order
        except Exception:
            pass
    return []


def _save_tab_order(order: list) -> None:
    try:
        TAB_ORDER_FILE.write_text(
            json.dumps(order, ensure_ascii=False, indent=2), "utf-8")
    except Exception:
        pass


class Logger:
    """把事件写入底部日志框，按级别着色，格式 [HH:MM:SS] 级别｜描述。"""

    def __init__(self, box: QPlainTextEdit):
        self.box = box

    def _fmt(self):
        return datetime.now().strftime("%H:%M:%S")

    def _emit(self, text, color):
        cur = self.box.textCursor()
        cur.movePosition(QTextCursor.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        cur.insertText(text, fmt)
        self.box.setTextCursor(cur)
        self.box.ensureCursorVisible()

    def log(self, msg: str) -> None:
        if any(k in msg for k in ("ERROR", "失败", "error", "错误")):
            self._emit(f"[{self._fmt()}] ERROR｜{msg}\n", "#dc2626")
        elif any(k in msg for k in ("WARNING", "警告")):
            self._emit(f"[{self._fmt()}] WARNING｜{msg}\n", "#d97706")
        else:
            self._emit(f"[{self._fmt()}] INFO｜{msg}\n", "#111827")


class FullWidthTabBar(QTabBar):
    """让顶部 Tab 栏（白色底框含底部下划线）横向铺满窗口宽度，与左右边缘对齐。"""

    def sizeHint(self):
        s = super().sizeHint()
        parent = self.parentWidget()
        if parent is not None:
            s.setWidth(max(s.width(), parent.width()))
        return s


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("EGO Courier")
        self.setStyleSheet(STYLE)
        self._build_ui()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        # 顶部 Tab 栏铺满窗口宽度：白色底框（含底部下划线）延伸到左右窗口边缘，
        # 与窗口左右边缘对齐；内容留白由各 Tab 页与底部日志区自带。
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumHeight(90)
        self.log_box.setPlaceholderText("[HH:MM:SS] 级别｜工具自身运行事件日志")
        self.logger = Logger(self.log_box)
        self.log = self.logger.log

        # 合并设备接入、app/固件日志导出和本地导入为一个工作台。
        self.tabs = QTabWidget()
        self.tabs.setTabBar(FullWidthTabBar(self.tabs))
        self.device_tab = DeviceTab(self.log)
        self.sessions_tab = SessionsTab(self.log)
        self.analysis_tab = AnalysisTab(self.log)
        self.trace_tab = TraceTab(self.log)
        for tab, title in (
            (self.device_tab, "日志导出"),
            (self.sessions_tab, "会话记录"),
            (self.analysis_tab, "日志分析"),
            (self.trace_tab, "Trace 查询"),
        ):
            self.tabs.addTab(tab, title)

        # 支持 Tab 拖拽重排：鼠标按住 Tab 文字区域可横向拖动，
        # 拖动时显示占位指示，松开后插入目标位置；点击切换功能不受影响。
        self.tabs.setMovable(True)
        self._restore_tab_order()
        self.tabs.tabBar().tabMoved.connect(self._save_current_tab_order)
        # 默认打开「日志导出」页（不随上次保存的 Tab 顺序变化）；
        # Tab 标签不显示点击后的虚线焦点框（保留鼠标操作，仅去掉视觉焦点框）。
        self.tabs.setCurrentWidget(self.device_tab)
        self.tabs.tabBar().setFocusPolicy(Qt.NoFocus)

        # 跨页联动
        # 日志分析完成 → 刷新会话记录列表（会话记录页保留，可在其中打开详情）
        self.analysis_tab.imported.connect(lambda _sid: self.sessions_tab.refresh_list())
        # 在会话记录页打开刚分析的会话
        self.analysis_tab.open_in_sessions.connect(self._open_session)
        # 设备工作台一键导出/本地导入成功 → 刷新并打开新会话
        self.device_tab.imported.connect(lambda _sid: self.sessions_tab.refresh_list())
        self.device_tab.imported.connect(self._open_session)

        root.addWidget(self.tabs, stretch=1)

        # 底部运行日志区（自带左右/底部留白，保持与其他内容框右边缘对齐）
        log_wrap = QWidget()
        lw = QVBoxLayout(log_wrap)
        lw.setContentsMargins(10, 8, 10, 6)
        lw.addWidget(self.log_box)
        root.addWidget(log_wrap)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status_label = QLabel("就绪")
        self.status.addPermanentWidget(self.status_label)

        self.log("EGO Courier 桌面版已启动（日志导出/会话记录/日志分析/Trace 查询）")

    def _current_tab_order(self) -> list:
        """按当前显示顺序返回 Tab 标题列表。"""
        return [self.tabs.tabText(i) for i in range(self.tabs.count())]

    def _restore_tab_order(self) -> None:
        """启动时按上次保存的顺序恢复 Tab 位置（QTabWidget.insertTab 会移动已存在的 widget）。"""
        saved = _load_tab_order()
        if not saved:
            return
        by_title = {self.tabs.tabText(i): self.tabs.widget(i)
                    for i in range(self.tabs.count())}
        for idx, title in enumerate(saved):
            widget = by_title.get(title)
            if widget is not None:
                self.tabs.insertTab(idx, widget, title)

    def _save_current_tab_order(self, _from: int, _to: int) -> None:
        """拖拽重排后把当前顺序写回配置文件。"""
        _save_tab_order(self._current_tab_order())

    def _open_session(self, session_id: int) -> None:
        self.sessions_tab.open_session(session_id)
        self.tabs.setCurrentWidget(self.sessions_tab)
