"""日志分析 Tab：整目录拉取 → 数采分析引擎（ego_checker）→ 自包含 HTML 报告。

分析内核来自 EGOChecker（内嵌于 ego_checker/ 包），与本工具其它页面同一套
浅色 QSS 风格。本页只保留一条主流程：

    选择数据目录 → 开始分析（后台线程 + 实时日志 + 进度） →
    结果表（PASS/FAIL 着色、双击看异常详情） → 打开 HTML 报告 / 输出目录

一次分析会读取目录下的全部采集数据：相机/IMU/夹爪等 CSV、MCAP 视频与传感器流、
WAV 音频与配套 *_pts.csv、meta_*.json 元数据，与其他录制段做跨段边界比对，
最终只输出一份 HTML 报告。
"""

from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime

from PySide6.QtCore import (
    Qt, QThread, Signal, QUrl, QMetaObject, Q_ARG, Slot, QTime, QDate, QDateTime,
)
from PySide6.QtGui import QColor, QDesktopServices, QDoubleValidator, QIntValidator
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QGroupBox, QLabel, QLineEdit,
    QPushButton, QCheckBox, QComboBox, QFileDialog, QPlainTextEdit, QTableWidget,
    QTableWidgetItem, QHeaderView, QProgressBar, QMessageBox, QSpinBox, QDialog,
    QAbstractItemView, QToolButton, QSizePolicy, QScrollArea, QFrame,
    QButtonGroup, QTimeEdit, QDateEdit, QListWidget, QApplication,
    QAbstractSpinBox,
)

from .analysis_schedule import (
    MODE_MANUAL, MODE_SCHEDULED, REPEAT_DAILY, REPEAT_ONCE,
    ScheduleController, ScheduleSettings, STATUS_LABELS, format_dt,
    schedule_field_defaults, schedule_path,
)
from ego_checker.app.options import parse_capture_part_limit, parse_oos_group_limit
from ego_checker.app.parallel import ANALYSIS_WORKER_CHOICES
from ego_checker.app.workflow import AnalysisTask
import ego_checker.media.video as fe
import ego_checker.reporting.outputs as rep
from ego_checker.runtime.errors import describe_exception


ROW_H = 34


def _field_label(text):
    """表单列标签：与设备导出页同一宽度与对齐，保证输入框基线一致。"""
    lbl = QLabel(text)
    lbl.setMinimumWidth(104)
    lbl.setAlignment(Qt.AlignCenter)
    return lbl


class AnalyzerWorker(QThread):
    """在后台线程执行一次完整分析：发现分段 → 逐段检查 → 跨段 → HTML。

    直接复用分析引擎的 ``AnalysisTask``；它把日志、进度、结束、失败、停止
    都以回调形式抛出，这里用信号转交给主线程。分析运行日志由引擎自行落盘到
    ``data/log/analysis_*.log``。
    """

    log = Signal(str)
    progress = Signal(str, int, int)
    finished_ok = Signal(list, str, str)
    failed = Signal(str)
    stopped = Signal()

    def __init__(self, root, out_root, params, options, parent=None):
        super().__init__(parent)
        self._task = AnalysisTask(
            root, out_root, params, options,
            log=self.log.emit, progress=self.progress.emit,
            done=self.finished_ok.emit, failed=self.failed.emit,
            stopped=self.stopped.emit,
        )

    def pause(self):
        self._task.pause()

    def resume(self):
        self._task.resume()

    def stop(self):
        self._task.stop()

    def is_paused(self):
        return self._task.is_paused()

    def was_stopped(self):
        """本次分析是否由用户停止（停止时也会带着部分报告进入 done）。"""
        return self._task.was_stopped()

    def run(self):
        self._task.run()


