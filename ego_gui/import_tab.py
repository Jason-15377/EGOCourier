"""导入 Tab：选择本地目录 / zip 日志包，解析并对齐分析，可在会话记录中打开。

与 Web 导入页一致：选择目录… / 选择 zip… + 导入并分析 → 结果显示来源文件与统计摘要，
并提供「在会话记录中打开」跳转到对应会话详情。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QColor, QBrush
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QFileDialog, QTableWidget,
    QTableWidgetItem, QHeaderView, QLabel, QMessageBox,
)

from ego_relay import analysis, parsers
from .sessions_tab import _fmt


class ImportWorker(QThread):
    progressed = Signal(int, int, int)
    finished_ok = Signal(int)
    failed = Signal(str)
    file_parsed = Signal(str, str, int, str)

    def __init__(self, path: str):
        super().__init__()
        self.path = path

    def run(self):
        try:
            parsed = parsers.import_path(self.path)
            total = len(parsed.files)
            succ = sum(1 for f in parsed.files if f.get("parsed"))
            fail = total - succ
            for f in parsed.files:
                self.file_parsed.emit(f.get("file", ""), f.get("source_type", ""),
                                      f.get("count", 0), f.get("note") or "")
            self.progressed.emit(total, succ, fail)
            r = analysis.align_and_analyze(parsed)
            self.finished_ok.emit(r["session_id"])
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"{type(e).__name__}: {e}")


class ImportTab(QWidget):
    """导入页。"""
    imported = Signal(int)   # 导入成功发出 session_id，供主窗口切到会话记录并打开

    def __init__(self, log):
        super().__init__()
        self.log = log
        self.worker: ImportWorker | None = None
        self._path = ""
        self._last_sid: int | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # 顶栏
        top = QHBoxLayout()
        self.btn_pick_dir = QPushButton("选择目录…")
        self.btn_pick_dir.clicked.connect(lambda: self._pick("dir"))
        self.btn_pick_zip = QPushButton("选择 zip…")
        self.btn_pick_zip.clicked.connect(lambda: self._pick("zip"))
        self.btn_import = QPushButton("导入并分析")
        self.btn_import.setEnabled(False)
        self.btn_import.setProperty("role", "primary")
        self.btn_import.clicked.connect(self._import)
        self.btn_open_sess = QPushButton("在会话记录中打开")
        self.btn_open_sess.setEnabled(False)
        self.btn_open_sess.clicked.connect(self._open_in_sessions)
        top.addWidget(self.btn_pick_dir)
        top.addWidget(self.btn_pick_zip)
        top.addWidget(self.btn_import)
        top.addStretch(1)
        top.addWidget(self.btn_open_sess)
        root.addLayout(top)

        # 状态行
        st = QHBoxLayout()
        st.addWidget(QLabel("总任务"))
        self.lbl_total = QLabel("0")
        st.addWidget(self.lbl_total)
        st.addWidget(QLabel("成功"))
        self.lbl_ok = QLabel("0")
        st.addWidget(self.lbl_ok)
        st.addWidget(QLabel("失败"))
        self.lbl_fail = QLabel("0")
        st.addWidget(self.lbl_fail)
        st.addWidget(QLabel("状态"))
        self.lbl_state = QLabel("空闲")
        st.addWidget(self.lbl_state)
        st.addStretch(1)
        root.addLayout(st)

        # 来源文件表
        root.addWidget(QLabel("来源文件"))
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["文件", "类型", "行数", "结果", "说明"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setAlternatingRowColors(True)
        root.addWidget(self.table, stretch=1)

        # 导入结果摘要
        root.addWidget(QLabel("导入结果"))
        self.lbl_result = QLabel("请选择日志目录或 zip 后点击「导入并分析」。")
        self.lbl_result.setWordWrap(True)
        root.addWidget(self.lbl_result)

    def _pick(self, mode):
        if mode == "dir":
            path = QFileDialog.getExistingDirectory(self, "选择本地日志文件夹")
        else:
            path, _ = QFileDialog.getOpenFileName(self, "选择 zip 日志包",
                                                  "", "ZIP 压缩包 (*.zip)")
        if path:
            self._path = path
            self.btn_import.setEnabled(True)
            self.log(f"已选择: {path}")

    def _import(self):
        if not self._path:
            return
        self.log(f"开始导入: {self._path}")
        self.lbl_state.setText("运行中")
        self.lbl_result.setText("导入中…")
        self.table.setRowCount(0)
        self.btn_import.setEnabled(False)
        self.btn_open_sess.setEnabled(False)
        self.worker = ImportWorker(self._path)
        self.worker.file_parsed.connect(self._on_file)
        self.worker.progressed.connect(self._on_progress)
        self.worker.finished_ok.connect(self._on_done)
        self.worker.failed.connect(self._on_fail)
        self.worker.start()

    def _on_file(self, file, kind, count, note):
        r = self.table.rowCount()
        self.table.insertRow(r)
        p = Path(file)
        self.table.setItem(r, 0, QTableWidgetItem(p.name))
        self.table.setItem(r, 1, QTableWidgetItem(kind or p.suffix.lstrip(".").upper()))
        self.table.setItem(r, 2, QTableWidgetItem(str(count)))
        if note:
            mark, tip = "❌ 异常", note
        elif count and kind:
            mark, tip = "✅ 正常", "已解析"
        else:
            mark, tip = "⚠️ 警告", "解析得到 0 行或无有效来源"
        it = QTableWidgetItem(mark)
        it.setToolTip(tip)
        if mark.startswith("❌"):
            it.setBackground(QBrush(QColor("#fdecea")))
        elif mark.startswith("⚠️"):
            it.setBackground(QBrush(QColor("#fff7e6")))
        self.table.setItem(r, 3, it)
        self.table.setItem(r, 4, QTableWidgetItem(tip))

    def _on_progress(self, total, ok, fail):
        self.lbl_total.setText(str(total))
        self.lbl_ok.setText(str(ok))
        self.lbl_fail.setText(str(fail))

    def _on_done(self, sid):
        self._last_sid = sid
        self.lbl_state.setText("完成")
        self.btn_import.setEnabled(True)
        self.btn_open_sess.setEnabled(True)
        self.log(f"导入完成: 会话 #{sid}")
        self.lbl_result.setText(f"导入完成：会话 #{sid}，可在下方「在会话记录中打开」查看详情。")
        self.imported.emit(sid)

    def _on_fail(self, err):
        self.lbl_state.setText("失败")
        self.btn_import.setEnabled(True)
        self.log(f"导入失败: {err}")
        self.lbl_result.setText(f"导入失败：{err}")

    def _open_in_sessions(self):
        if self._last_sid is not None:
            self.imported.emit(self._last_sid)
