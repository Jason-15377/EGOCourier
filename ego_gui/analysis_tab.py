"""日志分析 Tab：导入 + 综合分析 + 可视化（video_analyzer 功能布局）。

顶部选择目录/zip → 开始分析。后台线程同时跑两条管线：
  ① EGO 现有管道 `parsers.import_path` → `analysis.align_and_analyze`（落库，供「会话记录」页可见）；
  ② `video_analyzer.scan_and_analyze`（处理目录内 MP4/MCAP/*_pts.csv，做 SEI 比对、丢帧、
     左右 Color 同步、IMU 同步）。

流式输出：分析过程中的每一步（EGO 逐文件解析、video_analyzer 各文件夹/各流处理）都会实时
追加到「实时分析日志」面板，不是等全部跑完才一次性显示。

依赖提示：日志/CSV 分析开箱即用；若目录含 MP4/MCAP 而本机未装 ffmpeg，会先弹出安装说明，
避免新电脑上“点完没反应”。
"""

from __future__ import annotations

import io
import os
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor, QBrush, QTextCursor, QTextCharFormat
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QFileDialog, QTableWidget,
    QTableWidgetItem, QHeaderView, QLabel, QMessageBox, QTabWidget,
    QAbstractItemView, QStackedWidget, QPlainTextEdit, QGroupBox,
)
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
import matplotlib.pyplot as plt

from ego_relay import parsers, analysis, db, export as export_mod, deps, cache


# ---------------------------------------------------------------------------
# 通用格式化
# ---------------------------------------------------------------------------
def _fmt(us):
    if not us:
        return "-"
    return (datetime.fromtimestamp(us / 1e6, tz=timezone.utc)
            .astimezone().strftime("%Y-%m-%d %H:%M:%S"))


def _fmt_full(us):
    if not us:
        return "-"
    return (datetime.fromtimestamp(us / 1e6, tz=timezone.utc)
            .astimezone().strftime("%Y-%m-%d %H:%M:%S.%f"))[:-3]


def _pct(r):
    return f"{r * 100:.3f}%"


def _clean_ts(ts_list):
    return sorted({int(t) for t in ts_list if t})


def _scan_video_files(path: str) -> list:
    """返回目录里 MP4/MCAP 文件列表（用于缺 ffmpeg 时的提示）。"""
    p = Path(path)
    if not p.is_dir():
        return []
    return [f for f in p.rglob("*")
            if f.is_file() and f.suffix.lower() in (".mp4", ".mcap")]


def find_ego_viewer_data_dirs() -> list:
    """定位 EGOViewer 安装目录下的 data 文件夹（含 recordings/、logs/、firmware-logs/）。"""
    roots = (r"D:\Soft&tools\软件工具", r"D:\Soft&tools", r"D:\file")
    out = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            entries = list(os.scandir(root))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir() and e.name.startswith("EGOViewer_") and "win_x64" in e.name:
                    d = os.path.join(e.path, "data")
                    if os.path.isdir(os.path.join(d, "logs")):
                        out.append(d)
            except OSError:
                continue
    return out


# ---------------------------------------------------------------------------
# 视图模型：把两类分析结果统一成便于表格/图表渲染的 dict
# ---------------------------------------------------------------------------
def _video_to_model(results) -> dict:
    streams, sei_csv, sync, imu_sync = [], [], None, []
    for fr in results:
        for scr in fr.sei_csv_results:
            sei_csv.append({
                "mp4": scr.mp4_name, "codec": scr.codec, "tier": scr.extraction_tier,
                "sei_count": scr.sei_count, "csv_count": scr.csv_count,
                "count_match": scr.count_match, "match": scr.match, "error": scr.error,
                "mismatches": [
                    {"table": m.table, "idx": m.index, "source": m.source_val,
                     "closest": m.closest_val, "dev": m.deviation_us}
                    for m in scr.mismatches],
            })
        for s in fr.streams:
            streams.append({
                "name": s.source_name, "type": s.source_type, "codec": s.codec,
                "total": s.total_frames, "rate_hz": s.expected_rate_hz,
                "duration_s": s.duration_s, "actual_hz": s.actual_rate_hz,
                "sei": s.sei_extracted, "gaps": s.total_gaps, "lost": s.total_lost_frames,
                "error": s.error,
                "ts": list(s.timestamps_us),
                "gap_list": [
                    {"prev": g.prev_frame_index, "curr": g.curr_frame_index,
                     "pt": g.prev_timestamp_us, "ct": g.curr_timestamp_us,
                     "gap_us": g.gap_us, "exp_us": g.expected_interval_us,
                     "lost": g.estimated_lost_frames}
                    for g in s.gaps],
            })
        if fr.sync_result:
            sr = fr.sync_result
            sync = {
                "left": sr.left_name, "right": sr.right_name,
                "total_left": sr.total_left, "total_right": sr.total_right,
                "matched": sr.matched_pairs, "avg_diff_us": sr.avg_diff_us,
                "max_diff_us": sr.max_diff_us, "threshold_us": sr.threshold_us,
                "is_synced": sr.is_synced,
                "left_ts": list(sr.left_timestamps), "right_ts": list(sr.right_timestamps),
                "unpaired": [
                    {"side": u.side, "idx": u.left_idx or u.right_idx,
                     "ts": u.left_ts_us or u.right_ts_us,
                     "closest": u.closest_other_ts_us}
                    for u in sr.unpaired],
            }
        for ir in fr.imu_sync_results:
            imu_sync.append({
                "imu": ir.imu_name, "video": ir.video_name,
                "imu_total": ir.imu_total, "video_total": ir.video_total,
                "avg_off": ir.avg_offset_us, "max_off": ir.max_offset_us,
                "min_off": ir.min_offset_us, "threshold": ir.threshold_us,
                "is_synced": ir.is_synced,
                "outliers": [{"v": o.video_ts_us, "i": o.imu_ts_us, "off": o.offset_us}
                             for o in ir.outliers],
            })
    return {"source_kind": "video", "streams": streams, "sei_csv": sei_csv,
            "sync": sync, "imu_sync": imu_sync}


