"""会话记录 Tab：每次「日志分析」的归档，可按设备/数据源/结论筛选、回看、对比。

设计要点（与「日志分析」页职责切分）：
- 分析页负责**跑**一次分析；本页负责**留存**跑过的每一次，提供事后追溯。
- 记录里只存路径与统计，不复制报告；报告/数据目录被移动后如实标灰，
  绝不悄悄隐藏——用户要知道"那条记录对应的文件去哪了"。
- 列表 ↔ 详情用同一页内的抽屉（QStackedWidget），与旧会话页的返回体验一致；
  详情新增「Part 明细」子页，替代已随旧引擎移除的帧对齐表/异常列表原始表。
- 结论一律以 ``PartResult.business_status`` 为准（与 HTML 报告同一口径），
  历史页不另起一套判定逻辑。
"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QUrl, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu, QMessageBox,
    QPlainTextEdit, QPushButton, QStackedWidget, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget,
)

from ego_relay import history

COLUMNS = [
    ("time", "时间"),
    ("status", "结论"),
    ("parts", "Part 数"),
    ("abnormal", "异常"),
    ("devices", "设备"),
    ("root", "数据目录"),
    ("report", "报告"),
    ("note", "备注"),
]

STATUS_TEXT = {
    "finished": "完成",
    "partial": "部分报告",
    "failed": "失败",
}
STATUS_ORDER = ["finished", "partial", "failed"]

STATUS_COLOR = {
    "finished": ("#047857", "#ecfdf5"),
    "partial": ("#b45309", "#fffbeb"),
    "failed": ("#b91c1c", "#fdecea"),
}


def _fmt_time(text) -> str:
    text = str(text or "")
    if not text:
        return "-"
    try:
        from datetime import datetime
        return datetime.fromisoformat(text).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return text


def _basename(path) -> str:
    text = str(path or "").strip()
    if not text:
        return "-"
    return Path(text).name or text


class HistoryTab(QWidget):
    """会话记录页：筛选栏 + 记录表 + 详情抽屉。"""

    open_analysis_requested = Signal(str)   # "按此目录重新分析"
    status_message = Signal(str)            # 交给主窗口底部状态栏

    def __init__(self, log=None):
        super().__init__()
        self.log = log or (lambda _m: None)
        self._rows: list = []
        self._build_ui()
        self.reload()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_list_page())
        self.stack.addWidget(self._build_detail_page())
        root.addWidget(self.stack, stretch=1)

    # -- list page ---------------------------------------------------------
    def _build_list_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)
        v.addWidget(self._build_filter_bar())

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels([c[1] for c in COLUMNS])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.doubleClicked.connect(lambda _idx: self._open_selected())
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(QHeaderView.ResizeToContents)
        head.setSectionResizeMode(5, QHeaderView.Stretch)   # 数据目录占满剩余宽度
        v.addWidget(self.table, stretch=1)
        self.lbl_empty = QLabel("暂无会话记录。在「日志分析」页完成一次分析后，这里会自动出现一条记录。")
        self.lbl_empty.setStyleSheet("color:#6b7280;")
        v.addWidget(self.lbl_empty)
        return page

    def _build_filter_bar(self) -> QWidget:
        bar = QWidget()
        h = QHBoxLayout(bar)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)

        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("搜索设备 / 数据目录 / 备注")
        self.ed_search.setClearButtonEnabled(True)
        self.ed_search.setMinimumWidth(240)
        self.ed_search.textChanged.connect(self.reload)
        h.addWidget(self.ed_search)

        self.cmb_status = QComboBox()
        self.cmb_status.addItem("全部结论", "")
        for key in STATUS_ORDER:
            self.cmb_status.addItem(STATUS_TEXT[key], key)
        self.cmb_status.currentIndexChanged.connect(self.reload)
        h.addWidget(self.cmb_status)

        self.cmb_source = QComboBox()
        self.cmb_source.addItem("全部数据源", "")
        self.cmb_source.addItem("导出目录（data/exports）", "export")
        self.cmb_source.addItem("手动选择的目录", "manual")
        self.cmb_source.currentIndexChanged.connect(self.reload)
        h.addWidget(self.cmb_source)

        self.chk_missing = QCheckBox("仅报告缺失")
        self.chk_missing.toggled.connect(self.reload)
        h.addWidget(self.chk_missing)

        h.addStretch(1)

        def tool(text, tip, slot):
            btn = QPushButton(text)
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            h.addWidget(btn)
            return btn

        tool("刷新", "重新读取历史登记簿", self.reload)
        tool("打开", "打开选中记录的详情", self._open_selected)
        self._tool_compare = tool("对比", "并排对比两条记录的统计（先选中 2 条）",
                                  self._compare_selected)
        self._tool_delete = tool("删除", "删除选中记录（默认保留报告文件）",
                                 self._delete_selected)
        tool("在文件夹中显示", "打开选中记录的报告所在目录",
             self._reveal_selected)
        return bar

    # -- detail page -------------------------------------------------------
    def _build_detail_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        top = QHBoxLayout()
        self.btn_back = QPushButton("← 返回列表")
        self.btn_back.setObjectName("backBtn")
        self.btn_back.clicked.connect(self._show_list)
        top.addWidget(self.btn_back)
        self.lbl_detail_title = QLabel("")
        self.lbl_detail_title.setStyleSheet("font-weight:600;font-size:14px;")
        top.addWidget(self.lbl_detail_title, stretch=1)
        self.btn_open_html = QPushButton("打开报告")
        self.btn_open_html.setProperty("role", "primary")
        self.btn_open_html.clicked.connect(self._open_html)
        self.btn_reanalyze = QPushButton("按此目录重新分析")
        self.btn_reanalyze.clicked.connect(self._reanalyze)
        top.addWidget(self.btn_reanalyze)
        top.addWidget(self.btn_open_html)
        v.addLayout(top)

        self.lbl_notice = QLabel("")
        self.lbl_notice.setWordWrap(True)
        self.lbl_notice.setStyleSheet(
            "background:#fffbeb;border:1px solid #fde68a;border-radius:4px;"
            "color:#92400e;padding:6px 8px;")
        self.lbl_notice.hide()
        v.addWidget(self.lbl_notice)

        self.detail_cards = {}
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        for idx, (key, label) in enumerate((
            ("status", "结论"), ("total", "Part 总数"), ("normal", "正常"),
            ("abnormal", "异常"), ("errors", "执行异常"),
            ("pass_s", "合格时长"), ("fail_s", "不合格时长"),
            ("time", "登记时间"),
        )):
            card = QFrame()
            card.setObjectName("statCard")
            cv = QVBoxLayout(card)
            cv.setContentsMargins(10, 8, 10, 8)
            val = QLabel("-")
            val.setObjectName("statValue")
            cap = QLabel(label)
            cap.setObjectName("statLabel")
            cv.addWidget(val)
            cv.addWidget(cap)
            self.detail_cards[key] = val
            grid.addWidget(card, 0, idx)
        v.addLayout(grid)

        self.detail_sub = QTabWidget()
        v.addWidget(self.detail_sub, stretch=1)

        # Part 明细
        self.part_table = QTableWidget(0, 0)
        self.part_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.part_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.part_table.setAlternatingRowColors(True)
        self.part_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.part_table.horizontalHeader().setStretchLastSection(True)
        self.detail_sub.addTab(self.part_table, "Part 明细")

        # 来源文件
        self.file_box = QPlainTextEdit()
        self.file_box.setReadOnly(True)
        self.detail_sub.addTab(self.file_box, "来源文件")

        # 备注
        note_page = QWidget()
        nv = QVBoxLayout(note_page)
        nv.setContentsMargins(0, 8, 0, 0)
        self.ed_note = QPlainTextEdit()
        self.ed_note.setPlaceholderText("给这次分析写备注，例如：现场 A 机位，左腕带电池不足")
        nv.addWidget(self.ed_note, stretch=1)
        nbtns = QHBoxLayout()
        self.btn_save_note = QPushButton("保存备注")
        self.btn_save_note.setProperty("role", "primary")
        self.btn_save_note.clicked.connect(self._save_note)
        nbtns.addWidget(self.btn_save_note)
        nbtns.addStretch(1)
        nv.addLayout(nbtns)
        self.detail_sub.addTab(note_page, "备注")

        self._current_id = ""
        return page

    # ------------------------------------------------------------- 数据加载
    def reload(self) -> None:
        """从登记簿重新读取并按当前筛选条件渲染。"""
        try:
            self._rows = history.load_sessions()
        except history.HistoryError as exc:
            self.log(f"读取会话记录失败：{exc}")
            self._rows = []
        history.prune_missing(self._rows)
        self._populate_devices_hint()
        rows = self._filtered()
        self._fill_table(rows)
        total = len(self._rows)
        self.lbl_empty.setVisible(not rows)
        if total and not rows:
            self.lbl_empty.setText("当前筛选条件下没有记录，试试清空搜索或切换结论。")
        elif not total:
            self.lbl_empty.setText(
                "暂无会话记录。在「日志分析」页完成一次分析后，这里会自动出现一条记录。")

    def _populate_devices_hint(self) -> None:
        devices = []
        for row in self._rows:
            for name in row.get("device_labels") or []:
                if name not in devices:
                    devices.append(name)
        self.ed_search.setPlaceholderText(
            "搜索 / 设备：" + "、".join(devices[:6]) + ("…" if len(devices) > 6 else "")
            if devices else "搜索设备 / 数据目录 / 备注")

    def _filtered(self) -> list:
        text = self.ed_search.text().strip().lower()
        status = self.cmb_status.currentData() or ""
        source = self.cmb_source.currentData() or ""
        only_missing = self.chk_missing.isChecked()
        out = []
        for row in self._rows:
            if status and row.get("status") != status:
                continue
            if source:
                in_exports = _is_in_exports(row.get("root"))
                if (source == "export") != in_exports:
                    continue
            if only_missing and not row.get("_html_missing"):
                continue
            if text:
                hay = " ".join([
                    str(row.get("root") or ""), str(row.get("html_path") or ""),
                    str(row.get("note") or ""), str(row.get("created_at") or ""),
                    " ".join(row.get("device_labels") or []),
                ]).lower()
                if text not in hay:
                    continue
            out.append(row)
        return out

    def _fill_table(self, rows: list) -> None:
        self.table.setRowCount(len(rows))
        for ri, row in enumerate(rows):
            summary = row.get("summary") or {}
            status = row.get("status") or ""
            time_text = _fmt_time(row.get("created_at"))
            if row.get("_html_missing"):
                time_text += "（报告缺失）"
            parts = summary.get("total", "-")
            abnormal = summary.get("abnormal", "-")
            report = "-" if not row.get("html_path") else (
                "缺失" if row.get("_html_missing") else "可用")
            devices = "、".join(row.get("device_labels") or []) or "-"
            root = str(row.get("root") or "-")
            values = [
                time_text, STATUS_TEXT.get(status, status or "-"),
                str(parts), str(abnormal), devices, root, report,
                str(row.get("note") or ""),
            ]
            for ci, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if ci == 0:
                    item.setData(Qt.UserRole, row.get("id"))
                if ci == 1:
                    fg, bg = STATUS_COLOR.get(status, ("#374151", "#ffffff"))
                    item.setForeground(QBrush(QColor(fg)))
                    item.setBackground(QBrush(QColor(bg)))
                    item.setTextAlignment(Qt.AlignCenter)
                if ci == 3 and isinstance(abnormal, int) and abnormal > 0:
                    item.setForeground(QBrush(QColor("#b91c1c")))
                if ci in (2, 3):
                    item.setTextAlignment(Qt.AlignCenter)
                if ci == 6 and row.get("_html_missing"):
                    item.setForeground(QBrush(QColor("#b45309")))
                if row.get("_root_missing"):
                    item.setForeground(QBrush(QColor("#9ca3af")))
                self.table.setItem(ri, ci, item)
        self._sync_tool_buttons()

    def _sync_tool_buttons(self) -> None:
        n = len(self._selected_rows())
        self._tool_compare.setEnabled(n == 2)
        self._tool_delete.setEnabled(n >= 1)

    # ------------------------------------------------------------- 列表操作
    def _selected_rows(self) -> list:
        ids = []
        for idx in self.table.selectionModel().selectedRows() if self.table.selectionModel() else []:
            item = self.table.item(idx.row(), 0)
            if item is not None and item.data(Qt.UserRole):
                ids.append(item.data(Qt.UserRole))
        if not ids and self.table.currentRow() >= 0:
            item = self.table.item(self.table.currentRow(), 0)
            if item is not None and item.data(Qt.UserRole):
                ids.append(item.data(Qt.UserRole))
        return ids

    def _row_by_id(self, session_id):
        for row in self._rows:
            if row.get("id") == session_id:
                return row
        return None

    def _show_context_menu(self, pos) -> None:
        menu = QMenu(self)
        menu.addAction("打开详情", self._open_selected)
        menu.addAction("打开报告", self._open_html)
        menu.addAction("打开数据目录", self._open_root)
        menu.addAction("在文件夹中显示报告", self._reveal_selected)
        menu.addSeparator()
        menu.addAction("按此目录重新分析", self._reanalyze)
        menu.addSeparator()
        menu.addAction("删除记录", lambda: self._delete_selected(False))
        menu.addAction("删除记录并删除报告文件",
                       lambda: self._delete_selected(True))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _open_selected(self) -> None:
        ids = self._selected_rows()
        if not ids:
            return
        row = self._row_by_id(ids[0])
        if row:
            self._show_detail(row)

    def _show_detail(self, row) -> None:
        self._current_id = row.get("id") or ""
        summary = row.get("summary") or {}
        status = row.get("status") or ""
        self.lbl_detail_title.setText(
            f"{_fmt_time(row.get('created_at'))} ｜ {_basename(row.get('root'))}")
        cards = self.detail_cards
        cards["status"].setText(STATUS_TEXT.get(status, status or "-"))
        fg, _bg = STATUS_COLOR.get(status, ("#222222", "#ffffff"))
        cards["status"].setStyleSheet(f"font-size:20px;font-weight:bold;color:{fg};")
        cards["total"].setText(str(summary.get("total", "-")))
        cards["normal"].setText(str(summary.get("normal", "-")))
        cards["abnormal"].setText(str(summary.get("abnormal", "-")))
        cards["errors"].setText(str(summary.get("errors", "-")))
        cards["pass_s"].setText(f"{summary.get('pass_s', '-')} s")
        cards["fail_s"].setText(f"{summary.get('fail_s', '-')} s")
        cards["time"].setText(_fmt_time(row.get("created_at")))

        notices = []
        if row.get("_html_missing"):
            notices.append(f"报告文件已不存在：{row.get('html_path') or '（未记录）'}")
        if row.get("_root_missing"):
            notices.append(f"数据目录已不存在：{row.get('root') or '（未记录）'}")
        self.lbl_notice.setText("；".join(notices))
        self.lbl_notice.setVisible(bool(notices))

        self._fill_parts(row.get("parts") or [])
        self.file_box.setPlainText("\n".join([
            f"数据目录：{row.get('root') or '-'}",
            f"输出目录：{row.get('out_root') or '-'}",
            f"报告文件：{row.get('html_path') or '-'}",
            f"设备：{'、'.join(row.get('device_labels') or []) or '-'}",
            f"记录更新：{_fmt_time(row.get('updated_at'))}",
        ]))
        self.ed_note.setPlainText(str(row.get("note") or ""))
        self.btn_open_html.setEnabled(bool(row.get("html_path"))
                                      and not row.get("_html_missing"))
        self.btn_reanalyze.setEnabled(bool(row.get("root")) and not row.get("_root_missing"))
        self.detail_sub.setCurrentIndex(0)
        self.stack.setCurrentIndex(1)

    def _fill_parts(self, parts: list) -> None:
        headers = ["Part 名称", "数据文件夹", "结论", "相机", "设备", "帧率", "时长(s)", "固件"]
        self.part_table.clear()
        self.part_table.setColumnCount(len(headers))
        self.part_table.setRowCount(len(parts))
        self.part_table.setHorizontalHeaderLabels(headers)
        for ri, part in enumerate(parts):
            status = str(part.get("status") or "")
            values = [
                str(part.get("name") or "-"),
                str(part.get("data_folder") or "-"),
                status or "-",
                str(part.get("cameras") or "-"),
                "、".join(part.get("devices") or []) or "-",
                f"{part.get('fps', 0):.1f}" if part.get("fps") else "-",
                str(part.get("duration_s", "-")),
                str(part.get("firmware") or "-"),
            ]
            for ci, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if ci == 2:
                    if status == "PASS":
                        item.setForeground(QBrush(QColor("#059669")))
                    elif status == "FAIL":
                        item.setForeground(QBrush(QColor("#dc2626")))
                    elif status == "ERROR":
                        item.setForeground(QBrush(QColor("#b91c1c")))
                self.part_table.setItem(ri, ci, item)

    def _show_list(self) -> None:
        self.stack.setCurrentIndex(0)
        self.reload()

    def _open_html(self) -> None:
        row = self._current_row_for_action()
        if not row:
            return
        path = str(row.get("html_path") or "")
        if not path or not Path(path).is_file():
            QMessageBox.information(self, "报告不可用",
                                    "该记录对应的报告文件已不存在（可能已被移动或删除）。")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _open_root(self) -> None:
        row = self._current_row_for_action()
        if not row:
            return
        root = str(row.get("root") or "")
        if not root or not Path(root).is_dir():
            QMessageBox.information(self, "目录不可用", "该记录对应的数据目录已不存在。")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(root))

    def _reveal_selected(self) -> None:
        row = self._current_row_for_action()
        if not row:
            return
        path = str(row.get("html_path") or "")
        target = path if path and Path(path).exists() else str(row.get("out_root") or "")
        if target and Path(target).exists():
            if Path(target).is_file():
                import subprocess
                subprocess.Popen(["explorer", "/select,", str(Path(target))])
                return
            QDesktopServices.openUrl(QUrl.fromLocalFile(target))
            return
        self._open_root()

    def _reanalyze(self) -> None:
        row = self._current_row_for_action()
        if not row:
            return
        root = str(row.get("root") or "")
        if not root or not Path(root).is_dir():
            QMessageBox.information(self, "目录不可用", "该记录对应的数据目录已不存在。")
            return
        self.open_analysis_requested.emit(root)

    def _current_row_for_action(self):
        if self.stack.currentIndex() == 1 and self._current_id:
            return self._row_by_id(self._current_id)
        ids = self._selected_rows()
        return self._row_by_id(ids[0]) if ids else None

    def _save_note(self) -> None:
        if not self._current_id:
            return
        text = self.ed_note.toPlainText().strip()
        try:
            history.update_session(self._current_id, note=text)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self.status_message.emit("备注已保存")

    def _delete_selected(self, delete_files: bool = False) -> None:
        ids = self._selected_rows()
        if not ids:
            return
        rows = [self._row_by_id(i) for i in ids]
        rows = [r for r in rows if r]
        detail = "、".join(_fmt_time(r.get("created_at")) for r in rows[:3])
        if len(rows) > 3:
            detail += f" 等 {len(rows)} 条"
        extra = ("\n\n注意：将同时删除这些记录对应的报告 HTML 文件。"
                 if delete_files else "\n\n报告与数据文件都会保留，只删登记记录。")
        ret = QMessageBox.question(
            self, "删除会话记录",
            f"确定删除 {detail} 的会话记录吗？{extra}\n\n数据目录（原始采集数据）永远不会被删除。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        for row in rows:
            try:
                history.delete_session(row.get("id"), delete_files=delete_files)
            except history.HistoryError as exc:
                QMessageBox.warning(self, "删除失败", str(exc))
                break
        self._show_list()

    def _compare_selected(self) -> None:
        ids = self._selected_rows()
        if len(ids) != 2:
            QMessageBox.information(self, "对比", "请先在列表中选中两条记录（Ctrl 多选）。")
            return
        rows = [self._row_by_id(i) for i in ids]
        rows = [r for r in rows if r]
        if len(rows) != 2:
            return
        _CompareDialog(rows, self).exec()

    # ------------------------------------------------------------- 外部入口
    def show_session(self, session_id: str) -> None:
        """从其它页跳转过来并直接打开某条记录。"""
        self.reload()
        row = self._row_by_id(session_id)
        if row:
            self._show_detail(row)

    def refresh_silently(self) -> None:
        """其它页写入记录后刷新；停在列表页时不打扰，详情页保留当前记录。"""
        if self.stack.currentIndex() == 0:
            self.reload()
        else:
            self._rows = history.load_sessions()
            history.prune_missing(self._rows)


class _CompareDialog(QDialog):
    """两条记录并排对比：统计字段两列 + 每个 Part 的结论差异高亮。"""

    def __init__(self, rows, parent=None):
        super().__init__(parent)
        self.setWindowTitle("会话对比")
        self.resize(900, 600)
        v = QVBoxLayout(self)
        left, right = rows[0], rows[1]
        v.addWidget(QLabel(
            f"左：{_fmt_time(left.get('created_at'))} ｜ {_basename(left.get('root'))}　"
            f"右：{_fmt_time(right.get('created_at'))} ｜ {_basename(right.get('root'))}"))
        fields = [
            ("结论", lambda r: STATUS_TEXT.get(r.get("status"), r.get("status") or "-")),
            ("数据目录", lambda r: str(r.get("root") or "-")),
            ("设备", lambda r: "、".join(r.get("device_labels") or []) or "-"),
            ("Part 总数", lambda r: str((r.get("summary") or {}).get("total", "-"))),
            ("正常", lambda r: str((r.get("summary") or {}).get("normal", "-"))),
            ("异常", lambda r: str((r.get("summary") or {}).get("abnormal", "-"))),
            ("执行异常", lambda r: str((r.get("summary") or {}).get("errors", "-"))),
            ("合格时长(s)", lambda r: str((r.get("summary") or {}).get("pass_s", "-"))),
            ("不合格时长(s)", lambda r: str((r.get("summary") or {}).get("fail_s", "-"))),
            ("备注", lambda r: str(r.get("note") or "-")),
        ]
        tbl = QTableWidget(len(fields), 3)
        tbl.setHorizontalHeaderLabels(["字段", "左侧", "右侧"])
        tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        tbl.verticalHeader().setVisible(False)
        for ri, (label, getter) in enumerate(fields):
            lv, rv = getter(left), getter(right)
            tbl.setItem(ri, 0, QTableWidgetItem(label))
            for ci, val in ((1, lv), (2, rv)):
                item = QTableWidgetItem(val)
                item.setToolTip(val)
                if lv != rv:
                    item.setForeground(QBrush(QColor("#b91c1c")))
                tbl.setItem(ri, ci, item)
        tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        tbl.horizontalHeader().setStretchLastSection(True)
        v.addWidget(tbl, stretch=2)

        # Part 级差异：同名 Part 结论不同的单独列出
        diff = _part_status_diff(left.get("parts") or [], right.get("parts") or [])
        cap = QLabel(f"Part 结论差异：{len(diff)} 处" if diff else "Part 结论一致")
        cap.setStyleSheet("font-weight:600;")
        v.addWidget(cap)
        diff_table = QTableWidget(len(diff), 3)
        diff_table.setHorizontalHeaderLabels(["Part", "左侧", "右侧"])
        diff_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        for ri, (name, ls, rs) in enumerate(diff):
            diff_table.setItem(ri, 0, QTableWidgetItem(name))
            diff_table.setItem(ri, 1, QTableWidgetItem(ls or "-"))
            diff_table.setItem(ri, 2, QTableWidgetItem(rs or "-"))
        diff_table.horizontalHeader().setStretchLastSection(True)
        v.addWidget(diff_table, stretch=1)

        btn = QPushButton("关闭")
        btn.clicked.connect(self.accept)
        v.addWidget(btn, alignment=Qt.AlignRight)


def _part_status_diff(left_parts, right_parts):
    left_map = {p.get("name"): p.get("status") for p in left_parts}
    right_map = {p.get("name"): p.get("status") for p in right_parts}
    out = []
    for name in sorted(set(left_map) | set(right_map)):
        ls, rs = left_map.get(name), right_map.get(name)
        if ls != rs:
            out.append((str(name or "-"), ls, rs))
    return out


def _is_in_exports(path) -> bool:
    text = str(path or "")
    if not text:
        return False
    exports = str(Path(history.config.EXPORT_DIR).resolve())
    try:
        resolved = str(Path(text).resolve())
    except OSError:
        return False
    try:
        return os.path.commonpath([resolved, exports]) == exports
    except ValueError:
        return False
