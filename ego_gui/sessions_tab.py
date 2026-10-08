"""会话记录 Tab：会话列表 + 会话详情面板（与 Web 会话记录页一致）。

- 会话列表：ID / 设备 / 名称 / 帧 / 异常(badge) / trace_id / 开始时间。
- 双击或选中点「打开」→ 切到详情面板（列表↔详情，类似 Web 的 overlay + 返回）。
- 详情面板：统计卡片 + 概览(图表/同步区间) / 帧对齐表 / 异常列表 / 来源文件 四个子页
  + 工具栏（关联 app 日志 / 导出报告 / 导出 zip）。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QBrush, QPixmap
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QFileDialog, QTableWidget,
    QTableWidgetItem, QHeaderView, QLabel, QComboBox, QSplitter, QPlainTextEdit,
    QFrame, QMessageBox, QTabWidget, QCheckBox, QStackedWidget, QApplication,
    QAbstractItemView, QDialog, QGridLayout, QScrollArea,
)
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg

from ego_relay import analysis, db, config, export as export_mod
from ego_relay import compare as compare_mod
from . import charts as charts_mod
from . import report as report_mod


def _fmt(us):
    if not us:
        return "-"
    return (datetime.fromtimestamp(us / 1e6, tz=timezone.utc)
            .astimezone().strftime("%Y-%m-%d %H:%M:%S"))


def _fmt_ms(us):
    return "-" if us is None else f"{us / 1000.0:.1f}"


def _pct(r):
    return f"{r * 100:.3f}%"


def _badge_item(text, good):
    """异常计数 badge：绿色 0 / 红色 >0（对齐 Web 的 badge.err/badge.ok）。"""
    it = QTableWidgetItem(text)
    if good:
        it.setForeground(QBrush(QColor("#047857")))
        it.setBackground(QBrush(QColor("#ecfdf5")))
    else:
        it.setForeground(QBrush(QColor("#b91c1c")))
        it.setBackground(QBrush(QColor("#fdecea")))
    it.setTextAlignment(Qt.AlignCenter)
    return it


class SessionDetail(QWidget):
    """单个会话的分析详情面板（统计卡 + 概览/帧对齐表/异常列表/来源文件 + 导出）。"""

    def __init__(self, log):
        super().__init__()
        self.log = log
        self.session_id: int | None = None
        self._frames: list = []
        self._stats: dict = {}
        self._segments: list = []
        self._figure = None
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        # 工具按钮：由 SessionsTab 的顶栏统一放置，与「← 返回列表」同一行
        self.btn_attach = QPushButton("关联 app 日志")
        self.btn_attach.clicked.connect(self._attach_log)
        self.btn_report = QPushButton("导出报告")
        self.btn_report.clicked.connect(self._export_report)
        self.btn_zip = QPushButton("导出 zip")
        self.btn_zip.clicked.connect(self._export_zip)

        # 统计卡片
        self._build_cards(root)

        # 子页
        self.sub = QTabWidget()
        self._build_overview_tab()
        self._build_frames_tab()
        self._build_anomaly_tab()
        self._build_files_tab()
        root.addWidget(self.sub, stretch=1)

    def _build_cards(self, root):
        # 卡片用 QGridLayout 按可用宽度动态换行：宽度够就多放几张，不够就换行。
        # QGridLayout 是标准布局，高度计算可靠，最下面一行不会被裁剪。
        self._cards = []
        self._card_grid = QGridLayout()
        self._card_grid.setContentsMargins(0, 0, 0, 0)
        self._card_grid.setHorizontalSpacing(10)
        self._card_grid.setVerticalSpacing(8)
        self.cards = {}
        specs = [
            ("dev", "设备SN"), ("start", "开始时间"), ("end", "结束时间"),
            ("frames", "总样本数"), ("imu_drop", "IMU丢点"), ("imu_ratio", "IMU丢点占比"),
            ("vid_drop", "视频丢帧"), ("vid_ratio", "视频丢帧占比"),
            ("ptp_seg", "PTP异常片段"), ("trace_ok", "traceID匹配"),
            ("drop_rate", "丢帧率"), ("stereo_avg", "左右同步均值(μs)"), ("imuv_avg", "IMU-视频均值(μs)"),
        ]
        for key, label in specs:
            card = QFrame(); card.setObjectName("statCard")
            v = QVBoxLayout(card); v.setContentsMargins(10, 8, 10, 8)
            val = QLabel("0" if key == "frames" else "-")
            val.setObjectName("statValue")
            cap = QLabel(label); cap.setObjectName("statLabel")
            v.addWidget(val); v.addWidget(cap)
            self._cards.append(card)
            self.cards[key] = val
        root.addLayout(self._card_grid)
        self._relayout_cards()

    def _relayout_cards(self) -> None:
        """按当前可用宽度把 13 张卡片重新排进 QGridLayout（放不下就换行）。"""
        if not getattr(self, "_cards", None):
            return
        grid = self._card_grid
        avail = max(120, self.width() - 4)
        # 清空网格（takeAt 不移除 widget，卡片可重新 addWidget）
        while grid.count():
            grid.takeAt(0)
        spacing = grid.horizontalSpacing()
        row = col = 0
        x = 0
        for card in self._cards:
            w = card.sizeHint().width()
            if col > 0 and x + w > avail:
                row += 1
                col = 0
                x = 0
            grid.addWidget(card, row, col)
            x += w + spacing
            col += 1

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relayout_cards()

    def _build_overview_tab(self):
        # 概览内容放进纵向滚动容器：窗口高度不足时也能滚动看全图表和下方表格
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        v = QVBoxLayout(inner)
        v.setContentsMargins(0, 0, 0, 0)
        self._canvas_holder = QVBoxLayout()
        self._canvas_holder.setContentsMargins(0, 0, 0, 0)
        v.addLayout(self._canvas_holder)
        self._set_canvas(charts_mod.build_figure([], {}))

        split = QSplitter(Qt.Horizontal)
        seg_frame = QFrame(); seg_frame.setFrameShape(QFrame.StyledPanel)
        seg_v = QVBoxLayout(seg_frame)
        seg_v.addWidget(QLabel("同步异常区间（超阈值片段，选中查看 trace 详情）"))
        self.seg_table = QTableWidget(0, 4)
        self.seg_table.setHorizontalHeaderLabels(["起始时间", "结束时间", "最大偏移(ms)", "归属traceID"])
        # 列宽按内容自适应，避免「起始时间/结束时间/最大偏移(ms)」表头被截断
        self.seg_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.seg_table.setAlternatingRowColors(True)
        seg_v.addWidget(self.seg_table)
        split.addWidget(seg_frame)

        trace_frame = QFrame(); trace_frame.setFrameShape(QFrame.StyledPanel)
        tv = QVBoxLayout(trace_frame)
        tv.addWidget(QLabel("traceID 关联详情（三类日志原始片段预览）"))
        self.trace_info = QLabel("未选择")
        self.trace_info.setWordWrap(True)
        tv.addWidget(self.trace_info)
        self.trace_preview = QPlainTextEdit()
        self.trace_preview.setReadOnly(True)
        tv.addWidget(self.trace_preview, stretch=1)
        split.addWidget(trace_frame)
        split.setSizes([520, 460])
        v.addWidget(split, stretch=1)

        scroll.setWidget(inner)
        tab = QWidget()
        tl = QVBoxLayout(tab)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(scroll)

        self.seg_table.itemSelectionChanged.connect(self._on_segment_selected)
        self.sub.addTab(tab, "概览")

    def _build_frames_tab(self):
        tab = QWidget()
        v = QVBoxLayout(tab)
        flt = QHBoxLayout()
        flt.addWidget(QLabel("侧"))
        self.combo_side = QComboBox()
        self.combo_side.addItems(["全部", "left", "right"])
        self.combo_side.currentIndexChanged.connect(self._load_frames)
        flt.addWidget(self.combo_side)
        self.chk_anom = QCheckBox("仅异常帧")
        self.chk_anom.toggled.connect(self._load_frames)
        flt.addWidget(self.chk_anom)
        flt.addStretch(1)
        self.lbl_frames_count = QLabel("")
        flt.addWidget(self.lbl_frames_count)
        v.addLayout(flt)
        self.frame_table = QTableWidget(0, 7)
        self.frame_table.setHorizontalHeaderLabels(
            ["帧号", "侧", "hw_ptp(us)", "hw_ptp 本地", "sei(us)", "app_recv(us)", "imu(us)"])
        self.frame_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.frame_table.setAlternatingRowColors(True)
        v.addWidget(self.frame_table, stretch=1)
        self.sub.addTab(tab, "帧对齐表")

    def _build_anomaly_tab(self):
        tab = QWidget()
        v = QVBoxLayout(tab)
        self.anom_table = QTableWidget(0, 7)
        self.anom_table.setHorizontalHeaderLabels(
            ["规则", "帧", "侧", "偏差(μs)", "阈值(μs)", "级别", "截帧"])
        self.anom_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.anom_table.setAlternatingRowColors(True)
        v.addWidget(self.anom_table, stretch=1)
        self.sub.addTab(tab, "异常列表")

    def _build_files_tab(self):
        tab = QWidget()
        v = QVBoxLayout(tab)
        self.file_table = QTableWidget(0, 5)
        self.file_table.setHorizontalHeaderLabels(["文件", "类型", "行数", "结果", "说明"])
        self.file_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.file_table.setAlternatingRowColors(True)
        v.addWidget(self.file_table, stretch=1)
        self.sub.addTab(tab, "来源文件")

    # ------------------------------------------------------------------ 加载
    def load_session(self, session_id: int) -> None:
        self.session_id = session_id
        s = db.get_session(session_id)
        if not s:
            self.log(f"会话 #{session_id} 不存在")
            return
        meta = s.get("meta") or {}
        stats = meta.get("stats", {})
        dev = db.query_one("SELECT serial FROM devices WHERE id=?", (s.get("device_id"),))
        self._stats = stats
        self._frames = charts_mod.load_left_frames(session_id)
        self._segments = stats.get("ptp_anomaly_segments", [])

        # 卡片
        self.cards["dev"].setText((dev or {}).get("serial") or "-")
        self.cards["start"].setText(_fmt(s.get("start_us")))
        self.cards["end"].setText(_fmt(s.get("end_us")))
        self.cards["frames"].setText(str(stats.get("total_frames", 0)))
        self.cards["imu_drop"].setText(str(stats.get("imu", {}).get("dropped", 0)))
        self.cards["imu_ratio"].setText(_pct(stats.get("imu", {}).get("ratio", 0)))
        self.cards["vid_drop"].setText(str(stats.get("video", {}).get("dropped", 0)))
        self.cards["vid_ratio"].setText(_pct(stats.get("video", {}).get("ratio", 0)))
        self.cards["ptp_seg"].setText(str(len(self._segments)))
        self.cards["trace_ok"].setText(str(stats.get("trace_matched", 0)))
        sc = stats.get("script", {})
        rates = [v.get("drop_rate", 0) for v in sc.get("streams", {}).values()]
        self.cards["drop_rate"].setText(_pct(max(rates)) if rates else "-")
        self.cards["stereo_avg"].setText(f"{sc.get('stereo_sync', {}).get('avg_diff_us', 0):.1f}")
        self.cards["imuv_avg"].setText(f"{sc.get('imu_video_sync', {}).get('avg_off_us', 0):.1f}")

        # 图表
        thr_ms = stats.get("seg_threshold_us", 50000) / 1000.0
        self._figure = charts_mod.build_figure(self._frames, stats, thr_ms=thr_ms)
        self._set_canvas(self._figure)

        # 异常区间
        self.seg_table.setRowCount(0)
        for x in self._segments:
            r = self.seg_table.rowCount()
            self.seg_table.insertRow(r)
            self.seg_table.setItem(r, 0, QTableWidgetItem(_fmt(x.get("start_us"))))
            self.seg_table.setItem(r, 1, QTableWidgetItem(_fmt(x.get("end_us"))))
            self.seg_table.setItem(r, 2, QTableWidgetItem(_fmt_ms(x.get("max_us"))))
            self.seg_table.setItem(r, 3, QTableWidgetItem(str(x.get("trace") or "-")))

        # 来源文件（从 meta.files）
        self._load_files(s)

        self._load_frames()
        self._load_anomalies()
        self._relayout_cards()
        self.log(f"已加载会话 #{session_id}: 帧={stats.get('total_frames', 0)} "
                 f"异常片段={len(self._segments)}")

    def _load_files(self, session):
        files = (session.get("meta") or {}).get("files", [])
        self.file_table.setRowCount(0)
        for f in files:
            r = self.file_table.rowCount()
            self.file_table.insertRow(r)
            self.file_table.setItem(r, 0, QTableWidgetItem(Path(f.get("file", "")).name))
            self.file_table.setItem(r, 1, QTableWidgetItem(f.get("source_type", "")))
            self.file_table.setItem(r, 2, QTableWidgetItem(str(f.get("count", ""))))
            ok = bool(f.get("parsed"))
            self.file_table.setItem(r, 3, QTableWidgetItem("✅ 正常" if ok else "⚠️ 警告"))
            self.file_table.setItem(r, 4, QTableWidgetItem(f.get("note") or ""))

    def _load_frames(self):
        if not self.session_id:
            return
        side = self.combo_side.currentText()
        side = None if side == "全部" else side
        only = self.chk_anom.isChecked()
        frames = db.session_frames(self.session_id, side=side)
        if only:
            keys = {(a["frame_index"], a["side"]) for a in db.session_anomalies(self.session_id)
                    if a["side"] != "frame"}
            fids = {a["frame_index"] for a in db.session_anomalies(self.session_id)
                    if a["side"] == "frame"}
            frames = [f for f in frames
                      if (f["frame_index"], f["side"]) in keys or f["frame_index"] in fids]
        self.frame_table.setRowCount(0)
        for f in frames:
            r = self.frame_table.rowCount()
            self.frame_table.insertRow(r)
            vals = [f["frame_index"], f["side"], f["hw_ptp_us"] or "", _fmt(f["hw_ptp_us"]),
                    f["sei_hw_ptp_us"] or "", f["app_recv_us"] or "", f["imu_us"] or ""]
            for c, v in enumerate(vals):
                self.frame_table.setItem(r, c, QTableWidgetItem(str(v)))
        self.lbl_frames_count.setText(f"共 {len(frames)} 帧")

    def _load_anomalies(self):
        if not self.session_id:
            return
        anoms = db.session_anomalies(self.session_id)
        self.anom_table.setRowCount(0)
        for a in anoms:
            r = self.anom_table.rowCount()
            self.anom_table.insertRow(r)
            vals = [a["rule"], a["frame_index"], a["side"], a["delta_us"],
                    a["threshold_us"], a["severity"]]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                if c == 5 and v == "error":
                    it.setBackground(QBrush(QColor("#fdecea")))
                elif c == 5 and v == "warn":
                    it.setBackground(QBrush(QColor("#fff7e6")))
                self.anom_table.setItem(r, c, it)
            img = a.get("frame_image")
            if img and os.path.exists(img):
                pm = QPixmap(img).scaled(100, 70, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                lab = QLabel()
                lab.setPixmap(pm)
                self.anom_table.setCellWidget(r, 6, lab)
            else:
                self.anom_table.setItem(r, 6, QTableWidgetItem(""))

    def _set_canvas(self, figure):
        while self._canvas_holder.count():
            item = self._canvas_holder.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        c = FigureCanvasQTAgg(figure)
        c.setMinimumHeight(320)
        self._canvas_holder.addWidget(c)

    # ------------------------------------------------------------------ 区间/trace
    def _on_segment_selected(self):
        sel = self.seg_table.selectedItems()
        if not sel or not self.session_id:
            return
        row = sel[0].row()
        if row >= len(self._segments):
            return
        self._show_trace_detail(self._segments[row])

    def _show_trace_detail(self, seg):
        tid = seg.get("trace")
        self.trace_info.setText(
            f"traceID: {tid or '-'}   区间: {_fmt(seg.get('start_us'))} ~ {_fmt(seg.get('end_us'))}")
        start_us, end_us = seg.get("start_us"), seg.get("end_us")
        if not start_us or not end_us or not self.session_id:
            self.trace_preview.setPlainText("无区间数据")
            return
        try:
            self.trace_preview.setPlainText(self._raw_preview(self.session_id, start_us, end_us))
        except Exception as e:  # noqa: BLE001
            self.trace_preview.setPlainText(f"读取原始片段失败: {e}")

    def _raw_preview(self, session_id, start_us, end_us, max_lines=12):
        from ego_relay.parsers import _WALLCLOCK_RE, wallclock_to_us, to_us, _extract_zip
        s = db.get_session(session_id)
        root = Path(s["source_path"])
        if root.suffix.lower() == ".zip":
            root = _extract_zip(root)
        files = db.query("SELECT * FROM log_files WHERE session_id=?", (session_id,))
        lines = []
        for f in files:
            fp = root / f["file_path"]
            if not fp.exists():
                continue
            stype = f["source_type"]
            keep = []
            try:
                with open(fp, encoding="utf-8-sig", errors="replace") as fh:
                    for ln in fh:
                        ln = ln.rstrip("\n")
                        if stype == "viewer_app":
                            m = _WALLCLOCK_RE.search(ln)
                            if not m:
                                continue
                            t = wallclock_to_us(m.group(1))
                            if t is not None and start_us <= t <= end_us:
                                keep.append(ln)
                        else:
                            parts = ln.split(",")
                            if len(parts) < 2:
                                continue
                            try:
                                t = to_us(float(parts[-1]))
                            except ValueError:
                                continue
                            if start_us <= t <= end_us:
                                keep.append(ln)
            except Exception:
                continue
            if keep:
                lines.append(f"── {stype} ({f['side'] or '-'}) ──")
                lines.extend(keep[:max_lines])
                if len(keep) > max_lines:
                    lines.append(f"…（共 {len(keep)} 行）")
        return "\n".join(lines) if lines else "该区间内未读取到原始行"

    # ------------------------------------------------------------------ 导出 / 关联
    def _export_report(self):
        if not self.session_id:
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出分析报告",
                                              f"report_session{self.session_id}.html",
                                              "HTML 报告 (*.html)")
        if not path:
            return
        try:
            report_mod.build_html_report(self.session_id, self._stats, self._segments,
                                         figure=self._figure, out_path=path)
            self.log(f"报告已导出: {path}")
            QMessageBox.information(self, "导出成功", f"分析报告已保存到:\n{path}")
        except Exception as e:  # noqa: BLE001
            self.log(f"报告导出失败: {e}")
            QMessageBox.warning(self, "导出失败", str(e))

    def _export_zip(self):
        if not self.session_id:
            return
        path, _ = QFileDialog.getSaveFileName(self, "一键导出 zip",
                                              f"session{self.session_id}.zip", "ZIP (*.zip)")
        if not path:
            return
        try:
            export_mod.export_session(self.session_id, out_path=path)
            self.log(f"已导出 zip: {path}")
            QMessageBox.information(self, "导出成功", f"zip 已保存到:\n{path}")
        except Exception as e:  # noqa: BLE001
            self.log(f"zip 导出失败: {e}")
            QMessageBox.warning(self, "导出失败", str(e))

    def _attach_log(self):
        if not self.session_id:
            return
        path, _ = QFileDialog.getOpenFileName(self, "选择 EGOViewer app 日志（ego-viewer-*.log）",
                                              "", "日志 (*.log);;所有文件 (*)")
        if not path:
            return
        try:
            r = analysis.attach_app_log(self.session_id, path)
            self.log(f"已关联 app 日志并重算会话 #{self.session_id}")
            self.load_session(self.session_id)
            QMessageBox.information(self, "已关联", "已关联 app 日志并就地重算。")
        except Exception as e:  # noqa: BLE001
            self.log(f"关联失败: {e}")
            QMessageBox.warning(self, "关联失败", str(e))

    def recompute(self, thresholds_us: dict) -> None:
        """用新阈值重算当前会话并刷新（同步执行）。"""
        if not self.session_id:
            return
        QApplication.processEvents()
        try:
            r = analysis.recompute_session(self.session_id, thresholds_us)
            self.session_id = r["session_id"]
            self.load_session(self.session_id)
            self.log(f"已用新阈值重算会话 #{self.session_id}")
        except Exception as e:  # noqa: BLE001
            self.log(f"重算失败: {e}")


class SessionsTab(QWidget):
    """会话记录页：会话列表（列表 ↔ 详情 stack）。"""
    session_opened = Signal(int)

    def __init__(self, log):
        super().__init__()
        self.log = log
        self._detail = None
        self._build_ui()
        self.refresh_list()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        root.addWidget(self.stack)

        # ---- 列表页 ----
        list_page = QWidget()
        lv = QVBoxLayout(list_page)
        top = QHBoxLayout()
        top.addWidget(QLabel("会话列表"))
        top.addWidget(QLabel("（勾选后可批量删除；同一日志/目录只保留一条，重新分析就地更新）"))
        top.addStretch(1)
        self.btn_sel_all = QPushButton("全选")
        self.btn_sel_all.clicked.connect(self._select_all)
        self.btn_sel_clear = QPushButton("清空")
        self.btn_sel_clear.clicked.connect(self._clear_selection)
        self.btn_del = QPushButton("删除选中")
        self.btn_del.setProperty("role", "danger")
        self.btn_del.clicked.connect(self._delete_selected)
        self.btn_compare = QPushButton("对比选中")
        self.btn_compare.clicked.connect(self._compare_selected)
        self.btn_summary = QPushButton("导出汇总报告")
        self.btn_summary.clicked.connect(self._export_summary)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self.refresh_list)
        self.btn_open = QPushButton("打开")
        self.btn_open.setProperty("role", "primary")
        self.btn_open.clicked.connect(self._open_selected)
        top.addWidget(self.btn_sel_all)
        top.addWidget(self.btn_sel_clear)
        top.addWidget(self.btn_del)
        top.addWidget(self.btn_compare)
        top.addWidget(self.btn_summary)
        top.addWidget(self.btn_refresh)
        top.addWidget(self.btn_open)
        lv.addLayout(top)
        self.sess_table = QTableWidget(0, 8)
        self.sess_table.setHorizontalHeaderLabels(
            ["", "ID", "设备", "名称", "帧", "异常", "trace_id", "开始时间"])
        self.sess_table.setColumnWidth(0, 34)
        self.sess_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.sess_table.setAlternatingRowColors(True)
        self.sess_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.sess_table.itemDoubleClicked.connect(lambda _it: self._open_selected())
        lv.addWidget(self.sess_table, stretch=1)
        self.stack.addWidget(list_page)

        # ---- 详情页 ----
        detail_page = QWidget()
        dv = QVBoxLayout(detail_page)
        # 顶栏：返回列表 与 关联app日志/导出报告/导出zip 同一行（去掉会话#N 详情标题）
        self._detail = SessionDetail(self.log)
        brow = QHBoxLayout()
        self.btn_back = QPushButton("← 返回列表")
        self.btn_back.clicked.connect(self._back_to_list)
        brow.addWidget(self.btn_back)
        brow.addStretch(1)
        brow.addWidget(self._detail.btn_attach)
        brow.addWidget(self._detail.btn_report)
        brow.addWidget(self._detail.btn_zip)
        dv.addLayout(brow)
        dv.addWidget(self._detail, stretch=1)
        self.stack.addWidget(detail_page)

        self.stack.setCurrentIndex(0)

    def refresh_list(self) -> None:
        self.sess_table.setRowCount(0)
        for s in db.list_sessions(200):
            r = self.sess_table.rowCount()
            self.sess_table.insertRow(r)
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            chk.setCheckState(Qt.Unchecked)
            self.sess_table.setItem(r, 0, chk)
            self.sess_table.setItem(r, 1, QTableWidgetItem(str(s["id"])))
            self.sess_table.setItem(r, 2, QTableWidgetItem(s.get("device_serial") or "-"))
            self.sess_table.setItem(r, 3, QTableWidgetItem(s.get("name", "")))
            self.sess_table.setItem(r, 4, QTableWidgetItem(str(s.get("frames", 0))))
            self.sess_table.setItem(r, 5, _badge_item(str(s.get("anomalies", 0)),
                                                      (s.get("anomalies") or 0) == 0))
            self.sess_table.setItem(r, 6, QTableWidgetItem((s.get("trace_id") or "-")[:16]))
            self.sess_table.setItem(r, 7, QTableWidgetItem(_fmt(s.get("start_us"))))

    def _select_all(self) -> None:
        for r in range(self.sess_table.rowCount()):
            it = self.sess_table.item(r, 0)
            if it:
                it.setCheckState(Qt.Checked)

    def _clear_selection(self) -> None:
        for r in range(self.sess_table.rowCount()):
            it = self.sess_table.item(r, 0)
            if it:
                it.setCheckState(Qt.Unchecked)

    def _delete_selected(self) -> None:
        ids = []
        for r in range(self.sess_table.rowCount()):
            it = self.sess_table.item(r, 0)
            if it and it.checkState() == Qt.Checked:
                idit = self.sess_table.item(r, 1)
                if idit:
                    ids.append(int(idit.text()))
        if not ids:
            QMessageBox.information(self, "提示", "请先勾选要删除的会话")
            return
        ret = QMessageBox.question(
            self, "确认删除",
            f"确定删除选中的 {len(ids)} 条会话吗？\n"
            "将同时删除其帧、异常、来源文件记录，且不可恢复。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        for sid in ids:
            db.delete_session(sid)
        self.refresh_list()
        self.log(f"已删除 {len(ids)} 条会话")
        QMessageBox.information(self, "完成", f"已删除 {len(ids)} 条会话")

    def _checked_ids(self) -> list:
        ids = []
        for r in range(self.sess_table.rowCount()):
            it = self.sess_table.item(r, 0)
            if it and it.checkState() == Qt.Checked:
                idit = self.sess_table.item(r, 1)
                if idit:
                    ids.append(int(idit.text()))
        return ids

    def _compare_selected(self) -> None:
        ids = self._checked_ids()
        if len(ids) < 2:
            QMessageBox.information(self, "提示", "请至少勾选 2 条会话进行对比")
            return
        trs = compare_mod.compare_table_rows(compare_mod.compare_sessions(ids))
        if not trs:
            QMessageBox.warning(self, "提示", "未获取到对比数据")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"多会话对比（{len(trs)} 条）")
        dlg.resize(1280, 500)
        lay = QVBoxLayout(dlg)
        headers = list(trs[0].keys())
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        table.setAlternatingRowColors(True)
        for tr in trs:
            r = table.rowCount()
            table.insertRow(r)
            for c, h in enumerate(headers):
                table.setItem(r, c, QTableWidgetItem(str(tr[h])))
        lay.addWidget(table)
        btn = QPushButton("关闭")
        btn.clicked.connect(dlg.accept)
        lay.addWidget(btn)
        dlg.exec()

    def _export_summary(self) -> None:
        ids = self._checked_ids()
        if len(ids) < 2:
            QMessageBox.information(self, "提示", "请至少勾选 2 条会话导出汇总报告")
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出多会话汇总报告",
                                              "compare_summary.html", "HTML 报告 (*.html)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(compare_mod.compare_html(ids))
            self.log(f"已导出多会话汇总报告: {path}")
            QMessageBox.information(self, "完成", f"汇总报告已保存到:\n{path}")
        except Exception as e:  # noqa: BLE001
            self.log(f"汇总报告导出失败: {e}")
            QMessageBox.warning(self, "导出失败", str(e))

    def _open_selected(self) -> None:
        row = self.sess_table.currentRow()
        if row < 0:
            return
        sid = int(self.sess_table.item(row, 1).text())
        self.open_session(sid)

    def open_session(self, session_id: int) -> None:
        self._detail.load_session(session_id)
        self.stack.setCurrentIndex(1)
        self.session_opened.emit(session_id)

    def _back_to_list(self) -> None:
        self.stack.setCurrentIndex(0)
        self.refresh_list()

    def current_session_id(self):
        return self._detail.session_id

    def recompute_current(self, thresholds_us: dict) -> None:
        self._detail.recompute(thresholds_us)