def _egosession_to_model(session_id: int) -> dict:
    s = db.get_session(session_id)
    stats = (s or {}).get("meta", {}).get("stats", {})
    sc = stats.get("script", {})
    frames = db.session_frames(session_id)
    sides = {}
    for f in frames:
        sides.setdefault(f["side"], []).append(f)

    streams = []
    for side, fl in sides.items():
        fl.sort(key=lambda x: x["frame_index"])
        ts = _clean_ts(f["hw_ptp_us"] for f in fl)
        rate = analysis._auto_rate(ts) if len(ts) >= 2 else 30.0
        _, lost, gaps = analysis._detect_gaps(ts, rate) if ts else (0, 0, [])
        dur = (ts[-1] - ts[0]) / 1e6 if len(ts) >= 2 else 0
        streams.append({
            "name": f"{side} (hw_ptp)", "type": "video_pts", "codec": "",
            "total": len(ts), "rate_hz": rate, "duration_s": dur,
            "actual_hz": (len(ts) - 1) / dur if dur > 0 else 0,
            "sei": len(ts), "gaps": len(gaps), "lost": lost, "error": "",
            "ts": ts,
            "gap_list": [{"prev": g[0], "curr": g[1], "pt": ts[g[0] - 1] if g[0] - 1 < len(ts) else 0,
                          "ct": g[1], "gap_us": g[2], "exp_us": int(1e6 / rate),
                          "lost": g[3]} for g in gaps],
        })
    for side, v in (sc.get("streams") or {}).items():
        if any(st["name"].startswith(side) for st in streams):
            continue
        streams.append({
            "name": f"{side} (script)", "type": "video_pts", "codec": "",
            "total": v.get("total_frames", 0), "rate_hz": v.get("expected_hz", 0),
            "duration_s": v.get("duration_s", 0), "actual_hz": v.get("actual_hz", 0),
            "sei": v.get("total_frames", 0), "gaps": v.get("gaps", 0),
            "lost": v.get("lost_frames", 0), "error": "", "ts": [], "gap_list": [],
        })

    sync = None
    if "left" in sides and "right" in sides:
        lf = {f["frame_index"]: f["hw_ptp_us"] for f in sides["left"] if f["hw_ptp_us"]}
        rf = {f["frame_index"]: f["hw_ptp_us"] for f in sides["right"] if f["hw_ptp_us"]}
        common = sorted(set(lf) & set(rf))
        diffs = [abs(lf[k] - rf[k]) for k in common]
        thr = sc.get("stereo_sync", {}).get("threshold_us", 1000)
        sync = {
            "left": "left (hw_ptp)", "right": "right (hw_ptp)",
            "total_left": len(lf), "total_right": len(rf),
            "matched": len(common), "avg_diff_us": (sum(diffs) / len(diffs)) if diffs else 0,
            "max_diff_us": max(diffs) if diffs else 0, "threshold_us": thr,
            "is_synced": (sc.get("stereo_sync", {}).get("is_synced", True)
                          if sc.get("stereo_sync") else (not diffs or max(diffs) <= thr)),
            "left_ts": [lf[k] for k in common], "right_ts": [rf[k] for k in common],
            "unpaired": [],
        }

    imu_sync = []
    iv = sc.get("imu_video_sync") or {}
    if iv:
        imu_sync.append({
            "imu": "imu", "video": "video", "imu_total": iv.get("total", 0),
            "video_total": stats.get("total_frames", 0),
            "avg_off": iv.get("avg_off_us", 0), "max_off": iv.get("max_off_us", 0),
            "min_off": iv.get("min_off_us", 0), "threshold": iv.get("threshold_us", 2000),
            "is_synced": iv.get("is_synced", True),
            "outliers": [{"v": o[0], "i": o[1], "off": o[2]} for o in iv.get("outliers", [])],
        })

    sei_csv = []
    for side, v in (sc.get("sei_vs_csv") or {}).items():
        sei_csv.append({
            "mp4": f"{side} SEI vs CSV", "codec": "", "tier": 0,
            "sei_count": v.get("sei_count", 0), "csv_count": v.get("csv_count", 0),
            "count_match": v.get("count_match", True), "match": v.get("match", True),
            "error": "", "mismatches": [], "side": side,
        })

    return {"source_kind": "ego", "streams": streams, "sei_csv": sei_csv,
            "sync": sync, "imu_sync": imu_sync}


def _build_model(session_id: int, video_model: dict | None) -> dict:
    if video_model and (video_model["streams"] or video_model["sei_csv"]):
        return video_model
    return _egosession_to_model(session_id)


# ---------------------------------------------------------------------------
# 图表
# ---------------------------------------------------------------------------
def _setup_zh(ax):
    try:
        plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
        plt.rcParams["axes.unicode_minus"] = False
    except Exception:
        pass


def _drops_figure(model: dict) -> "plt.Figure":
    streams = [s for s in model["streams"]
               if s["type"] in ("mp4_video", "mcap_video", "video_pts") and not s["error"]]
    fig, ax = plt.subplots(figsize=(10, 4.4), dpi=100)
    _setup_zh(ax)
    plotted = 0
    for s in streams:
        ts = s.get("ts") or []
        if len(ts) < 2:
            continue
        t0 = ts[0]
        rel = [(t - t0) / 1e6 for t in ts]
        ax.plot(range(len(rel)), rel, lw=0.8, label=s["name"])
        for g in s.get("gap_list", []):
            ct = g.get("ct")
            if ct and t0:
                ax.axvline(g.get("curr", 0), color="red", lw=0.6, alpha=0.4)
        plotted += 1
    if not plotted:
        ax.text(0.5, 0.5, "无逐帧时间戳数据", ha="center", va="center",
                transform=ax.transAxes, color="#6b7280")
    ax.set_xlabel("帧序号")
    ax.set_ylabel("相对时间 (s)")
    ax.set_title("视频/时间戳流（红竖线为丢帧事件位置）")
    if plotted:
        ax.legend(fontsize=8, loc="upper left")
    fig.tight_layout()
    return fig