class AnalysisTab(QWidget):
    """日志分析页：目录选择 + 参数 + 结果表 + 报告入口。"""

    CLOSE_WAIT_MS = 30_000

    # 一次分析结束后把记录写入「会话记录」；参数是新记录的 id。
    history_recorded = Signal(str)
    # 定时触发/完成等状态交给主窗口底部状态栏。
    status_message = Signal(str)

    def __init__(self, log, log_box=None):
        super().__init__()
        self.log = log
        self._results = []
        self._out_root = ""
        self._html_path = ""
        self._worker: AnalyzerWorker | None = None
        self._thr_edits: dict = {}
        self._run_ctx: dict = {}      # 本次分析的 root/out/起止时间，供历史登记
        self._history_pending = False  # 已写入历史，防止 done/stopped 双回调重复登记
        self._schedule_batch: dict | None = None  # 多目录定时批次
        self._closing = False
        self._build_ui()

        # 定时调度：创建即加载 data/analysis_schedule.json，启动后获取单实例锁。
        # 记住磁盘上是否已有配置：没有配置时「仅一次」默认使用电脑当前时间，
        # 而不是一个与现场无关的固定时刻。
        self._schedule_file_existed = schedule_path().is_file()
        self._scheduler = ScheduleController(self)
        self._scheduler.due.connect(self._on_schedule_due)
        self._scheduler.status_changed.connect(self._on_schedule_status)
        self._scheduler.log.connect(self._append_log)
        self.radio_manual.toggled.connect(self._on_mode_toggled)
        self._apply_schedule_to_ui()
        self.chk_sched_enabled.toggled.connect(self._on_sched_enabled_toggled)
        self._scheduler.start()
        self._update_schedule_labels()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 整页套滚动区（与「日志导出」页一致）：窗口高度不足时上下滚动查看，
        # 而不是把结果表 / 日志框压成一条缝。滚动区内部按内容高度排布，
        # 内容不超出时不出现滚动条。
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(14, 12, 14, 12)
        bl.setSpacing(10)
        bl.addWidget(self._build_cond())
        bl.addWidget(self._build_schedule())
        bl.addWidget(self._build_res())
        bl.addWidget(self._build_log())
        bl.addStretch(1)
        scroll.setWidget(body)
        root.addWidget(scroll, stretch=1)

    def _build_cond(self) -> QGroupBox:
        gb = QGroupBox("分析条件")
        v = QGridLayout(gb)
        v.setContentsMargins(12, 16, 12, 10)
        v.setHorizontalSpacing(8)
        v.setVerticalSpacing(8)
        r = 0

        # 分析方式：分段按钮（不使用单选圆点）。定时设置组只在定时模式显示，
        # 切回手动时定时任务暂停但配置保留。
        way_row = QHBoxLayout()
        way_row.setSpacing(8)
        self.radio_manual = QPushButton("目录分析")
        self.radio_manual.setObjectName("modeSwitch")
        self.radio_manual.setCheckable(True)
        self.radio_manual.setChecked(True)
        self.radio_manual.setFixedHeight(32)
        self.radio_manual.setMinimumWidth(108)
        self.radio_manual.setToolTip("选择数据目录后点「开始分析」；现有手动流程不变")
        self.radio_scheduled = QPushButton("定时分析")
        self.radio_scheduled.setObjectName("modeSwitch")
        self.radio_scheduled.setCheckable(True)
        self.radio_scheduled.setFixedHeight(32)
        self.radio_scheduled.setMinimumWidth(108)
        self.radio_scheduled.setToolTip(
            "到设定时间自动开始分析；需要 EGO Courier 保持运行（可最小化）")
        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        self._mode_group.addButton(self.radio_manual)
        self._mode_group.addButton(self.radio_scheduled)
        way_row.addWidget(self.radio_manual)
        way_row.addWidget(self.radio_scheduled)
        way_row.addStretch(1)
        way_w = QWidget(); way_w.setLayout(way_row)
        v.addWidget(_field_label("分析方式"), r, 0)
        v.addWidget(way_w, r, 1)
        r += 1

        # 数据根目录 / 输出目录
        self.ed_root = QLineEdit()
        self.ed_root.setPlaceholderText("选择要分析的录制目录（可含多层子文件夹）")
        self.ed_root.setMinimumHeight(30)
        btn_root = QPushButton("浏览…")
        btn_root.clicked.connect(self._pick_root)
        root_row = QHBoxLayout()
        root_row.addWidget(self.ed_root, 1)
        root_row.addWidget(btn_root)
        root_w = QWidget(); root_w.setLayout(root_row)
        self._root_label = _field_label("数据根目录")
        self._root_row_widget = root_w
        v.addWidget(self._root_label, r, 0)
        v.addWidget(self._root_row_widget, r, 1)
        r += 1

        self.ed_out = QLineEdit()
        self.ed_out.setPlaceholderText("留空则输出到数据根目录下的 fsync_output")
        self.ed_out.setMinimumHeight(30)
        btn_out = QPushButton("浏览…")
        btn_out.clicked.connect(self._pick_out)
        out_row = QHBoxLayout()
        out_row.addWidget(self.ed_out, 1)
        out_row.addWidget(btn_out)
        out_w = QWidget(); out_w.setLayout(out_row)
        v.addWidget(_field_label("输出目录"), r, 0)
        v.addWidget(out_w, r, 1)
        r += 1

        # 会话与文件（拆两行，避免一行控件过多把整页撑宽）
        mode_row = QHBoxLayout()
        mode_row.setSpacing(6)
        mode_row.addWidget(QLabel("会话数据类型"))
        self.cmb_input_mode = QComboBox()
        self.cmb_input_mode.addItem("单设备", "single")
        self.cmb_input_mode.addItem("多设备", "multi")
        self.cmb_input_mode.setToolTip(
            "单设备：各设备独立分析；多设备：按录制批次组合，可选包含多个会话的父目录")
        mode_row.addWidget(self.cmb_input_mode)
        mode_row.addSpacing(16)
        mode_row.addWidget(QLabel("落盘格式"))
        self.cmb_layout_mode = QComboBox()
        self.cmb_layout_mode.addItem("录制独立目录", "separate")
        self.cmb_layout_mode.addItem("多次录制平铺", "flat")
        self.cmb_layout_mode.setToolTip(
            "录制独立目录：每次录制占用各自的设备目录（默认）。"
            "多次录制平铺：同一设备目录下并存多次录制，按 meta 拆分会话，每次录制单独出一份报告")
        mode_row.addWidget(self.cmb_layout_mode)
        mode_row.addStretch(1)
        mode_w = QWidget(); mode_w.setLayout(mode_row)
        v.addWidget(_field_label("会话与文件"), r, 0)
        v.addWidget(mode_w, r, 1)
        r += 1

        pair_row = QHBoxLayout()
        pair_row.setSpacing(6)
        pair_row.addWidget(QLabel("配对等级"))
        self.cmb_pairing = QComboBox()
        self.cmb_pairing.addItem("meta校验配对", "meta")
        self.cmb_pairing.addItem("父目录配对", "parent")
        self.cmb_pairing.setToolTip(
            "仅多设备生效。meta校验配对：按 UUID/录制组ID/开始时间/主从引用校验（默认）；"
            "父目录配对：直接按设备父目录组合，差异仅作告警")
        pair_row.addWidget(self.cmb_pairing)
        pair_row.addSpacing(16)
        pair_row.addWidget(QLabel("分析并发"))
        self.cmb_workers = QComboBox()
        for value in ANALYSIS_WORKER_CHOICES:
            label = "自动" if value == 0 else "串行" if value == 1 else f"{value} 个进程"
            self.cmb_workers.addItem(label, value)
        self.cmb_workers.setToolTip(
            "自动根据核心数、数据量与可用内存选择最多 8 个进程；"
            "SEI 对齐、帧截取或关闭流式释放时使用串行分析")
        pair_row.addWidget(self.cmb_workers)
        pair_row.addStretch(1)
        pair_w = QWidget(); pair_w.setLayout(pair_row)
        v.addWidget(_field_label("配对与并发"), r, 0)
        v.addWidget(pair_w, r, 1)
        r += 1
        self.cmb_input_mode.currentIndexChanged.connect(self._sync_pairing_enabled)

        # 检查项：相机/深度/SEI/音频/元数据
        chk1 = QHBoxLayout()
        chk1.setSpacing(18)
        self.chk_depth_sync = QCheckBox("Depth 同步测试")
        self.chk_depth_sync.setChecked(True)
        self.chk_depth_sync.setToolTip(
            "检查 Depth 与其它相机的同步；取消后 Depth 不参与相机间同步及其跨段补偿。"
            "Depth RGB 仅与左、右、上三目比较；鱼眼内部同步照常执行")
        self.chk_sei = QCheckBox("SEI 时间戳对齐")
        self.chk_sei.setChecked(False)
        self.chk_audio = QCheckBox("音频测试（WAV）")
        self.chk_audio.setChecked(True)
        self.chk_audio.setToolTip(
            "检查音频时长、包完整性和连续性；取消后不读取 WAV，报告不显示音频相关列")
        self.chk_meta = QCheckBox("元数据完整性测试")
        self.chk_meta.setChecked(True)
        self.chk_meta.setToolTip(
            "检测 meta_*.json 完整性与准确性（必备区块/字段/分片/取值/跨文件一致）")
        for w in (self.chk_depth_sync, self.chk_sei, self.chk_audio, self.chk_meta):
            chk1.addWidget(w)
        chk1.addStretch(1)
        chk1_w = QWidget(); chk1_w.setLayout(chk1)
        v.addWidget(_field_label("检查项"), r, 0)
        v.addWidget(chk1_w, r, 1)
        r += 1

        # 音频目标时长 / 实测漂移 / SHA256
        aud = QHBoxLayout()
        aud.setSpacing(6)
        aud.addWidget(QLabel("音频目标时长(s)"))
        self.ed_audio_target = QLineEdit()
        self.ed_audio_target.setPlaceholderText("可选")
        self.ed_audio_target.setValidator(QDoubleValidator(0.000001, 1_000_000_000, 6, self))
        self.ed_audio_target.setFixedWidth(88)
        self.ed_audio_target.setToolTip("按每个音频流判定；必须同时填写实测时钟漂移 ppm")
        aud.addWidget(self.ed_audio_target)
        aud.addWidget(QLabel("实测漂移(ppm)"))
        self.ed_audio_drift = QLineEdit()
        self.ed_audio_drift.setPlaceholderText("可选")
        self.ed_audio_drift.setValidator(QDoubleValidator(0, 1_000_000, 6, self))
        self.ed_audio_drift.setFixedWidth(88)
        self.ed_audio_drift.setToolTip("留空时目标时长只展示，不输出 PASS/FAIL")
        aud.addWidget(self.ed_audio_drift)
        self.chk_audio_sha256 = QCheckBox("音频SHA256留证")
        self.chk_audio_sha256.setToolTip(
            "可审计但会完整读取音频文件，批量大文件分析耗时会增加")
        aud.addWidget(self.chk_audio_sha256)
        aud.addStretch(1)
        aud_w = QWidget(); aud_w.setLayout(aud)
        v.addWidget(_field_label("音频附加"), r, 0)
        v.addWidget(aud_w, r, 1)
        r += 1

        # 阈值模式
        row_adaptive = QHBoxLayout()
        self.chk_adaptive = QCheckBox("阈值自适应")
        self.chk_adaptive.setChecked(True)
        self.chk_fixed = QCheckBox("阈值固定输入")
        row_adaptive.addWidget(self.chk_adaptive)
        btn_note = QToolButton()
        btn_note.setText("ⓘ")
        btn_note.setCursor(Qt.PointingHandCursor)
        btn_note.setAutoRaise(True)
        btn_note.setStyleSheet("QToolButton{color:#2563eb;font-size:15px;font-weight:700;}")
        btn_note.setToolTip("点击查看阈值自适应取阈规则")
        btn_note.clicked.connect(self._show_adaptive_note)
        row_adaptive.addWidget(btn_note)
        row_adaptive.addSpacing(12)
        row_adaptive.addWidget(self.chk_fixed)
        row_adaptive.addStretch(1)
        row_adaptive_w = QWidget(); row_adaptive_w.setLayout(row_adaptive)
        v.addWidget(_field_label("阈值"), r, 0)
        v.addWidget(row_adaptive_w, r, 1)
        r += 1

        thr = QHBoxLayout()
        thr.setSpacing(8)
        threshold_fields = [
            ("interval_ms", "帧间隔波动(ms)", "10"),
            ("row_align_ms", "同行对齐(ms)", "10"),
            ("cam_sync_ms", "相机同步(ms)", "1"),
            ("depth_sync_ms", "深度同步(ms)", "5"),
            ("imu_sync_ms", "IMU同步(ms)", "1"),
        ]
        # 固定阈值 5 项一行排完；页面宽度足够，不再拆两行。
        for key, lab, default in threshold_fields:
            e = QLineEdit(default)
            e.setFixedWidth(60)
            e.setMinimumHeight(28)
            e.setEnabled(False)
            if key == "depth_sync_ms":
                e.setValidator(QDoubleValidator(0.001, 1_000_000, 3, self))
                e.setToolTip("Depth、Depth RGB 与其它相机同步共用；偏差必须严格小于输入值")
            self._thr_edits[key] = e
            thr.addWidget(QLabel(lab))
            thr.addWidget(e)
        thr.addStretch(1)
        thr_w = QWidget(); thr_w.setLayout(thr)
        v.addWidget(_field_label("固定阈值"), r, 0)
        v.addWidget(thr_w, r, 1)
        r += 1

        # 帧截取：不同步 / 同步 / Part 数一行排完。
        cap = QHBoxLayout()
        cap.setSpacing(6)
        self.chk_oos = QCheckBox("不同步帧图片截取")
        self.chk_oos.setChecked(False)
        self.ed_oos_groups = QLineEdit()
        self.ed_oos_groups.setPlaceholderText("ALL")
        self.ed_oos_groups.setValidator(QIntValidator(1, 1_000_000, self))
        self.ed_oos_groups.setFixedWidth(66)
        self.ed_oos_groups.setEnabled(False)
        self.ed_oos_groups.setToolTip("留空（ALL）截取全部不同步组；填写正整数则最多截取该组数")
        self.chk_oos.toggled.connect(self.ed_oos_groups.setEnabled)
        cap.addWidget(self.chk_oos)
        cap.addWidget(self.ed_oos_groups)
        cap.addWidget(QLabel("组"))
        cap.addSpacing(18)
        self.chk_syn = QCheckBox("同步帧图片截取")
        self.chk_syn.setChecked(False)
        self.sp_syn_groups = QSpinBox()
        self.sp_syn_groups.setRange(1, 1000)
        self.sp_syn_groups.setValue(10)
        self.sp_syn_groups.setFixedWidth(66)
        self.sp_syn_groups.setEnabled(False)
        self.chk_syn.toggled.connect(self.sp_syn_groups.setEnabled)
        cap.addWidget(self.chk_syn)
        cap.addWidget(self.sp_syn_groups)
        cap.addWidget(QLabel("组"))
        cap.addSpacing(18)
        cap.addWidget(QLabel("截取帧Part数"))
        self.ed_capture_parts = QLineEdit()
        self.ed_capture_parts.setPlaceholderText("ALL")
        self.ed_capture_parts.setValidator(QIntValidator(-1_000_000, 1_000_000, self))
        self.ed_capture_parts.setFixedWidth(66)
        self.ed_capture_parts.setEnabled(False)
        self.ed_capture_parts.setToolTip(
            "留空（ALL）截取全部 Part；填写整数时首尾 Part 必选，中间优先随机选择有不同步情况的 Part")
        cap.addWidget(self.ed_capture_parts)
        cap.addStretch(1)
        cap_w = QWidget(); cap_w.setLayout(cap)
        v.addWidget(_field_label("帧截取"), r, 0)
        v.addWidget(cap_w, r, 1)
        r += 1

        def _update_capture_parts(_=False):
            self.ed_capture_parts.setEnabled(self.chk_oos.isChecked() or self.chk_syn.isChecked())

        self.chk_oos.toggled.connect(_update_capture_parts)
        self.chk_syn.toggled.connect(_update_capture_parts)

        # 操作按钮 + 进度
        act = QHBoxLayout()
        act.setSpacing(8)
        self.btn_run = QPushButton("▶ 开始分析")
        self.btn_run.setProperty("role", "primary")
        self.btn_pause = QPushButton("⏸ 暂停")
        self.btn_stop = QPushButton("⏹ 停止")
        self.btn_stop.setProperty("role", "stop")
        self.btn_reset = QPushButton("恢复默认")
        for b in (self.btn_run, self.btn_pause, self.btn_stop, self.btn_reset):
            b.setFixedHeight(ROW_H)
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.btn_run.clicked.connect(self._start)
        self.btn_pause.clicked.connect(self._toggle_pause)
        self.btn_stop.clicked.connect(self._stop_run)
        self.btn_reset.clicked.connect(self._reset_params)

        self.progress = QProgressBar()
        self.progress.setObjectName("taskProgress")
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(True)
        self.progress.setFormat("%p%")
        self.progress.setValue(0)
        self.progress.setFixedHeight(ROW_H)
        self.progress.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        act.addWidget(self.progress, 1)
        act.addWidget(self.btn_run)
        act.addWidget(self.btn_pause)
        act.addWidget(self.btn_stop)
        act.addWidget(self.btn_reset)
        act_w = QWidget(); act_w.setLayout(act)
        v.addWidget(_field_label("分析进度"), r, 0)
        v.addWidget(act_w, r, 1)
        r += 1

        v.setColumnStretch(1, 1)

        # 阈值自适应（默认）↔ 固定输入互斥：勾一个自动取消另一个，并联动输入框可用状态。
        def _on_adaptive(checked):
            if checked and self.chk_fixed.isChecked():
                self.chk_fixed.blockSignals(True)
                self.chk_fixed.setChecked(False)
                self.chk_fixed.blockSignals(False)
            self._apply_threshold_mode()

        def _on_fixed(checked):
            if checked and self.chk_adaptive.isChecked():
                self.chk_adaptive.blockSignals(True)
                self.chk_adaptive.setChecked(False)
                self.chk_adaptive.blockSignals(False)
            self._apply_threshold_mode()

        self.chk_adaptive.toggled.connect(_on_adaptive)
        self.chk_fixed.toggled.connect(_on_fixed)

        self._sync_pairing_enabled()
        self._apply_threshold_mode()
        return gb

    def _build_schedule(self) -> QGroupBox:
        """定时分析设置组；只在「定时分析」模式下显示。"""
        gb = QGroupBox("定时设置")
        self.grp_schedule = gb
        v = QGridLayout(gb)
        v.setContentsMargins(12, 16, 12, 10)
        v.setHorizontalSpacing(8)
        v.setVerticalSpacing(8)

        hint = QLabel(
            "定时分析按列表顺序逐个目录执行；输出目录沿用上方「输出目录」。"
            "勾选「启用定时分析」或点「保存定时设置」时会记录当前参数快照。"
            "定时分析需要 EGO Courier 保持运行（可最小化，但不要退出）。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#64748b;font-size:12px;")
        v.addWidget(hint, 0, 0, 1, 6)

        # ---- 定时数据目录（支持多个，按列表顺序逐个跑） ----
        self.lst_sched_roots = QListWidget()
        self.lst_sched_roots.setObjectName("schedDirList")
        self.lst_sched_roots.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.lst_sched_roots.setAlternatingRowColors(True)
        self.lst_sched_roots.setMinimumHeight(74)
        self.lst_sched_roots.setMaximumHeight(118)
        self.lst_sched_roots.setToolTip("定时分析按此顺序逐个目录执行；可添加多个目录")
        self.lst_sched_roots.setTextElideMode(Qt.ElideMiddle)
        self.lst_sched_roots.setStyleSheet(
            "QListWidget#schedDirList{background:#ffffff;border:1px solid #d1d5db;"
            "border-radius:4px;padding:3px;}"
            "QListWidget#schedDirList::item{padding:4px 6px;}")
        self.btn_sched_add_root = QPushButton("添加目录…")
        self.btn_sched_remove_root = QPushButton("移除选中")
        for btn in (self.btn_sched_add_root, self.btn_sched_remove_root):
            btn.setFixedHeight(32)
            btn.setMinimumWidth(120)
        self.btn_sched_add_root.clicked.connect(self._add_schedule_root)
        self.btn_sched_remove_root.clicked.connect(self._remove_schedule_root)
        dirs = QHBoxLayout()
        dirs.setSpacing(8)
        dirs.addWidget(self.lst_sched_roots, 1)
        dir_btns = QVBoxLayout()
        dir_btns.setSpacing(6)
        dir_btns.addWidget(self.btn_sched_add_root)
        dir_btns.addWidget(self.btn_sched_remove_root)
        dir_btns.addStretch(1)
        dirs.addLayout(dir_btns)
        dirs_w = QWidget(); dirs_w.setLayout(dirs)
        v.addWidget(_field_label("定时数据目录"), 1, 0)
        v.addWidget(dirs_w, 1, 1, 1, 5)

        # ---- 定时规则 ----
        self.ed_sched_time = QTimeEdit()
        self.ed_sched_time.setObjectName("dtTimeEdit")
        self.ed_sched_time.setDisplayFormat("HH:mm")
        self.ed_sched_time.setTime(QTime(2, 0))
        self.ed_sched_time.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.ed_sched_time.setAlignment(Qt.AlignCenter)
        self.ed_sched_time.setFixedWidth(96)
        self.ed_sched_time.setFixedHeight(30)
        self.ed_sched_time.setToolTip(
            "触发时刻（本机时间）；可直接输入 HH:MM，或用上下方向键调整")
        self.cmb_sched_repeat = QComboBox()
        self.cmb_sched_repeat.addItem("每天", REPEAT_DAILY)
        self.cmb_sched_repeat.addItem("仅一次", REPEAT_ONCE)
        self.cmb_sched_repeat.setFixedWidth(96)
        self.cmb_sched_repeat.setFixedHeight(30)
        self.ed_sched_date = QDateEdit()
        self.ed_sched_date.setDisplayFormat("yyyy-MM-dd")
        self.ed_sched_date.setDate(QDate.currentDate())
        self.ed_sched_date.setMinimumDate(QDate.currentDate())
        self.ed_sched_date.setCalendarPopup(True)
        self.ed_sched_date.setFixedWidth(130)
        self.ed_sched_date.setFixedHeight(30)
        self.ed_sched_date.setStyleSheet(
            "QDateEdit{background:#ffffff;border:1px solid #d1d5db;"
            "border-radius:4px;padding:4px 7px;}")
        self.ed_sched_date.setToolTip("仅一次模式的日期（默认跟随电脑当前日期）")
        self.ed_sched_date.setVisible(False)
        self.btn_sched_sync_now = QPushButton("现在")
        self.btn_sched_sync_now.setFixedWidth(56)
        self.btn_sched_sync_now.setFixedHeight(30)
        self.btn_sched_sync_now.setToolTip(
            "同步为电脑当前时间（触发时刻取下一分钟，避免一保存就提示时间已过）")
        self.btn_sched_sync_now.clicked.connect(self._use_current_datetime)
        self.cmb_sched_repeat.currentIndexChanged.connect(self._sync_schedule_date)
        trig = QHBoxLayout()
        trig.setSpacing(8)
        trig.addWidget(QLabel("触发时间"))
        trig.addWidget(self.ed_sched_time)
        trig.addWidget(self.btn_sched_sync_now)
        trig.addSpacing(12)
        trig.addWidget(QLabel("重复"))
        trig.addWidget(self.cmb_sched_repeat)
        trig.addWidget(self.ed_sched_date)
        trig.addStretch(1)
        trig_w = QWidget(); trig_w.setLayout(trig)
        v.addWidget(_field_label("定时规则"), 2, 0)
        v.addWidget(trig_w, 2, 1, 1, 5)

        # ---- 定时选项 ----
        self.chk_sched_enabled = QCheckBox("启用定时分析")
        self.chk_sched_enabled.setToolTip(
            "取消勾选后定时暂停、配置保留；重新勾选会保存当前表单并恢复")
        self.chk_sched_catchup = QCheckBox("错过时补跑（2小时内）")
        self.chk_sched_catchup.setChecked(True)
        self.chk_sched_catchup.setToolTip(
            "程序启动或电脑睡眠唤醒后，若已过触发点但不超过 2 小时，补跑一次；超过则跳过")
        self.chk_sched_independent_out = QCheckBox("每次输出到独立时间戳目录")
        self.chk_sched_independent_out.setChecked(True)
        self.chk_sched_independent_out.setToolTip(
            "每个目录在输出目录下新建 scheduled_YYYYMMDD_HHMMSS/，避免报告互相覆盖")
        opts = QHBoxLayout()
        opts.setSpacing(18)
        opts.addWidget(self.chk_sched_enabled)
        opts.addWidget(self.chk_sched_catchup)
        opts.addWidget(self.chk_sched_independent_out)
        info = QToolButton()
        info.setText("ⓘ")
        info.setCursor(Qt.PointingHandCursor)
        info.setAutoRaise(True)
        info.setStyleSheet("QToolButton{color:#2563eb;font-size:15px;font-weight:700;}")
        info.setToolTip("查看定时选项说明")
        info.clicked.connect(self._show_schedule_note)
        opts.addWidget(info)
        opts.addStretch(1)
        opts_w = QWidget(); opts_w.setLayout(opts)
        v.addWidget(_field_label("定时选项"), 3, 0)
        v.addWidget(opts_w, 3, 1, 1, 5)

        # ---- 运行状态：直接两行，避免包裹背景盖住下方按钮 ----
        self.lbl_sched_next = QLabel("下次运行：—")
        self.lbl_sched_next.setStyleSheet("color:#334155;font-size:12px;")
        self.lbl_sched_last = QLabel("上次运行：—")
        self.lbl_sched_last.setStyleSheet("color:#64748b;font-size:12px;")
        v.addWidget(_field_label("运行状态"), 4, 0, 2, 1)
        v.addWidget(self.lbl_sched_next, 4, 1, 1, 5)
        v.addWidget(self.lbl_sched_last, 5, 1, 1, 5)

        # ---- 操作按钮 ----
        self.btn_sched_save = QPushButton("保存定时设置")
        self.btn_sched_save.setProperty("role", "primary")
        self.btn_sched_now = QPushButton("立即运行一次")
        self.btn_sched_pause = QPushButton("暂停定时")
        for btn in (self.btn_sched_save, self.btn_sched_now, self.btn_sched_pause):
            btn.setFixedHeight(ROW_H)
            btn.setMinimumWidth(116)
        self.btn_sched_save.clicked.connect(self._save_schedule)
        self.btn_sched_now.clicked.connect(self._run_schedule_now)
        self.btn_sched_pause.clicked.connect(self._pause_schedule)
        btns = QHBoxLayout()
        btns.setSpacing(8)
        btns.addWidget(self.btn_sched_save)
        btns.addWidget(self.btn_sched_now)
        btns.addWidget(self.btn_sched_pause)
        btns.addStretch(1)
        btns_w = QWidget(); btns_w.setLayout(btns)
        v.addWidget(_field_label("选项"), 6, 0)
        v.addWidget(btns_w, 6, 1, 1, 5)

        gb.setVisible(False)
        v.setColumnStretch(1, 1)
        return gb

    def _add_schedule_root(self):
        """向定时目录列表追加一个目录（去重）。"""
        start = (self.lst_sched_roots.item(0).text()
                 if self.lst_sched_roots.count() else self.ed_root.text().strip())
        path = QFileDialog.getExistingDirectory(self, "添加定时分析目录", start)
        if not path:
            return
        path = os.path.normpath(path)
        for index in range(self.lst_sched_roots.count()):
            if os.path.normcase(self.lst_sched_roots.item(index).text()) == \
                    os.path.normcase(path):
                return
        self.lst_sched_roots.addItem(path)
        self.lst_sched_roots.item(
            self.lst_sched_roots.count() - 1).setToolTip(path)
        self.lst_sched_roots.scrollToBottom()
        self._append_log(f"[定时] 已添加目录：{path}")

    def _remove_schedule_root(self):
        """移除列表中选中的目录。"""
        for item in self.lst_sched_roots.selectedItems():
            self.lst_sched_roots.takeItem(self.lst_sched_roots.row(item))

    def _schedule_roots_ui(self):
        return [self.lst_sched_roots.item(i).text().strip()
                for i in range(self.lst_sched_roots.count())
                if self.lst_sched_roots.item(i).text().strip()]

    def _set_schedule_roots(self, paths):
        self.lst_sched_roots.clear()
        seen = set()
        for path in paths or []:
            text = os.path.normpath(str(path))
            key = os.path.normcase(text)
            if not text or key in seen:
                continue
            seen.add(key)
            self.lst_sched_roots.addItem(text)
            self.lst_sched_roots.item(
                self.lst_sched_roots.count() - 1).setToolTip(text)

    def _show_schedule_note(self):
        text = (
            "启用定时分析：取消勾选后定时暂停，配置保留；重新勾选会保存当前表单并恢复。\n\n"
            "错过时补跑（2小时内）：程序启动或电脑睡眠唤醒后，若已过触发点但不超过 "
            "2 小时，补跑一次；超过则跳过，避免几天后突然运行。\n\n"
            "每次输出到独立时间戳目录：每个定时目录在输出目录下新建 "
            "scheduled_YYYYMMDD_HHMMSS/；多个目录会各自一个子目录，避免报告互相覆盖。"
        )
        dlg = QDialog(self)
        dlg.setWindowTitle("定时选项说明")
        dlg.resize(560, 230)
        lay = QVBoxLayout(dlg)
        lab = QLabel(text)
        lab.setWordWrap(True)
        lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lab.setStyleSheet("font-size:13px;")
        lay.addWidget(lab)
        btn = QPushButton("关闭")
        btn.setFixedWidth(96)
        btn.clicked.connect(dlg.accept)
        lay.addWidget(btn, alignment=Qt.AlignRight)
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.show()

    def _sync_schedule_date(self, _index=0, *, refresh_past=True):
        """仅一次模式显示日期；日期未设置或已过期时跟随电脑当前时间。

        日期控件的最小值固定为今天，因此旧配置里遗留的 2016 年日期不会显示，
        第一次打开「仅一次」也不会停在一个与系统时间无关的日期上。
        """
        once = self.cmb_sched_repeat.currentData() == REPEAT_ONCE
        if not hasattr(self, "ed_sched_date"):
            return
        today = QDate.currentDate()
        self.ed_sched_date.setMinimumDate(today)
        if once:
            date_value = self.ed_sched_date.date()
            if not date_value.isValid() or date_value < today:
                upcoming = QDateTime.currentDateTime().addSecs(60)
                self.ed_sched_date.setMinimumDate(upcoming.date())
                self.ed_sched_date.setDate(upcoming.date())
                self.ed_sched_time.setTime(upcoming.time())
            elif (refresh_past and date_value == today
                  and self.ed_sched_time.time() < QTime.currentTime()):
                # 今天但时刻已过：仅一次无法保存过去时间，顺延到下一分钟。
                upcoming = QDateTime.currentDateTime().addSecs(60)
                self.ed_sched_date.setDate(upcoming.date())
                self.ed_sched_time.setTime(upcoming.time())
        self.ed_sched_date.setVisible(once)

    def _use_current_datetime(self):
        """把「仅一次」的日期和触发时刻同步为电脑当前时间。"""
        now = QDateTime.currentDateTime()
        upcoming = now.addSecs(60)
        self.ed_sched_date.setMinimumDate(upcoming.date())
        self.ed_sched_date.setDate(upcoming.date())
        self.ed_sched_time.setTime(upcoming.time())
        self._append_log(
            "[定时] 触发时间已同步为当前系统时间，下一次触发顺延到 "
            + upcoming.toString("yyyy-MM-dd HH:mm"))

    def _build_res(self) -> QGroupBox:
        gb = QGroupBox("分析结果")
        v = QVBoxLayout(gb)
        v.setContentsMargins(12, 16, 12, 10)
        self.lbl_summary = QLabel("尚未分析。选择数据目录后点击「开始分析」。")
        self.lbl_summary.setStyleSheet("font-size:13px;color:#334155;")
        self.lbl_summary.setWordWrap(True)
        v.addWidget(self.lbl_summary)

        btns = QHBoxLayout()
        self.btn_open_html = QPushButton("打开 HTML 报告")
        self.btn_open_dir = QPushButton("打开输出文件夹")
        self.btn_open_html.setEnabled(False)
        self.btn_open_dir.setEnabled(False)
        self.btn_open_html.clicked.connect(self._open_html)
        self.btn_open_dir.clicked.connect(self._open_outdir)
        btns.addWidget(self.btn_open_html)
        btns.addWidget(self.btn_open_dir)
        btns.addStretch(1)
        v.addLayout(btns)

        self.table = QTableWidget(0, 0)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        # 固定高度，让整页滚动而不是把表格压扁；表格自身仍可上下滚动看更多行。
        self.table.setMinimumHeight(320)
        v.addWidget(self.table)
        return gb

    def _build_log(self) -> QGroupBox:
        gb = QGroupBox("运行日志")
        v = QVBoxLayout(gb)
        v.setContentsMargins(12, 16, 12, 10)
        self.txt_log = QPlainTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setMaximumBlockCount(8000)
        self.txt_log.setMinimumHeight(220)
        self.txt_log.setPlaceholderText("分析过程中的阶段进度、分段结果与异常会实时写入这里")
        v.addWidget(self.txt_log)
        return gb

    # ------------------------------------------------------------- 联动/参数
    def _sync_pairing_enabled(self):
        if hasattr(self, "cmb_pairing"):
            self.cmb_pairing.setEnabled(self.cmb_input_mode.currentData() == "multi")

    def _apply_threshold_mode(self):
        fixed = self.chk_fixed.isChecked()
        for e in self._thr_edits.values():
            e.setEnabled(fixed)

    def _show_adaptive_note(self):
        text = (
            "帧间隔波动阈值，30FPS 为 10ms，60FPS 为 5ms；\n"
            "同行对齐阈值，30FPS 为 10ms，60FPS 为 5ms；\n"
            "相机同步阈值，RGB&IR 相机是 1ms，depth 与 depth_rgb 是 5ms，"
            "depth_rgb 与 RGB 是 5ms；固定输入模式下，Depth 和 Depth RGB 共用深度同步阈值；\n"
            "自适应模式下，IMU 同步阈值按当前录制JSON声明频率计算：400Hz 为 2.5ms，"
            "500Hz 为 2ms，1000Hz 为 1ms；没有匹配的录制JSON时，按IMU时间戳计算，"
            "并在±15Hz内吸附到标称频率；固定输入模式使用 IMU同步阈值框中的数值。"
        )
        dlg = QDialog(self)
        dlg.setWindowTitle("阈值自适应说明")
        dlg.resize(560, 260)
        lay = QVBoxLayout(dlg)
        lab = QLabel(text)
        lab.setWordWrap(True)
        lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lab.setStyleSheet("font-size:13px;")
        lay.addWidget(lab)
        btn = QPushButton("关闭")
        btn.setFixedWidth(96)
        btn.clicked.connect(dlg.accept)
        lay.addWidget(btn, alignment=Qt.AlignRight)
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.show()

    def _pick_root(self):
        d = QFileDialog.getExistingDirectory(self, "选择数据路径", self.ed_root.text())
        if d:
            self.ed_root.setText(d)

    def set_root(self, path: str) -> None:
        """外部（如「日志导出」完成后）预填数据目录，等待用户手动开始。

        外部预填意味着用户要手动看这一次导出结果；如果当前停在定时模式，
        自动切回目录分析，避免「数据目录变了但开始按钮不能用」。
        """
        if path:
            self.ed_root.setText(path)
            if hasattr(self, "radio_scheduled") and self.radio_scheduled.isChecked():
                self.radio_manual.setChecked(True)

    def _pick_out(self):
        d = QFileDialog.getExistingDirectory(self, "选择输出路径", self.ed_out.text())
        if d:
            self.ed_out.setText(d)

    def _reset_params(self):
        self.cmb_input_mode.setCurrentIndex(0)
        self.cmb_layout_mode.setCurrentIndex(0)
        self.cmb_pairing.setCurrentIndex(0)
        self.cmb_workers.setCurrentIndex(0)
        self._sync_pairing_enabled()
        self.chk_adaptive.setChecked(True)
        self.chk_fixed.setChecked(False)
        defaults = {"interval_ms": "10", "row_align_ms": "10",
                    "cam_sync_ms": "1", "depth_sync_ms": "5", "imu_sync_ms": "1"}
        for k, val in defaults.items():
            self._thr_edits[k].setText(val)
        self.chk_depth_sync.setChecked(True)
        self.chk_sei.setChecked(False)
        self.chk_audio.setChecked(True)
        self.chk_meta.setChecked(True)
        self.ed_audio_target.clear()
        self.ed_audio_drift.clear()
        self.chk_audio_sha256.setChecked(False)
        self.chk_oos.setChecked(False)
        self.chk_syn.setChecked(False)
        self.ed_oos_groups.clear()
        self.ed_capture_parts.clear()
        self.sp_syn_groups.setValue(10)
        self._apply_threshold_mode()
        self.log("已恢复默认参数")

    def _collect_params(self):
        def fv(key, cast, default):
            try:
                return cast(self._thr_edits[key].text().strip()) or default
            except (ValueError, KeyError):
                return default

        def optional_float(edit):
            text = edit.text().strip()
            return float(text) if text else None

        return {
            "input_mode": self.cmb_input_mode.currentData(),
            "layout_mode": self.cmb_layout_mode.currentData(),
            "pairing_level": self.cmb_pairing.currentData(),
            "analysis_workers": self.cmb_workers.currentData(),
            "adaptive_thresholds": self.chk_adaptive.isChecked(),
            "depth_sync": self.chk_depth_sync.isChecked(),
            "frame_count_ms": 1.0,
            "interval_ms": fv("interval_ms", float, 10.0),
            "row_align_ms": fv("row_align_ms", float, 10.0),
            "cam_sync_ms": fv("cam_sync_ms", float, 1.0),
            "depth_sync_ms": fv("depth_sync_ms", float, 5.0),
            "imu_sync_ms": fv("imu_sync_ms", float, 1.0),
            "rgb_drop_mult": 1.5,
            "imu_drop_mult": 1.5,
            "audio": self.chk_audio.isChecked(),
            "meta_check": self.chk_meta.isChecked(),
            "audio_target_s": optional_float(self.ed_audio_target),
            "audio_drift_ppm": optional_float(self.ed_audio_drift),
            "audio_sha256": self.chk_audio_sha256.isChecked(),
        }

    # ------------------------------------------------------------------ 运行
    def _validate(self, root, params, *, silent=False):
        def warn(title, message, focus=None):
            if not silent:
                QMessageBox.warning(self, title, message)
            if focus is not None:
                focus.setFocus()
            return False

        if not self.chk_adaptive.isChecked() and not self.chk_fixed.isChecked():
            return warn("提示", "请选择「阈值自适应」或「阈值固定输入」后再开始分析")
        if not root or not os.path.isdir(root):
            return warn("提示", "请先选择有效的数据目录")
        if self.chk_fixed.isChecked():
            edit = self._thr_edits["depth_sync_ms"]
            try:
                ok = edit.hasAcceptableInput() and 0.001 <= float(edit.text().strip()) <= 1_000_000
            except ValueError:
                ok = False
            if not ok:
                return warn("提示", "深度同步阈值须为 0.001～1000000 ms，最多 3 位小数", edit)
        if (params.get("audio") and params.get("audio_drift_ppm") is not None
                and params.get("audio_target_s") is None):
            return warn("提示", "实测音频时钟漂移 ppm 必须与目标时长同时填写",
                        self.ed_audio_target)
        return True

    def _collect_options(self, *, warn_capture=True):
        """收集帧截取 / SEI 选项；参数非法时返回 None。"""
        try:
            oos_limit = parse_oos_group_limit(self.ed_oos_groups.text())
            part_limit = parse_capture_part_limit(self.ed_capture_parts.text())
        except ValueError as exc:
            if warn_capture:
                QMessageBox.warning(self, "提示", describe_exception(exc))
            return None
        options = {
            "whitelist": None,
            "capture_oos": self.chk_oos.isChecked(),
            "oos_group_limit": oos_limit,
            "capture_synced": self.chk_syn.isChecked(),
            "capture_part_limit": part_limit,
            "sei_align": self.chk_sei.isChecked(),
            "synced_groups": int(self.sp_syn_groups.value()),
        }
        if (options["capture_oos"] or options["capture_synced"]) and not fe.ffmpeg_available():
            if warn_capture:
                QMessageBox.warning(self, "提示", "勾选了帧截取但未找到 ffmpeg，将跳过帧截取功能。")
            options["capture_oos"] = False
            options["capture_synced"] = False
        return options

    def _start(self):
        """目录分析（手动）入口：现有流程保持不变。"""
        if self._worker is not None and self._worker.isRunning():
            return
        if self._schedule_batch:
            self._append_log("[跳过] 定时批量分析进行中，请等待完成")
            return
        root = self.ed_root.text().strip()
        params = self._collect_params()
        if not self._validate(root, params):
            return
        options = self._collect_options()
        if options is None:
            return
        out = self.ed_out.text().strip() or root
        self._run_analysis(root, out, params, options, source="manual")

    def _run_analysis(self, root, out, params, options, *, source="manual",
                      scheduled_for=None, extra_ctx=None) -> bool:
        """手动与定时共用的启动入口。

        定时任务使用保存时的参数快照，不读取当前界面控件，避免用户临时
        切换阈值模式 / 勾选检查项影响无人值守运行；这里只复核数据目录。
        """
        if self._worker is not None and self._worker.isRunning():
            self._append_log("[跳过] 已有分析任务在运行")
            return False
        if source == "scheduled":
            if not root or not os.path.isdir(root):
                self._append_log(f"[定时] 数据目录不存在：{root or '(空)'}")
                return False
            if not isinstance(params, dict) or not params:
                self._append_log("[定时] 定时参数为空，请重新保存定时设置")
                return False
        elif not self._validate(root, params):
            return False
        self._history_pending = False
        self._run_ctx = {
            "root": root, "out": out, "started": time.monotonic(),
            "source": source, "scheduled_for": scheduled_for,
        }
        if isinstance(extra_ctx, dict):
            self._run_ctx.update(extra_ctx)
        self._scheduler.set_busy(True)
        try:
            worker = AnalyzerWorker(root, out, params, options)
        except Exception as exc:  # noqa: BLE001
            self._on_failed(f"启动分析失败：{exc}")
            return False

        self.btn_run.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_pause.setText("⏸ 暂停")
        self.btn_stop.setEnabled(True)
        self.progress.setValue(0)
        self.txt_log.clear()
        label = "定时分析" if str(source).startswith("scheduled") else "开始分析"
        self.log(f"{label}：{root}")
        if scheduled_for:
            self._append_log(f"[定时] 本次计划时刻：{format_dt(scheduled_for)}")

        self._worker = worker
        self._worker.log.connect(self._append_log)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.stopped.connect(self._on_stopped)
        try:
            self._worker.start()
        except Exception as exc:  # noqa: BLE001
            self._worker = None
            self._reset_run_buttons()
            self._append_log(f"[分析失败] 启动分析线程失败：{exc}")
            self._finish_schedule("failed_to_start", error=f"启动分析线程失败：{exc}")
            return False
        return True

    # ------------------------------------------------------------- 定时分析
    def _apply_schedule_to_ui(self):
        """启动时把已保存的定时配置回填到界面。"""
        settings = self._scheduler.settings
        roots = list(settings.data_roots)
        if not roots and settings.data_root:
            roots = [settings.data_root]
        self._set_schedule_roots(roots)
        if roots and not self.ed_root.text().strip():
            self.ed_root.setText(roots[0])
        if settings.out_root and not self.ed_out.text().strip():
            self.ed_out.setText(settings.out_root)
        time_text, date_text = schedule_field_defaults(
            settings, from_disk=self._schedule_file_existed)
        parsed_time = QTime.fromString(time_text, "HH:mm")
        self.ed_sched_time.setTime(parsed_time if parsed_time.isValid()
                                   else QTime(2, 0))
        self.cmb_sched_repeat.blockSignals(True)
        self.cmb_sched_repeat.setCurrentIndex(
            0 if settings.repeat == REPEAT_DAILY else 1)
        self.cmb_sched_repeat.blockSignals(False)
        if date_text:
            one_day = QDate.fromString(date_text, "yyyy-MM-dd")
            if one_day.isValid():
                self.ed_sched_date.setDate(one_day)
        self.chk_sched_enabled.setChecked(bool(settings.enabled))
        self.chk_sched_catchup.setChecked(bool(settings.catch_up))
        self.chk_sched_independent_out.setChecked(bool(settings.independent_output))
        self.radio_manual.blockSignals(True)
        self.radio_scheduled.blockSignals(True)
        if settings.mode == MODE_SCHEDULED:
            self.radio_scheduled.setChecked(True)
        else:
            self.radio_manual.setChecked(True)
        self.radio_manual.blockSignals(False)
        self.radio_scheduled.blockSignals(False)
        self._sync_schedule_date(refresh_past=False)
        self.grp_schedule.setVisible(settings.mode == MODE_SCHEDULED)
        self._sync_mode_widgets(settings.mode == MODE_SCHEDULED)
        self.btn_run.setEnabled(settings.mode == MODE_MANUAL)
        self._update_schedule_labels()

    def _sync_mode_widgets(self, scheduled):
        """定时模式用列表管数据目录，隐藏上方单目录输入行，避免两处冲突。"""
        if hasattr(self, "_root_label"):
            self._root_label.setVisible(not scheduled)
        if hasattr(self, "_root_row_widget"):
            self._root_row_widget.setVisible(not scheduled)
            parent = self._root_row_widget.parentWidget()
            if parent is not None:
                parent.updateGeometry()
        if hasattr(self, "grp_schedule"):
            self.grp_schedule.updateGeometry()
        self.updateGeometry()
        self.update()

    def _on_mode_toggled(self, manual_checked):
        """手动 / 定时模式切换；只影响后续触发，不打断已在跑的分析。"""
        scheduled = not manual_checked
        self.grp_schedule.setVisible(scheduled)
        self._sync_mode_widgets(scheduled)
        self.grp_schedule.updateGeometry()
        parent = self.grp_schedule.parentWidget()
        if parent is not None:
            parent.updateGeometry()
        running = self._worker is not None and self._worker.isRunning()
        self.btn_run.setEnabled(not scheduled and not running and not self._schedule_batch)
        self._scheduler.set_mode(MODE_SCHEDULED if scheduled else MODE_MANUAL)
        if scheduled and self.lst_sched_roots.count() == 0:
            self._append_log("[定时] 请先在「定时数据目录」列表中添加目录")
        self._update_schedule_labels()

    def _on_sched_enabled_toggled(self, checked):
        """勾选启用时先把当前表单保存下来，保证“下次运行”与界面一致。"""
        if not hasattr(self, "_scheduler"):
            return
        if checked:
            if not self._save_schedule(auto=True):
                self.chk_sched_enabled.blockSignals(True)
                self.chk_sched_enabled.setChecked(False)
                self.chk_sched_enabled.blockSignals(False)
                self._scheduler.set_enabled(False)
                self._append_log("[定时] 启用失败：请检查数据目录与参数")
                self.status_message.emit("启用定时分析失败：请检查数据目录与参数")
        else:
            # 暂停立即生效，不要求先保存表单里的时间/参数修改。
            self._scheduler.set_enabled(False)
        self._update_schedule_labels()

    def _save_schedule(self, _checked=False, *, auto=False) -> bool:
        """保存定时规则 + 当前参数快照；失败返回 False。"""
        roots = self._schedule_roots_ui()
        if not roots:
            if not auto:
                QMessageBox.warning(self, "提示", "请先添加至少一个「定时数据目录」")
            return False
        invalid = [path for path in roots if not os.path.isdir(path)]
        if invalid:
            if not auto:
                QMessageBox.warning(
                    self, "提示", "以下定时数据目录不存在：\n" + "\n".join(invalid))
            return False
        params = self._collect_params()
        if not self._validate(roots[0], params, silent=auto):
            return False
        options = self._collect_options(warn_capture=not auto)
        if options is None:
            return False
        repeat = self.cmb_sched_repeat.currentData()
        date_text = None
        if repeat == REPEAT_ONCE:
            one_day = self.ed_sched_date.date().toPython()
            one_clock = self.ed_sched_time.time().toPython()
            if datetime.combine(one_day, one_clock) <= datetime.now():
                if not auto:
                    QMessageBox.warning(self, "提示", "「仅一次」的触发时间已过，请选择未来时间")
                return False
            date_text = self.ed_sched_date.date().toString("yyyy-MM-dd")
        old = self._scheduler.settings
        new = ScheduleSettings(
            mode=MODE_SCHEDULED,
            enabled=self.chk_sched_enabled.isChecked(),
            repeat=repeat,
            time=self.ed_sched_time.time().toString("HH:mm"),
            date=date_text,
            data_roots=roots,
            data_root=roots[0],
            out_root=self.ed_out.text().strip(),
            independent_output=self.chk_sched_independent_out.isChecked(),
            catch_up=self.chk_sched_catchup.isChecked(),
            grace_minutes=old.grace_minutes,
            params=params,
            options=options,
            last_trigger_for=old.last_trigger_for,
            last_run=old.last_run,
        )
        self._scheduler.update_settings(new)
        self._update_schedule_labels()
        prefix = "启用定时分析" if auto else "设置已保存"
        self._append_log(
            f"[定时] {prefix}：{len(roots)} 个目录；{self._scheduler.status_text()}")
        return True

    def _run_schedule_now(self):
        """用当前界面参数立即跑一次，不改变已保存的定时计划。"""
        if self._schedule_batch:
            QMessageBox.warning(self, "提示", "已有定时批量分析正在进行")
            return
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.warning(self, "提示", "已有分析任务在运行")
            return
        roots = self._schedule_roots_ui()
        if not roots and self.ed_root.text().strip():
            roots = [self.ed_root.text().strip()]
        if not roots:
            QMessageBox.warning(self, "提示", "请先添加至少一个「定时数据目录」")
            return
        invalid = [path for path in roots if not os.path.isdir(path)]
        if invalid:
            QMessageBox.warning(
                self, "提示", "以下定时数据目录不存在：\n" + "\n".join(invalid))
            return
        params = self._collect_params()
        if not self._validate(roots[0], params):
            return
        options = self._collect_options()
        if options is None:
            return
        self._schedule_batch = {
            "occurrence": None,
            "roots": roots,
            "index": 0,
            "runs": [],
            "started": time.monotonic(),
            "batch_stamp": time.strftime("%Y%m%d_%H%M%S"),
            "source": "scheduled_now",
            "out_base": self.ed_out.text().strip(),
            "independent_output": self.chk_sched_independent_out.isChecked(),
            "params": params,
            "options": options,
        }
        self._append_log(f"[定时] 立即运行 {len(roots)} 个目录")
        self._start_next_scheduled()

    def _pause_schedule(self):
        """立即停用定时（不要求先保存表单里的时间/参数修改）。"""
        # 勾选框的 toggled 信号会立即写入 enabled=false。
        self.chk_sched_enabled.setChecked(False)
        self._update_schedule_labels()
        self._append_log("[定时] 定时分析已暂停")

    def _update_schedule_labels(self):
        if not hasattr(self, "_scheduler"):
            return
        settings = self._scheduler.settings
        if hasattr(self, "chk_sched_enabled"):
            self.chk_sched_enabled.blockSignals(True)
            self.chk_sched_enabled.setChecked(bool(settings.enabled))
            self.chk_sched_enabled.blockSignals(False)
        if settings.mode == MODE_SCHEDULED and not self._scheduler.available:
            next_text = "下次运行：定时不可用（另一个实例正在负责）"
        elif settings.mode != MODE_SCHEDULED:
            next_text = "下次运行：定时分析已暂停（目录分析模式）"
        elif not settings.enabled:
            next_text = "下次运行：定时分析未启用"
        elif settings.next_run:
            next_text = f"下次运行：{format_dt(settings.next_run)}"
        else:
            next_text = "下次运行：—"
        self.lbl_sched_next.setText(next_text)
        self.lbl_sched_next.setToolTip(self._scheduler.status_text())
        self.lbl_sched_last.setText(self._scheduler.last_run_text())
        run = settings.last_run if isinstance(settings.last_run, dict) else {}
        report = str(run.get("report") or "")
        error = str(run.get("error") or "")
        runs = run.get("runs") if isinstance(run.get("runs"), list) else []
        if runs:
            lines = []
            for item in runs:
                if not isinstance(item, dict):
                    continue
                status = STATUS_LABELS.get(
                    str(item.get("status")), str(item.get("status") or "—"))
                detail = str(item.get("report") or item.get("error") or "")
                line = f"{item.get('root') or '(未知目录)'}：{status}"
                if detail:
                    line += f"\n    {detail}"
                lines.append(line)
            tip = "\n".join(lines)
        else:
            tip = report or error
        self.lbl_sched_last.setToolTip(
            tip or "定时运行结束后在这里显示状态与耗时")
        self.btn_sched_pause.setEnabled(bool(settings.enabled))

    def _on_schedule_status(self, text):
        self.status_message.emit(text)
        self._update_schedule_labels()

    def _scheduled_out_dir(self, batch, index, root):
        """为批次中的某个目录创建独立输出目录。"""
        base = batch.get("out_base") or root
        if not batch.get("independent_output", True):
            return base
        stamp = batch.get("batch_stamp") or time.strftime("%Y%m%d_%H%M%S")
        total = len(batch.get("roots") or [])
        if total <= 1:
            name = f"scheduled_{stamp}"
        else:
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "_",
                          os.path.basename(str(root).rstrip("\\/"))) or "root"
            name = f"scheduled_{stamp}_{index + 1:02d}_{safe[:40]}"
        out = os.path.join(base, name)
        try:
            os.makedirs(out, exist_ok=True)
        except OSError as exc:  # noqa: BLE001
            self._append_log(f"[定时] 创建输出目录失败，改用 {base}：{exc}")
            return base
        return out

    def _on_schedule_due(self, payload):
        """调度器到点回调：把保存快照里的多个目录排成批次逐个跑。"""
        data = payload if isinstance(payload, dict) else {}
        occurrence = data.get("occurrence")
        try:
            raw = data.get("settings") if isinstance(data.get("settings"), dict) else {}
            roots = raw.get("data_roots")
            if not isinstance(roots, list):
                roots = [raw.get("data_root")] if raw.get("data_root") else []
            roots = [str(item).strip() for item in roots
                     if isinstance(item, str) and str(item).strip()]
            seen = set()
            roots = [item for item in roots if not (item in seen or seen.add(item))]
            if not roots:
                self._append_log("[定时] 没有配置定时数据目录，本次跳过")
                self._scheduler.mark_finished(
                    "failed_to_start", occurrence=occurrence,
                    error="没有配置定时数据目录")
                self._update_schedule_labels()
                return
            if self._schedule_batch:
                self._append_log("[定时] 上一批仍在进行，本次跳过")
                self._scheduler.mark_finished(
                    "skipped_busy", occurrence=occurrence,
                    error="上一批仍在进行")
                self._update_schedule_labels()
                return
            if self._worker is not None and self._worker.isRunning():
                self._append_log("[定时] 已有分析任务在运行，本次跳过")
                self._scheduler.mark_finished(
                    "skipped_busy", occurrence=occurrence,
                    error="已有分析任务")
                self._update_schedule_labels()
                return
            self._schedule_batch = {
                "occurrence": occurrence,
                "roots": roots,
                "index": 0,
                "runs": [],
                "started": time.monotonic(),
                "batch_stamp": time.strftime("%Y%m%d_%H%M%S"),
                "source": "scheduled",
                "out_base": str(raw.get("out_root") or "").strip(),
                "independent_output": bool(raw.get("independent_output", True)),
                "params": raw.get("params") if isinstance(raw.get("params"), dict) else {},
                "options": raw.get("options") if isinstance(raw.get("options"), dict) else {},
            }
            self._append_log(f"[定时] 到达计划时刻，共 {len(roots)} 个目录")
            self._start_next_scheduled()
        except Exception as exc:  # noqa: BLE001 —— 定时槽函数不能把异常抛进事件循环
            self._schedule_batch = None
            self._append_log(f"[定时] 触发失败：{exc}")
            self._scheduler.mark_finished(
                "failed_to_start", occurrence=occurrence, error=str(exc))
            self._update_schedule_labels()

    def _start_next_scheduled(self):
        """启动批次里的下一个目录；无效目录跳过，全部结束后统一收尾。"""
        batch = self._schedule_batch
        if not batch:
            return
        if self._closing:
            self._finalize_schedule_batch()
            return
        while batch["index"] < len(batch["roots"]):
            index = batch["index"]
            root = batch["roots"][index]
            batch["index"] += 1
            if not root or not os.path.isdir(root):
                batch["runs"].append({
                    "root": root, "status": "failed_to_start",
                    "error": "数据目录不存在", "report": "",
                })
                self._append_log(f"[定时] 目录不存在，跳过：{root or '(空)'}")
                continue
            out = self._scheduled_out_dir(batch, index, root)
            extra = {
                "batch_index": index,
                "batch_total": len(batch["roots"]),
                "batch_root": root,
                "batch_out": out,
            }
            self.status_message.emit(
                f"定时分析 {index + 1}/{len(batch['roots'])}：{root}")
            if self._run_analysis(
                    root, out, batch.get("params") or {},
                    batch.get("options") or {},
                    source=batch.get("source", "scheduled"),
                    scheduled_for=batch.get("occurrence"),
                    extra_ctx=extra):
                return
            busy = self._worker is not None and self._worker.isRunning()
            batch["runs"].append({
                "root": root,
                "status": "skipped_busy" if busy else "failed_to_start",
                "error": "已有分析任务" if busy else "启动失败",
                "report": "",
            })
            if busy:
                self._finalize_schedule_batch()
                return
        self._finalize_schedule_batch()

    def _finalize_schedule_batch(self):
        """批次结束：汇总状态写回调度器（多目录时附 runs 明细）。"""
        batch = self._schedule_batch
        if not batch:
            return
        self._schedule_batch = None
        runs = batch.get("runs") or []
        duration = time.monotonic() - (batch.get("started") or time.monotonic())
        if not runs:
            overall = "failed_to_start"
        elif any(item.get("status") == "partial" for item in runs):
            overall = "partial"
        elif all(item.get("status") == "finished" for item in runs):
            overall = "finished"
        elif any(item.get("status") == "finished" for item in runs):
            overall = "partial_success"
        elif any(item.get("status") == "skipped_busy" for item in runs):
            overall = "skipped_busy"
        else:
            overall = "failed"
        report = ""
        for item in reversed(runs):
            if item.get("report"):
                report = item["report"]
                break
        error = next((item.get("error") for item in runs if item.get("error")), "")
        self._scheduler.mark_finished(
            overall, occurrence=batch.get("occurrence"), duration_s=duration,
            report=report, error=error, runs=runs if len(runs) > 1 else None)
        self._update_schedule_labels()
        ok = sum(1 for item in runs if item.get("status") == "finished")
        self._append_log(f"[定时] 批次结束：{ok}/{len(runs)} 个目录成功")
        self.status_message.emit(f"定时分析结束：{ok}/{len(runs)} 个目录成功")
        # 多个目录按顺序跑，只有整批结束才弹一次窗；单目录定时任务同样在这里收尾。
        self._notify_batch_done(batch, runs)

    def _finish_schedule(self, status, report="", error=""):
        """分析结束统一回写调度状态；批次中则记录并启动下一个目录。"""
        ctx = self._run_ctx if isinstance(self._run_ctx, dict) else {}
        occurrence = ctx.get("scheduled_for")
        duration = None
        if ctx.get("started"):
            duration = time.monotonic() - ctx["started"]
        batch = self._schedule_batch
        if batch:
            batch["runs"].append({
                "root": ctx.get("root", ""),
                "status": status,
                "report": report or "",
                "error": error or "",
                "duration_s": round(duration, 1) if duration is not None else None,
                "out_root": ctx.get("out", ""),
            })
            if status == "partial" or self._closing:
                self._finalize_schedule_batch()
            else:
                self._append_log(
                    f"[定时] 目录 {ctx.get('root', '')} 完成：{status}，继续下一个")
                self._start_next_scheduled()
            return
        self._scheduler.mark_finished(
            status, occurrence=occurrence, duration_s=duration,
            report=report or "", error=error)
        self._update_schedule_labels()
        if occurrence:
            label = {"finished": "成功", "partial": "已停止（部分报告）",
                     "failed": "失败"}.get(status, status)
            self.status_message.emit(f"定时分析结束：{label}")

    def _toggle_pause(self):
        w = self._worker
        if w is None or not w.isRunning():
            return
        if w.is_paused():
            w.resume()
            self.btn_pause.setText("⏸ 暂停")
            self.log("[已继续]")
        else:
            w.pause()
            self.btn_pause.setText("▶ 继续")
            self.log("[已暂停]")

    def _stop_run(self):
        w = self._worker
        if w is None or not w.isRunning():
            return
        w.stop()
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.log("[正在停止…]")

    def _reset_run_buttons(self):
        self.btn_run.setEnabled(
            self.radio_manual.isChecked() and not self._schedule_batch)
        self.btn_pause.setEnabled(False)
        self.btn_pause.setText("⏸ 暂停")
        self.btn_stop.setEnabled(False)

    def _on_progress(self, name, i, total):
        total = max(1, total)
        self.progress.setRange(0, total)
        self.progress.setValue(max(0, min(total, i)))
        if name:
            self.lbl_summary.setText(f"分析中：{name}")

    def _on_done(self, results, out_root, html_path):
        self._results = results
        self._out_root = out_root
        self._html_path = html_path
        self._reset_run_buttons()
        self.progress.setValue(self.progress.maximum())
        stats = rep.compute_stats(results)
        self.lbl_summary.setText(
            f"Part 总数 <b>{stats['total']}</b> ｜ 正常 <b style='color:#059669'>{stats['normal']}</b> ｜ "
            f"异常 <b style='color:#dc2626'>{stats['abnormal']}</b> ｜ "
            f"执行异常 <b>{stats['errors']}</b> ｜ 未验证 <b>{stats['unverified']}</b> ｜ "
            f"合格时长 <b>{stats['pass_s']:.1f}s</b> ｜ 不合格时长 <b style='color:#dc2626'>{stats['fail_s']:.1f}s</b>")
        self._fill_table(results)
        self.btn_open_html.setEnabled(True)
        self.btn_open_dir.setEnabled(True)
        self.log(f"HTML 报告：{html_path}")
        # 用户停止时引擎仍会先发 done（带部分报告），随后再发 stopped；
        # 这里交给 _on_stopped 统一登记 partial，不能误报“分析完成”。
        if self._worker is not None and self._worker.was_stopped():
            self.lbl_summary.setText(
                "<b style='color:#b45309'>分析已停止，已生成部分报告</b>")
            return
        in_batch = self._schedule_batch is not None
        self._record_history("finished", results, out_root, html_path)
        self._finish_schedule("finished", html_path)
        if not in_batch:
            self._notify_analysis_done(results, out_root, html_path)

    def _notify_analysis_done(self, results, out_root, html_path):
        """单目录分析完成弹窗：可直接打开 HTML 报告或输出文件夹。"""
        if self._closing:
            return
        lines = []
        if results:
            stats = rep.compute_stats(results)
            lines.append(
                f"Part 总数 {stats['total']} ｜ 正常 {stats['normal']} ｜ "
                f"异常 {stats['abnormal']} ｜ 执行异常 {stats['errors']} ｜ "
                f"未验证 {stats['unverified']}")
            lines.append(
                f"合格时长 {stats['pass_s']:.1f}s ｜ 不合格时长 {stats['fail_s']:.1f}s")
            lines.append("")
        lines.append(f"HTML 报告：{html_path or '（未生成）'}")
        if html_path:
            lines.append("")
            lines.append("是否打开 HTML 报告？")
        self._show_finish_dialog(
            "分析完成", "分析完成", "\n".join(lines), html_path,
            out_root or (os.path.dirname(html_path) if html_path else ""))

    def _notify_batch_done(self, batch, runs):
        """定时/批量分析只在全部目录结束后弹一次窗。"""
        if self._closing or not runs:
            return
        total = len(runs)
        ok = sum(1 for item in runs if item.get("status") == "finished")
        partial = sum(1 for item in runs if item.get("status") == "partial")
        failed = total - ok - partial
        manual_now = str(batch.get("source") or "") == "scheduled_now"
        prefix = "分析" if manual_now else "定时分析"
        if ok == total:
            headline = f"{prefix}全部完成：{ok}/{total} 个目录成功"
        else:
            headline = f"{prefix}结束：{ok}/{total} 个目录成功"
            if partial:
                headline += f"，{partial} 个仅生成部分报告"
            if failed:
                headline += f"，{failed} 个失败"
        lines = []
        last_report = ""
        for item in runs:
            status = STATUS_LABELS.get(
                str(item.get("status")), str(item.get("status") or "—"))
            root = str(item.get("root") or "").strip()
            name = os.path.basename(root.rstrip("\\/")) or root or "(未知目录)"
            line = f"• {name}：{status}"
            detail = str(item.get("error") or "").strip().splitlines()
            detail = detail[0] if detail else ""
            if len(detail) > 120:
                detail = detail[:117] + "..."
            if detail:
                line += f" — {detail}"
            lines.append(line)
            if item.get("report"):
                last_report = str(item["report"])
        shown = lines[:12]
        if len(lines) > 12:
            shown.append(f"… 其余 {len(lines) - 12} 个目录见运行日志")
        details = "\n".join(shown)
        if last_report:
            details += (f"\n\n最近一份 HTML 报告：\n{last_report}"
                        "\n\n是否打开 HTML 报告？")
        else:
            details += "\n\n本次没有生成 HTML 报告。"
        out_base = str(batch.get("out_base") or "").strip()
        if not out_base:
            outs = [str(item.get("out_root") or "").strip()
                    for item in runs if item.get("out_root")]
            out_base = outs[-1] if outs else (
                os.path.dirname(last_report) if last_report else "")
        self._show_finish_dialog(
            f"{prefix}完成" if ok == total else f"{prefix}结束",
            headline, details, last_report, out_base)

    def _show_finish_dialog(self, title, headline, details, report_path, out_dir):
        """结束提示：打开 HTML 报告 / 打开输出文件夹 / 关闭。"""
        if self._closing:
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle(title)
        box.setText(headline)
        box.setInformativeText(details)
        box.setTextInteractionFlags(Qt.TextSelectableByMouse)
        report_btn = None
        dir_btn = None
        if report_path and os.path.isfile(report_path):
            report_btn = box.addButton("打开 HTML 报告", QMessageBox.AcceptRole)
        if out_dir and os.path.isdir(out_dir):
            dir_btn = box.addButton("打开输出文件夹", QMessageBox.ActionRole)
        close_btn = box.addButton("关闭", QMessageBox.RejectRole)
        box.setDefaultButton(report_btn or close_btn)
        # 定时分析可能在最小化时结束；闪一下任务栏，避免弹窗藏在后面没人看见。
        window = self.window()
        if window is not None and not window.isActiveWindow():
            QApplication.alert(window, 0)
        box.exec()
        clicked = box.clickedButton()
        if clicked is report_btn:
            QDesktopServices.openUrl(QUrl.fromLocalFile(report_path))
        elif clicked is dir_btn:
            QDesktopServices.openUrl(QUrl.fromLocalFile(out_dir))

    def _on_failed(self, msg):
        message = str(msg or "").strip() or "错误：未知异常，请查看运行日志。"
        if "错误：" not in message:
            message = "错误：" + message
        self._append_log(f"[分析失败] {message}")
        self._reset_run_buttons()
        self.lbl_summary.setText("<b style='color:#dc2626'>分析失败，请查看运行日志与错误提示。</b>")
        scheduled = str(self._run_ctx.get("source") or "").startswith("scheduled")
        if not scheduled:
            QMessageBox.critical(self, "分析失败", message)
        else:
            self.status_message.emit(f"定时分析失败：{message}")
        self._finish_schedule("failed", "", error=message)

    # ------------------------------------------------------------------ 历史
    def _record_history(self, status, results, out_root, html_path):
        """把这次分析登记到「会话记录」。

        报告写入/结果序列化可能耗时（大目录的 Part 很多时要几秒），放到后台
        线程做，主线程立即返回；写完后用排队调用回到主线程发信号刷新列表。
        失败只记日志，不影响分析结果本身。
        """
        if self._history_pending:
            return
        self._history_pending = True
        ctx = dict(self._run_ctx)
        duration = None
        if ctx.get("started"):
            duration = round(time.monotonic() - ctx["started"], 1)

        def work():
            try:
                from ego_relay import history
                summary = history.summarize_results(results) if results else {}
                parts = history.summarize_parts(results) if results else []
                labels = history.device_labels_of(results) if results else []
                kind = "flat" if parts and len(parts) > 1 else "single"
                entry = history.record_analysis(
                    kind=kind, root=ctx.get("root", ""),
                    out_root=out_root or ctx.get("out", ""),
                    html_path=html_path or "", status=status,
                    summary=summary, parts=parts, device_labels=labels,
                    duration_s=duration,
                    note="定时任务" if ctx.get("source") == "scheduled" else "",
                )
                QMetaObject.invokeMethod(
                    self, "_on_history_written", Qt.QueuedConnection,
                    Q_ARG(str, entry.get("id", "")))
            except Exception as exc:  # noqa: BLE001
                self.log(f"写入会话记录失败：{exc}")

        threading.Thread(target=work, name="history-write", daemon=True).start()

    @Slot(str)
    def _on_history_written(self, session_id):
        """后台线程写完后回到主线程：允许下一次登记并发通知主窗口刷新。"""
        self._history_pending = False
        if session_id:
            self.history_recorded.emit(session_id)

    def _on_stopped(self):
        self._reset_run_buttons()
        self._append_log("[分析已停止] 已生成部分报告")
        self.lbl_summary.setText("<b style='color:#b45309'>分析已停止，已生成部分报告</b>")
        self._record_history("partial", [], self._out_root, self._html_path)
        self._finish_schedule("partial", self._html_path)

    # ------------------------------------------------------------------ 结果
    def _fill_table(self, results):
        cols = rep.ReportColumns(results)
        headers = cols.headers()
        self.table.clear()
        self.table.setColumnCount(len(headers))
        self.table.setRowCount(len(results))
        self.table.setHorizontalHeaderLabels(headers)
        for ri, r in enumerate(results):
            cells = cols.row_cells(r)
            for ci, val in enumerate(cells):
                item = QTableWidgetItem(val)
                if r.whitelisted:
                    item.setForeground(QColor("#86909c"))
                else:
                    v = str(val)
                    if v.startswith("PASS"):
                        item.setForeground(QColor("#059669"))
                    elif v.startswith("FAIL"):
                        item.setForeground(QColor("#dc2626"))
                    elif not getattr(r, "overall_pass", True):
                        item.setForeground(QColor("#b91c1c"))
                self.table.setItem(ri, ci, item)

    def _on_cell_double_clicked(self, row, col):
        if not self._results or row < 0 or row >= len(self._results):
            return
        r = self._results[row]
        cols = rep.ReportColumns(self._results)
        details = cols.row_details(r)
        if col >= len(details) or not details[col]:
            self._append_log(f"Part {r.name} 该单元格无展开详情")
            return
        blocks = rep.detail_blocks(details[col])
        if not blocks:
            self._append_log(f"Part {r.name} 该单元格无展开详情")
            return
        self._show_detail_dialog(r.name, blocks)

    def _show_detail_dialog(self, name, blocks):
        dlg = QDialog(self)
        titles = " · ".join(t for t, _h, _r in blocks if t)
        dlg.setWindowTitle(f"详情 — {name}{' — ' + titles if titles else ''}")
        dlg.resize(820, 520)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel("数据行号从 1 开始；CSV 表头不计入，第一条时间戳记录为第 1 行。"))
        for title, headers, rows in blocks:
            if title:
                cap = QLabel(title)
                cap.setStyleSheet("font-weight:600;")
                lay.addWidget(cap)
            tbl = QTableWidget(len(rows), len(headers))
            tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
            tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
            tbl.setAlternatingRowColors(True)
            tbl.setHorizontalHeaderLabels(headers)
            tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
            for ri, rowvals in enumerate(rows):
                for ci, v in enumerate(rowvals):
                    item = QTableWidgetItem(str(v))
                    if isinstance(v, (int, float)):
                        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    tbl.setItem(ri, ci, item)
            lay.addWidget(tbl)
        close_btn = QPushButton("关闭")
        close_btn.setFixedWidth(96)
        close_btn.clicked.connect(dlg.accept)
        lay.addWidget(close_btn, alignment=Qt.AlignRight)
        dlg.exec()

    # ------------------------------------------------------------------ 动作
    def _open_html(self):
        from pathlib import Path
        if self._html_path and Path(self._html_path).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._html_path))

    def _open_outdir(self):
        if self._out_root and os.path.isdir(self._out_root):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._out_root))

    def _append_log(self, msg):
        self.txt_log.appendPlainText(msg)

    # ------------------------------------------------------------------ 收尾
    def stop_for_close(self, timeout_ms=CLOSE_WAIT_MS):
        """窗口关闭前协作式停止后台分析，等待部分报告收尾。"""
        self._closing = True
        w = self._worker
        if w is None or not w.isRunning():
            if self._schedule_batch:
                self._finalize_schedule_batch()
            self._scheduler.stop()
            return True
        self._append_log("[退出] 正在停止后台分析并等待部分报告收尾…")
        w.stop()
        finished = w.wait() if timeout_ms is None else w.wait(timeout_ms)
        self._append_log("[退出] 后台分析线程已结束" if finished
                         else "[退出] 后台分析线程未在时限内结束")
        if finished:
            self._scheduler.stop()
        else:
            self._closing = False
        return finished