def _sync_figure(model: dict) -> "plt.Figure":
    sync = model.get("sync")
    fig, ax = plt.subplots(figsize=(10, 4.2), dpi=100)
    _setup_zh(ax)
    if sync and sync.get("left_ts") and sync.get("right_ts"):
        lts, rts = sync["left_ts"], sync["right_ts"]
        base = min(lts[0], rts[0])
        ax.plot(range(len(lts)), [(t - base) / 1e6 for t in lts], lw=0.9,
                label=f"左 {sync.get('left', '')}", color="#2563eb")
        ax.plot(range(len(rts)), [(t - base) / 1e6 for t in rts], lw=0.9,
                label=f"右 {sync.get('right', '')}", color="#f97316")
        ax.set_xlabel("帧序号")
        ax.set_ylabel("相对时间 (s)")
        ax.set_title(f"左右 Color 时间戳（匹配 {sync.get('matched', 0)} 对，"
                     f"平均偏差 {sync.get('avg_diff_us', 0):.0f}µs）")
        ax.legend(fontsize=8, loc="upper left")
    else:
        ax.text(0.5, 0.5, "无左右配对数据", ha="center", va="center",
                transform=ax.transAxes, color="#6b7280")
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# 流式 stdout → 信号（把 video_analyzer 的 print 实时转发到 GUI 日志）
# ---------------------------------------------------------------------------
class _StreamSignal(io.TextIOBase):
    def __init__(self, emit):
        super().__init__()
        self._emit = emit
        self._buf = ""

    def writable(self):
        return True

    def write(self, s):
        if not s:
            return 0
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip("\r")
            if line.strip():
                try:
                    self._emit(line)
                except Exception:
                    pass
        return len(s)

    def flush(self):
        if self._buf.strip():
            try:
                self._emit(self._buf.strip())
            except Exception:
                pass
        self._buf = ""


# ---------------------------------------------------------------------------
# 后台分析线程
# ---------------------------------------------------------------------------
class AnalysisWorker(QThread):
    file_parsed = Signal(str, str, int, str)
    progressed = Signal(int, int, int)
    progress_line = Signal(str)     # 流式日志行
    finished = Signal(int, dict, bool)   # session_id, model, used_cache
    cancelled = Signal()
    failed = Signal(str)

    def __init__(self, path: str):
        super().__init__()
        self.path = path
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def _want_cancel(self) -> bool:
        return self._cancelled

    def run(self):
        try:
            from contextlib import redirect_stdout

            sig = cache.source_signature(self.path)

            # 命中缓存：来源未变化 → 直接复用上次结果，跳过重算
            existing = db.find_session_by_source(self.path)
            if existing is not None:
                meta_sig = (db.get_session(existing) or {}).get("meta", {}).get("source_sig")
                if meta_sig == sig:
                    self.progress_line.emit("[缓存] 来源未变化，复用上次分析结果（跳过重算）")
                    video_model = cache.load_video_cache(sig) or None
                    model = _build_model(existing, video_model)
                    self.finished.emit(existing, model, True)
                    return

            # ① EGO 管道：导入并落库
            self.progress_line.emit(f"[EGO] 解析 {Path(self.path).name} …")
            parsed = parsers.import_path(self.path)
            total = len(parsed.files)
            succ = sum(1 for f in parsed.files if f.get("parsed"))
            fail = total - succ
            for f in parsed.files:
                self.file_parsed.emit(f.get("file", ""), f.get("source_type", ""),
                                      f.get("count", 0), f.get("note") or "")
                self.progress_line.emit(
                    f"[EGO] 文件 {f.get('file', '')} -> {f.get('count', 0)} 行 "
                    f"({'OK' if f.get('parsed') else 'SKIP'})")
            self.progressed.emit(total, succ, fail)
            if self._cancelled:
                self.cancelled.emit()
                return
            r = analysis.align_and_analyze(parsed)
            sid = r["session_id"]
            self.progress_line.emit(f"[EGO] 对齐完成：会话 #{sid}，{succ}/{total} 个文件解析成功")

            # ② video_analyzer：仅对目录做原始视频/CSV 分析（zip 不做）
            video_model = None
            if Path(self.path).is_dir():
                vids = _scan_video_files(self.path)
                if vids:
                    deps.ensure_ffmpeg_on_path()   # 确保视频分析的 ffmpeg 子进程可找到
                    self.progress_line.emit(
                        f"[视频] 检测到 {len(vids)} 个 MP4/MCAP，开始 video_analyzer 分析…")
                    try:
                        from ego_relay import video_analyzer
                        with redirect_stdout(_StreamSignal(self.progress_line.emit)):
                            vres = video_analyzer.scan_and_analyze(
                                self.path, threshold_multiplier=1.5, verbose=False,
                                should_cancel=self._want_cancel)
                        if not self._cancelled:
                            video_model = _video_to_model(vres)
                            cache.save_video_cache(sig, video_model)
                            self.progress_line.emit(
                                f"[视频] 完成：{len(video_model['streams'])} 条数据流，"
                                f"{len(video_model['sei_csv'])} 组 SEI 比对（已缓存）")
                    except Exception as ve:  # noqa: BLE001
                        video_model = None
                        self.progress_line.emit(f"[视频] 视频分析失败，已跳过: {ve}")
                else:
                    self.progress_line.emit("[视频] 目录内未发现 MP4/MCAP，跳过视频分析")
            else:
                self.progress_line.emit("[视频] zip 包仅做 EGO 日志分析，跳过视频分析")

            if self._cancelled:
                self.cancelled.emit()
                return

            # 记录来源签名，供下次命中缓存
            db.update_session_meta(sid, source_sig=sig)

            model = _build_model(sid, video_model)
            self.finished.emit(sid, model, False)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            self.progress_line.emit(f"[错误] {type(e).__name__}: {e}")
            self.failed.emit(f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
class AnalysisTab(QWidget):
    """日志分析页：导入 + 流式分析 + 可视化。"""
    imported = Signal(int)
    open_in_sessions = Signal(int)

    def __init__(self, log):
        super().__init__()
        self.log = log
        self.worker: AnalysisWorker | None = None
        self._path = ""
        self._last_sid: int | None = None
        self._model: dict = {}
        self._build_ui()
        self._update_deps()
        self.stack.setCurrentIndex(0)

    # ------------------------------------------------------------- 界面构建
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 6)
        root.setSpacing(8)

        # ---- 顶部工具栏 ----
        top = QHBoxLayout()
        self.btn_pick_dir = QPushButton("选择目录…")
        self.btn_pick_dir.clicked.connect(lambda: self._pick("dir"))
        self.btn_pick_zip = QPushButton("选择 zip…")
        self.btn_pick_zip.clicked.connect(lambda: self._pick("zip"))
        self.btn_ev = QPushButton("从 EGOViewer 导入")
        self.btn_ev.setToolTip("自动定位本机 EGOViewer 的 data 目录并直接分析（免 SSH）")
        self.btn_ev.clicked.connect(self._import_ego_viewer)
        self.btn_analyze = QPushButton("开始分析")
        self.btn_analyze.setProperty("role", "primary")
        self.btn_analyze.setEnabled(False)
        self.btn_analyze.clicked.connect(self._analyze)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel)
        top.addWidget(self.btn_pick_dir)
        top.addWidget(self.btn_pick_zip)
        top.addWidget(self.btn_ev)
        top.addWidget(self.btn_analyze)
        top.addWidget(self.btn_cancel)
        top.addSpacing(12)
        # 依赖状态
        self.lbl_deps = QLabel("")
        top.addWidget(self.lbl_deps)
        top.addStretch(1)
        self.lbl_state = QLabel("空闲")
        top.addWidget(self.lbl_state)
        self.btn_open_sess = QPushButton("在会话记录中打开")
        self.btn_open_sess.setEnabled(False)
        self.btn_open_sess.clicked.connect(
            lambda: self.open_in_sessions.emit(self._last_sid) if self._last_sid else None)
        top.addWidget(self.btn_open_sess)
        root.addLayout(top)

        # ---- 结果区 ----
        self.stack = QStackedWidget()
        root.addWidget(self.stack, stretch=1)

        # 占位页
        self.placeholder = QLabel(
            "请选择日志目录或 zip 包后点击「开始分析」。\n\n"
            "分析将同时：\n"
            "  ① 用 EGO 管道解析 SEI/MP4-pts/IMU/EGOViewer 日志并对齐（写入会话记录）；\n"
            "  ② 用 video_analyzer 处理目录内 MP4/MCAP/*_pts.csv，做 SEI 比对、丢帧、\n"
            "     左右 Color 同步、IMU 同步。\n\n"
            "分析过程会实时输出到「实时分析日志」，结果按功能分页展示。")
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setWordWrap(True)
        ph_wrap = QWidget()
        ph_l = QVBoxLayout(ph_wrap)
        ph_l.addWidget(self.placeholder)
        self.stack.addWidget(ph_wrap)

        self._build_result_page()
        self.stack.addWidget(self._result_page)
        self.stack.setCurrentIndex(0)

    def _build_result_page(self) -> None:
        self._result_page = QWidget()
        rv = QVBoxLayout(self._result_page)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(8)

        # 结果标题 + 导出
        tb = QHBoxLayout()
        self.lbl_result = QLabel("")
        self.lbl_result.setStyleSheet("font-weight:600;")
        tb.addWidget(self.lbl_result)
        tb.addStretch(1)
        self.btn_report = QPushButton("导出 HTML 报告")
        self.btn_report.setEnabled(False)
        self.btn_report.clicked.connect(self._export_report)
        self.btn_zip = QPushButton("导出 zip")
        self.btn_zip.setEnabled(False)
        self.btn_zip.clicked.connect(self._export_zip)
        tb.addWidget(self.btn_report)
        tb.addWidget(self.btn_zip)
        rv.addLayout(tb)

        # 实时分析日志（流式输出）
        grp = QGroupBox("实时分析日志（流式输出）")
        gv = QVBoxLayout(grp)
        self.progress_box = QPlainTextEdit()
        self.progress_box.setReadOnly(True)
        self.progress_box.setMaximumHeight(150)
        self.progress_box.setPlaceholderText("分析过程将在此实时显示…")
        gv.addWidget(self.progress_box)
        rv.addWidget(grp)

        # 结果子页
        self.sub = QTabWidget()
        self._build_drops_tab()
        self._build_sync_tab()
        self._build_seicsv_tab()
        self._build_imu_tab()
        self._build_stats_tab()
        self._build_files_tab()
        rv.addWidget(self.sub, stretch=1)

    def _new_table(self, headers, stretch_col=0):
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.horizontalHeader().setSectionResizeMode(stretch_col, QHeaderView.Stretch)
        t.setAlternatingRowColors(True)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        return t

    def _build_drops_tab(self):
        tab = QWidget()
        v = QVBoxLayout(tab)
        self._drops_canvas = QVBoxLayout()
        self._drops_canvas.setContentsMargins(0, 0, 0, 0)
        v.addLayout(self._drops_canvas)
        self.drops_table = self._new_table(
            ["数据源", "类型", "总帧数", "预期帧率", "时长(s)", "实际帧率", "丢帧事件", "丢失帧", "丢帧率", "状态"])
        v.addWidget(self.drops_table, stretch=1)
        self.drops_detail = self._new_table(
            ["数据源", "上一帧#", "本帧#", "上一帧时间戳", "本帧时间戳", "间隔(µs)", "预期间隔(µs)", "估计丢帧"])
        self.drops_detail.setMaximumHeight(220)
        v.addWidget(self.drops_detail)
        self.sub.addTab(tab, "丢帧检测")

    def _build_sync_tab(self):
        tab = QWidget()
        v = QVBoxLayout(tab)
        self._sync_canvas = QVBoxLayout()
        self._sync_canvas.setContentsMargins(0, 0, 0, 0)
        v.addLayout(self._sync_canvas)
        self.sync_table = self._new_table(
            ["左源", "右源", "左帧", "右帧", "匹配对", "平均偏差(µs)", "最大偏差(µs)", "阈值(µs)", "状态"])
        v.addWidget(self.sync_table)
        self.sync_unpaired = self._new_table(
            ["侧", "帧号", "时间戳", "最接近另一侧", "偏差(µs)"])
        self.sync_unpaired.setMaximumHeight(180)
        v.addWidget(self.sync_unpaired)
        self.sub.addTab(tab, "左右 Color 同步")

    def _build_seicsv_tab(self):
        tab = QWidget()
        v = QVBoxLayout(tab)
        self.seicsv_table = self._new_table(
            ["MP4/数据源", "编码", "SEI 数量", "CSV 数量", "数量一致", "内容一致", "状态"])
        v.addWidget(self.seicsv_table)
        self.seicsv_detail = self._new_table(
            ["数据源", "方向", "序号", "源时间戳", "最接近时间戳", "偏差(µs)"])
        self.seicsv_detail.setMaximumHeight(240)
        v.addWidget(self.seicsv_detail)
        self.sub.addTab(tab, "SEI vs CSV 比对")

    def _build_imu_tab(self):
        tab = QWidget()
        v = QVBoxLayout(tab)
        self.imu_table = self._new_table(
            ["IMU 源", "视频源", "IMU 帧", "视频帧", "平均偏移(µs)", "最大偏移(µs)", "最小偏移(µs)", "阈值(µs)", "状态"])
        v.addWidget(self.imu_table)
        self.imu_detail = self._new_table(
            ["IMU-视频", "视频时间戳", "最近 IMU 时间戳", "偏移(µs)"])
        self.imu_detail.setMaximumHeight(220)
        v.addWidget(self.imu_detail)
        self.sub.addTab(tab, "IMU 同步")

    def _build_stats_tab(self):
        tab = QWidget()
        v = QVBoxLayout(tab)
        self.lbl_dq = QLabel("")
        self.lbl_dq.setWordWrap(True)
        self.lbl_dq.setStyleSheet("color:#6b7280; padding:2px;")
        v.addWidget(self.lbl_dq)
        self.stats_table = self._new_table(
            ["数据源", "类型", "总帧数", "预期帧率(Hz)", "时长(s)", "实际帧率(Hz)", "SEI 提取", "丢帧/丢失", "说明"])
        v.addWidget(self.stats_table)
        self.sub.addTab(tab, "帧统计")

    def _build_files_tab(self):
        tab = QWidget()
        v = QVBoxLayout(tab)
        self.file_table = self._new_table(["文件", "类型", "行数", "结果", "说明"])
        v.addWidget(self.file_table)
        self.sub.addTab(tab, "来源文件")

    def _set_canvas(self, holder, figure, height=300):
        while holder.count():
            item = holder.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        c = FigureCanvasQTAgg(figure)
        c.setMinimumHeight(height)
        holder.addWidget(c)

    # ------------------------------------------------------------- 依赖状态
    def _update_deps(self):
        d = deps.check()
        parts = []
        parts.append(f"ffmpeg {'✓' if d['ffmpeg'] else '✗'}")
        parts.append(f"mcap {'✓' if d['mcap'] else '–'}")
        parts.append(f"numpy {'✓' if d['numpy'] else '–'}")
        if d["ffmpeg"]:
            self.lbl_deps.setText("依赖: " + "  ".join(parts))
            self.lbl_deps.setStyleSheet("color:#047857;")
        else:
            self.lbl_deps.setText("依赖: " + "  ".join(parts) + "  (视频分析需 ffmpeg)")
            self.lbl_deps.setStyleSheet("color:#b45309;")

    # ------------------------------------------------------------- 操作
    def _pick(self, mode):
        if mode == "dir":
            path = QFileDialog.getExistingDirectory(self, "选择本地日志/录像文件夹")
        else:
            path, _ = QFileDialog.getOpenFileName(self, "选择 zip 日志包",
                                                  "", "ZIP 压缩包 (*.zip)")
        if path:
            self._path = path
            self.btn_analyze.setEnabled(True)
            self.log(f"已选择: {path}")
            self._check_dep_prompt(path)

    def _import_ego_viewer(self):
        """定位本机 EGOViewer 的 data 目录，直接走标准分析管道（免 SSH）。"""
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "提示", "已有分析任务进行中")
            return
        dirs = find_ego_viewer_data_dirs()
        if not dirs:
            QMessageBox.warning(
                self, "未找到 EGOViewer",
                "未在常见目录找到 EGOViewer（D:\\Soft&tools 等）。\n"
                "请确认已安装 EGOViewer，或用「选择目录…」手动导入其 data 文件夹。")
            return
        data_dir = dirs[0]
        self._path = data_dir
        self.btn_analyze.setEnabled(True)
        self.log(f"从 EGOViewer 导入并分析: {data_dir}")
        self._check_dep_prompt(data_dir)
        self._analyze()

    def _check_dep_prompt(self, path):
        """选择目录后若含视频但缺 ffmpeg，先给提示与安装说明。"""
        vids = _scan_video_files(path)
        if not vids or deps.has_ffmpeg():
            return
        msg = QMessageBox(self)
        msg.setWindowTitle("缺少 ffmpeg")
        msg.setIcon(QMessageBox.Warning)
        msg.setText(
            f"所选目录包含 {len(vids)} 个 MP4/MCAP 视频文件，但本机未安装 ffmpeg。\n\n"
            "视频 SEI 提取、丢帧检测、帧截取需要 ffmpeg/ffprobe；\n"
            "日志/CSV 分析不受影响，仍可正常使用。")
        btn_help = msg.addButton("查看安装说明", QMessageBox.ActionRole)
        msg.addButton("知道了", QMessageBox.AcceptRole)
        msg.exec()
        if msg.clickedButton() is btn_help:
            QMessageBox.information(self, "ffmpeg 安装说明", deps.ffmpeg_install_help())

    def _analyze(self):
        if not self._path:
            return
        self.log(f"开始分析: {self._path}")
        self.lbl_state.setText("分析中…")
        self.btn_analyze.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.btn_open_sess.setEnabled(False)
        self.btn_report.setEnabled(False)
        self.btn_zip.setEnabled(False)
        self.progress_box.clear()
        self.worker = AnalysisWorker(self._path)
        self.worker.file_parsed.connect(self._on_file)
        self.worker.progressed.connect(self._on_progress)
        self.worker.progress_line.connect(self._append_progress)
        self.worker.finished.connect(self._on_done)
        self.worker.cancelled.connect(self._on_cancelled)
        self.worker.failed.connect(self._on_fail)
        self.worker.finished.connect(lambda *_: (self.btn_analyze.setEnabled(True),
                                                 self.btn_cancel.setEnabled(False)))
        self.worker.cancelled.connect(lambda: (self.btn_analyze.setEnabled(True),
                                               self.btn_cancel.setEnabled(False)))
        self.worker.failed.connect(lambda *_: (self.btn_analyze.setEnabled(True),
                                               self.btn_cancel.setEnabled(False)))
        self.worker.start()
        self.stack.setCurrentIndex(1)

    def _cancel(self):
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.btn_cancel.setEnabled(False)
            self.lbl_state.setText("取消中…")
            self._append_progress("[提示] 已请求取消，等待当前步骤结束…")
        else:
            self.log("当前没有运行中的分析任务")

    def _append_progress(self, line):
        cur = self.progress_box.textCursor()
        cur.movePosition(QTextCursor.End)
        fmt = QTextCharFormat()
        low = line.lower()
        if low.startswith("[错误]") or "error" in low or "失败" in line:
            fmt.setForeground(QColor("#dc2626"))
        elif "跳过" in line or "skip" in low or "warn" in low:
            fmt.setForeground(QColor("#d97706"))
        else:
            fmt.setForeground(QColor("#111827"))
        cur.insertText(line + "\n", fmt)
        self.progress_box.setTextCursor(cur)
        self.progress_box.ensureCursorVisible()

    def _on_file(self, file, kind, count, note):
        r = self.file_table.rowCount()
        self.file_table.insertRow(r)
        p = Path(file)
        self.file_table.setItem(r, 0, QTableWidgetItem(p.name))
        self.file_table.setItem(r, 1, QTableWidgetItem(kind or p.suffix.lstrip(".").upper()))
        self.file_table.setItem(r, 2, QTableWidgetItem(str(count)))
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
        self.file_table.setItem(r, 3, it)
        self.file_table.setItem(r, 4, QTableWidgetItem(tip))

    def _on_progress(self, total, ok, fail):
        self.lbl_state.setText(f"解析 {ok}/{total}")

    def _on_cancelled(self):
        self.lbl_state.setText("已取消")
        self._append_progress("[取消] 分析已取消")
        self.log("日志分析已取消")

    def _on_done(self, sid, model, used_cache):
        self._last_sid = sid
        self._model = model
        # 附上数据校验信息（垃圾时间戳统计，来自会话 stats）
        s0 = db.get_session(sid) or {}
        dq = (s0.get("meta") or {}).get("stats", {}).get("data_quality")
        if dq:
            self._model["data_quality"] = dq
        self.lbl_state.setText("完成" + ("（缓存）" if used_cache else ""))
        self.btn_open_sess.setEnabled(True)
        self.btn_report.setEnabled(True)
        self.btn_zip.setEnabled(True)
        src = "video_analyzer" if model.get("source_kind") == "video" else "EGO 会话"
        n_streams = len(model.get("streams", []))
        n_sei = len(model.get("sei_csv", []))
        self.lbl_result.setText(
            f"分析完成：会话 #{sid}（{src}） · {n_streams} 条数据流 · {n_sei} 组 SEI 比对"
            + (" · 来自缓存" if used_cache else ""))
        self._append_progress(f"[完成] 分析完成：会话 #{sid}，结果已生成"
                              + ("（复用缓存）" if used_cache else ""))
        self._fill_files_table(sid)
        self._render(model)
        self.log(f"日志分析完成: 会话 #{sid}, 数据流 {n_streams}, SEI比对 {n_sei}"
                 + ("（缓存）" if used_cache else ""))
        self.imported.emit(sid)

    def _fill_files_table(self, sid):
        """从会话 meta 的来源文件填充来源文件表（缓存命中时 _on_file 不会触发）。"""
        s = db.get_session(sid) or {}
        files = (s.get("meta") or {}).get("files", [])
        self.file_table.setRowCount(0)
        for f in files:
            r = self.file_table.rowCount()
            self.file_table.insertRow(r)
            p = Path(f.get("file", ""))
            self.file_table.setItem(r, 0, QTableWidgetItem(p.name))
            self.file_table.setItem(r, 1, QTableWidgetItem(f.get("source_type", "")))
            self.file_table.setItem(r, 2, QTableWidgetItem(str(f.get("count", ""))))
            ok = bool(f.get("parsed"))
            it = QTableWidgetItem("✅ 正常" if ok else "⚠️ 警告")
            it.setToolTip(f.get("note") or "")
            if not ok:
                it.setBackground(QBrush(QColor("#fff7e6")))
            self.file_table.setItem(r, 3, it)
            self.file_table.setItem(r, 4, QTableWidgetItem(f.get("note") or ""))

    def _on_fail(self, err):
        self.lbl_state.setText("失败")
        self.log(f"日志分析失败: {err}")
        self._append_progress(f"[错误] {err}")
        QMessageBox.warning(self, "分析失败", str(err))

    # ------------------------------------------------------------- 渲染
    def _render(self, model: dict) -> None:
        streams = model.get("streams", [])
        self._render_drops(model, streams)
        self._render_sync(model)
        self._render_seicsv(model)
        self._render_imu(model)
        self._render_stats(streams)

    def _render_drops(self, model, streams):
        self._set_canvas(self._drops_canvas, _drops_figure(model), 300)
        video = [s for s in streams
                 if s["type"] in ("mp4_video", "mcap_video", "video_pts")]
        self.drops_table.setRowCount(0)
        for s in video:
            r = self.drops_table.rowCount()
            self.drops_table.insertRow(r)
            rate = f"{s['rate_hz']:.1f}" if s["rate_hz"] else "-"
            act = f"{s['actual_hz']:.2f}" if s["actual_hz"] else "-"
            dur = f"{s['duration_s']:.3f}" if s["duration_s"] else "-"
            gap_rate = s["lost"] / max(1, s["total"]) * 100
            vals = [s["name"], s["type"], s["total"], rate, dur, act,
                    s["gaps"], s["lost"], _pct(gap_rate), ""]
            for c, v in enumerate(vals):
                self.drops_table.setItem(r, c, QTableWidgetItem(str(v)))
            bad = s["gaps"] > 0
            self.drops_table.setItem(r, 9, QTableWidgetItem("⚠ 丢帧" if bad else "✅ 正常"))
            if bad:
                for c in (6, 7):
                    self.drops_table.item(r, c).setBackground(QBrush(QColor("#fdecea")))
        self.drops_detail.setRowCount(0)
        for s in video:
            for g in s.get("gap_list", []):
                r = self.drops_detail.rowCount()
                self.drops_detail.insertRow(r)
                vals = [s["name"], g["prev"], g["curr"], _fmt_full(g["pt"]),
                        _fmt_full(g["ct"]), f"{g['gap_us']:,}", g["exp_us"], g["lost"]]
                for c, v in enumerate(vals):
                    self.drops_detail.setItem(r, c, QTableWidgetItem(str(v)))

    def _render_sync(self, model):
        self._set_canvas(self._sync_canvas, _sync_figure(model), 280)
        sync = model.get("sync")
        self.sync_table.setRowCount(0)
        self.sync_unpaired.setRowCount(0)
        if not sync:
            return
        r = self.sync_table.rowCount()
        self.sync_table.insertRow(r)
        ok = sync.get("is_synced", True)
        vals = [sync.get("left", "-"), sync.get("right", "-"),
                sync.get("total_left", 0), sync.get("total_right", 0),
                sync.get("matched", 0), f"{sync.get('avg_diff_us', 0):.1f}",
                sync.get("max_diff_us", 0), f"{sync.get('threshold_us', 0):.0f}", ""]
        for c, v in enumerate(vals):
            self.sync_table.setItem(r, c, QTableWidgetItem(str(v)))
        it = QTableWidgetItem("✅ 同步" if ok else "✗ 不同步")
        it.setForeground(QBrush(QColor("#047857" if ok else "#b91c1c")))
        self.sync_table.setItem(r, 8, it)
        for u in sync.get("unpaired", []):
            r2 = self.sync_unpaired.rowCount()
            self.sync_unpaired.insertRow(r2)
            closest = u.get("closest")
            dev = abs(closest - u["ts"]) if closest else 0
            vals = [u.get("side", ""), u.get("idx", ""), _fmt_full(u.get("ts")),
                    _fmt_full(closest) if closest else "-", dev]
            for c, v in enumerate(vals):
                self.sync_unpaired.setItem(r2, c, QTableWidgetItem(str(v)))

    def _render_seicsv(self, model):
        rows = model.get("sei_csv", [])
        self.seicsv_table.setRowCount(0)
        self.seicsv_detail.setRowCount(0)
        for x in rows:
            r = self.seicsv_table.rowCount()
            self.seicsv_table.insertRow(r)
            ok = bool(x.get("count_match") and x.get("match"))
            status = "✅ 一致" if ok else ("⚠ 数量不一致" if not x.get("count_match")
                                          else "✗ 内容不一致")
            vals = [x.get("mp4", "-"), x.get("codec", "-"), x.get("sei_count", 0),
                    x.get("csv_count", 0),
                    "是" if x.get("count_match") else "否",
                    "是" if x.get("match") else "否", status]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                if c == 6 and not ok:
                    it.setForeground(QBrush(QColor("#b91c1c")))
                self.seicsv_table.setItem(r, c, it)
            for m in x.get("mismatches", []):
                r2 = self.seicsv_detail.rowCount()
                self.seicsv_detail.insertRow(r2)
                vals = [x.get("mp4", "-"), "CSV→SEI" if m.get("table") == "csv_not_in_sei"
                        else "SEI→CSV", m.get("idx"), _fmt_full(m.get("source")),
                        _fmt_full(m.get("closest")), f"{m.get('dev', 0):+,}"]
                for c, v in enumerate(vals):
                    self.seicsv_detail.setItem(r2, c, QTableWidgetItem(str(v)))

    def _render_imu(self, model):
        rows = model.get("imu_sync", [])
        self.imu_table.setRowCount(0)
        self.imu_detail.setRowCount(0)
        for x in rows:
            r = self.imu_table.rowCount()
            self.imu_table.insertRow(r)
            ok = x.get("is_synced", True)
            vals = [x.get("imu", "-"), x.get("video", "-"), x.get("imu_total", 0),
                    x.get("video_total", 0), f"{x.get('avg_off', 0):.0f}",
                    f"{x.get('max_off', 0):,}", f"{x.get('min_off', 0):,}",
                    f"{x.get('threshold', 0):.0f}", ""]
            for c, v in enumerate(vals):
                self.imu_table.setItem(r, c, QTableWidgetItem(str(v)))
            it = QTableWidgetItem("✅ 同步" if ok else "✗ 不同步")
            it.setForeground(QBrush(QColor("#047857" if ok else "#b91c1c")))
            self.imu_table.setItem(r, 8, it)
            for o in x.get("outliers", []):
                r2 = self.imu_detail.rowCount()
                self.imu_detail.insertRow(r2)
                vals = [f"{x.get('imu', '-')} vs {x.get('video', '-')}",
                        _fmt_full(o.get("v")), _fmt_full(o.get("i")),
                        f"{o.get('off', 0):,}"]
                for c, v in enumerate(vals):
                    self.imu_detail.setItem(r2, c, QTableWidgetItem(str(v)))

    def _render_stats(self, streams):
        self.stats_table.setRowCount(0)
        dq = self._model.get("data_quality") or {}
        g = dq.get("garbage") or {}
        total_garbage = sum(g.values())
        if total_garbage:
            self.lbl_dq.setText(
                f"数据校验：共剔除 {total_garbage} 个异常时间戳（低于 {dq.get('floor_us', 0)}µs / 1973年）"
                f" —— hw_ptp {g.get('hw_ptp', 0)} · sei {g.get('sei', 0)} · imu {g.get('imu', 0)}"
                f" · viewer {g.get('viewer', 0)}，避免脏数据导致误判")
            self.lbl_dq.setStyleSheet("color:#b45309; padding:2px;")
        else:
            self.lbl_dq.setText("数据校验：未发现异常时间戳，数据质量正常")
            self.lbl_dq.setStyleSheet("color:#047857; padding:2px;")
        for s in streams:
            r = self.stats_table.rowCount()
            self.stats_table.insertRow(r)
            rate = f"{s['rate_hz']:.1f}" if s["rate_hz"] else "-"
            act = f"{s['actual_hz']:.2f}" if s["actual_hz"] else "-"
            dur = f"{s['duration_s']:.3f}" if s["duration_s"] else "-"
            note = s.get("error") or (f"{s['gaps']} 处丢帧 / {s['lost']} 帧丢失"
                                      if s["gaps"] else "正常")
            vals = [s["name"], s["type"], s["total"], rate, dur, act,
                    s.get("sei", ""), f"{s['gaps']}/{s['lost']}", note]
            for c, v in enumerate(vals):
                self.stats_table.setItem(r, c, QTableWidgetItem(str(v)))

    # ------------------------------------------------------------- 导出
    def _export_report(self):
        if not self._last_sid:
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出分析报告",
                                              f"report_session{self._last_sid}.html",
                                              "HTML 报告 (*.html)")
        if not path:
            return
        try:
            self._write_report(path)
            self.log(f"报告已导出: {path}")
            QMessageBox.information(self, "导出成功", f"分析报告已保存到:\n{path}")
        except Exception as e:  # noqa: BLE001
            self.log(f"报告导出失败: {e}")
            QMessageBox.warning(self, "导出失败", str(e))

    def _write_report(self, path: str) -> None:
        model = self._model
        s = db.get_session(self._last_sid) or {}
        lines = []
        lines.append("<html lang='zh'><head><meta charset='utf-8'>"
                     "<title>日志分析报告</title><style>"
                     "body{font-family:'Microsoft YaHei',sans-serif;background:#f5f6f8;color:#222;margin:0}"
                     "h1{background:#1f2937;color:#fff;padding:16px 24px;margin:0;font-size:20px}"
                     ".wrap{padding:20px;max-width:1200px;margin:0 auto}"
                     "h2{font-size:16px;border-bottom:1px solid #e5e7eb;padding-bottom:6px;margin:24px 0 10px}"
                     "table{border-collapse:collapse;width:100%;font-size:13px;background:#fff;margin-bottom:8px}"
                     "th,td{border:1px solid #e5e7eb;padding:6px 8px;text-align:left}"
                     "th{background:#f9fafb}.ok{color:#047857}.err{color:#b91c1c}"
                     "</style></head><body>")
        lines.append(f"<h1>日志分析报告 — 会话 #{self._last_sid}</h1><div class='wrap'>")
        lines.append(f"<p>来源路径: <code>{s.get('source_path', '-')}</code> · "
                     f"时间: {_fmt(s.get('start_us'))} ~ {_fmt(s.get('end_us'))} · "
                     f"trace: {(s.get('trace_id') or '-')[:16]}</p>")

        # 数据校验
        dq = model.get("data_quality") or {}
        g = dq.get("garbage") or {}
        total_garbage = sum(g.values())
        lines.append("<h2>数据校验（清洗层）</h2>")
        if total_garbage:
            lines.append(f"<p class='err'>已剔除 {total_garbage} 个异常时间戳（低于 "
                         f"{dq.get('floor_us', 0)}µs / 1973年）：hw_ptp {g.get('hw_ptp', 0)} · "
                         f"sei {g.get('sei', 0)} · imu {g.get('imu', 0)} · "
                         f"viewer {g.get('viewer', 0)}</p>")
        else:
            lines.append("<p class='ok'>未发现异常时间戳，数据质量正常</p>")

        lines.append("<h2>丢帧检测</h2>")
        if model.get("streams"):
            lines.append("<table><tr><th>数据源</th><th>类型</th><th>总帧</th><th>帧率(Hz)</th>"
                         "<th>时长(s)</th><th>丢帧事件</th><th>丢失帧</th><th>丢帧率</th></tr>")
            for st in [x for x in model["streams"]
                       if x["type"] in ("mp4_video", "mcap_video", "video_pts")]:
                gap_rate = st["lost"] / max(1, st["total"]) * 100
                cls = "err" if st["gaps"] else "ok"
                lines.append(f"<tr><td>{st['name']}</td><td>{st['type']}</td>"
                             f"<td>{st['total']}</td><td>{st['rate_hz']:.1f}</td>"
                             f"<td>{st['duration_s']:.3f}</td>"
                             f"<td class='{cls}'>{st['gaps']}</td>"
                             f"<td class='{cls}'>{st['lost']}</td><td>{gap_rate:.3f}%</td></tr>")
            lines.append("</table>")
        else:
            lines.append("<p>无丢帧检测数据</p>")

        lines.append("<h2>左右 Color 同步</h2>")
        sy = model.get("sync")
        if sy:
            cls = "ok" if sy.get("is_synced") else "err"
            lines.append(f"<p class='{cls}'>{'✅ 同步' if sy.get('is_synced') else '✗ 不同步'}"
                         f" · 匹配 {sy.get('matched', 0)} 对 · 平均偏差 {sy.get('avg_diff_us', 0):.1f}µs"
                         f" · 最大偏差 {sy.get('max_diff_us', 0)}µs · 阈值 {sy.get('threshold_us', 0):.0f}µs</p>")
            if sy.get("unpaired"):
                lines.append("<table><tr><th>侧</th><th>帧号</th><th>时间戳</th></tr>")
                for u in sy["unpaired"][:100]:
                    lines.append(f"<tr><td>{u.get('side')}</td><td>{u.get('idx')}</td>"
                                 f"<td>{_fmt_full(u.get('ts'))}</td></tr>")
                lines.append("</table>")
        else:
            lines.append("<p>无左右配对数据</p>")

        lines.append("<h2>SEI vs CSV 比对</h2>")
        if model.get("sei_csv"):
            lines.append("<table><tr><th>数据源</th><th>SEI</th><th>CSV</th><th>数量一致</th>"
                         "<th>内容一致</th></tr>")
            for x in model["sei_csv"]:
                cls = "ok" if (x.get("count_match") and x.get("match")) else "err"
                lines.append(f"<tr><td>{x.get('mp4')}</td><td>{x.get('sei_count', 0)}</td>"
                             f"<td>{x.get('csv_count', 0)}</td>"
                             f"<td class='{cls}'>{'是' if x.get('count_match') else '否'}</td>"
                             f"<td class='{cls}'>{'是' if x.get('match') else '否'}</td></tr>")
            lines.append("</table>")
        else:
            lines.append("<p>无 SEI 比对数据</p>")

        lines.append("<h2>IMU 同步</h2>")
        if model.get("imu_sync"):
            lines.append("<table><tr><th>IMU</th><th>视频</th><th>平均偏移</th><th>最大偏移</th>"
                         "<th>阈值</th><th>状态</th></tr>")
            for x in model["imu_sync"]:
                cls = "ok" if x.get("is_synced") else "err"
                lines.append(f"<tr><td>{x.get('imu')}</td><td>{x.get('video')}</td>"
                             f"<td>{x.get('avg_off', 0):.0f}µs</td><td>{x.get('max_off', 0)}µs</td>"
                             f"<td>{x.get('threshold', 0):.0f}µs</td>"
                             f"<td class='{cls}'>{'同步' if x.get('is_synced') else '不同步'}</td></tr>")
            lines.append("</table>")
        else:
            lines.append("<p>无 IMU 同步数据</p>")

        lines.append("<h2>来源文件</h2>")
        files = (s.get("meta") or {}).get("files", [])
        if files:
            lines.append("<table><tr><th>文件</th><th>类型</th><th>行数</th><th>结果</th></tr>")
            for f in files:
                lines.append(f"<tr><td>{Path(f.get('file', '')).name}</td>"
                             f"<td>{f.get('source_type', '')}</td><td>{f.get('count', '')}</td>"
                             f"<td>{'✅' if f.get('parsed') else '⚠️'}</td></tr>")
            lines.append("</table>")
        else:
            lines.append("<p>无来源文件</p>")

        lines.append("</div></body></html>")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))

    def _export_zip(self):
        if not self._last_sid:
            return
        path, _ = QFileDialog.getSaveFileName(self, "一键导出 zip",
                                              f"session{self._last_sid}.zip", "ZIP (*.zip)")
        if not path:
            return
        try:
            export_mod.export_session(self._last_sid, out_path=path)
            self.log(f"已导出 zip: {path}")
            QMessageBox.information(self, "导出成功", f"zip 已保存到:\n{path}")
        except Exception as e:  # noqa: BLE001
            self.log(f"zip 导出失败: {e}")
            QMessageBox.warning(self, "导出失败", str(e))
