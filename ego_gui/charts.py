"""matplotlib 嵌入 Qt 的图表构建（多子图布局）。

- 时间偏移时序图：以 PTP 硬件时间为基准，绘制「硬件 PTP ↔ EGOViewer 本地时间」
  与「硬件 PTP ↔ 视频帧 pts」两组差值曲线（毫秒），画红色阈值参考线，超阈值区间高亮。
- 丢点 / 丢帧柱状图：按时间切片统计 IMU 丢点、视频丢帧数量。

数据来自 frame_entries（left 目），均在 GUI 内嵌显示，不弹独立窗口。
"""

from __future__ import annotations

from typing import Dict, List, Optional

import matplotlib

matplotlib.use("QtAgg")
# 中文字体（Windows），避免图表中文变方块
matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
matplotlib.rcParams["axes.unicode_minus"] = False
from matplotlib.figure import Figure

from ego_relay import db


def load_left_frames(session_id: int) -> List[dict]:
    """读取 left 目帧序列（含各来源时间戳）。"""
    return [f for f in db.session_frames(session_id, side="left")
            if f.get("hw_ptp_us")]


def _segments_over(frames: List[dict], col: str, thr_ms: float) -> List[tuple]:
    """返回该列偏移(ms)连续超过阈值的 (start_idx, end_idx) 片段。"""
    segs: List[tuple] = []
    cur = None
    for i, f in enumerate(frames):
        v = f.get(col)
        over = v is not None and abs(v) > thr_ms
        if over:
            if cur is None:
                cur = [i, i]
            else:
                cur[1] = i
        else:
            if cur is not None:
                segs.append((cur[0], cur[1]))
                cur = None
    if cur is not None:
        segs.append((cur[0], cur[1]))
    return segs


def build_figure(frames: List[dict], stats: dict,
                 thr_ms: float = 50.0, slice_s: float = 5.0) -> Figure:
    """构建 2 子图 Figure：上=时间偏移曲线，下=丢点/丢帧柱状图。"""
    fig = Figure(figsize=(8, 6.5), dpi=100)
    # hspace 留足上下两个子图之间的垂直空间，避免上面子图的 x 轴标签
    # 与下面子图的标题重叠；bottom 留足下面子图 x 轴标签的显示空间。
    fig.subplots_adjust(hspace=0.85, left=0.09, right=0.97, top=0.90, bottom=0.17)
    ax_off = fig.add_subplot(2, 1, 1)
    ax_drop = fig.add_subplot(2, 1, 2)

    # ---------- 子图1：时间偏移曲线 ----------
    if frames:
        start = frames[0]["hw_ptp_us"]
        t = [(f["hw_ptp_us"] - start) / 1e6 for f in frames]
        app_off = [(f["app_recv_us"] - f["hw_ptp_us"]) / 1000.0
                   if f.get("app_recv_us") else None for f in frames]
        pts_off = [(f["sei_hw_ptp_us"] - f["hw_ptp_us"]) / 1000.0
                   if f.get("sei_hw_ptp_us") else None for f in frames]

        ax_off.plot(t, app_off, label="硬件PTP↔EGOViewer本地", lw=1.0, color="#3b82f6")
        ax_off.plot(t, pts_off, label="硬件PTP↔视频帧pts", lw=1.0, color="#f59e0b")

        # 阈值参考线 + 超阈值高亮
        for thr in (thr_ms, -thr_ms):
            ax_off.axhline(thr, color="red", lw=1.0, ls="--", alpha=0.7)
        for col, color in (("app_recv_us", "#bfdbfe"), ("sei_hw_ptp_us", "#fde68a")):
            off = [(f[col] - f["hw_ptp_us"]) / 1000.0 if f.get(col) else None
                   for f in frames]
            if not any(v is not None for v in off):
                continue
            segs = _segments_over(frames, col, thr_ms)
            for a, b in segs:
                ax_off.axvspan(t[a], t[b], color=color, alpha=0.4)

        ax_off.set_xlabel("时间 (s, 相对采集起点)")
        ax_off.set_ylabel("时间偏移 (ms)")
        ax_off.set_title(f"时间偏移时序图（阈值 ±{thr_ms:.0f} ms）")
        ax_off.legend(fontsize=8, loc="upper left")
        ax_off.grid(True, alpha=0.3)
    else:
        ax_off.text(0.5, 0.5, "无帧数据", ha="center", va="center")

    # ---------- 子图2：丢点/丢帧柱状图 ----------
    if frames:
        start = frames[0]["hw_ptp_us"]
        end = frames[-1]["hw_ptp_us"]
        bins = _make_bins(start, end, slice_s)
        video_drop, imu_drop = _drop_by_slice(frames, bins)

        xs = [(lo - start) / 1e6 for lo, hi in bins]
        width = max(0.4 * slice_s, 0.05)
        ax_drop.bar([x - width / 2 for x in xs], video_drop, width=width,
                    label="视频丢帧", color="#f87171")
        ax_drop.bar([x + width / 2 for x in xs], imu_drop, width=width,
                    label="IMU丢点", color="#60a5fa")
        ax_drop.set_xlabel("时间切片 (s)")
        ax_drop.set_ylabel("丢点/丢帧数")
        ax_drop.set_title(f"丢点/丢帧分布（切片 {slice_s:.0f}s）")
        ax_drop.legend(fontsize=8, loc="upper right")
        ax_drop.grid(True, alpha=0.3)
    else:
        ax_drop.text(0.5, 0.5, "无帧数据", ha="center", va="center")

    return fig


def _make_bins(start_us: int, end_us: int, slice_s: float):
    lo = start_us
    bins = []
    while lo < end_us:
        bins.append((lo, lo + int(slice_s * 1e6)))
        lo += int(slice_s * 1e6)
    return bins


def _drop_by_slice(frames: List[dict], bins: List[tuple]):
    """按时间切片估算视频丢帧与 IMU 丢点（基于帧间隔启发式）。"""
    video_drop = [0] * len(bins)
    imu_drop = [0] * len(bins)
    prev = None
    # 期望视频帧间隔（us）取相邻帧 hw_ptp 差的中位数
    gaps = [b["hw_ptp_us"] - a["hw_ptp_us"] for a, b in zip(frames, frames[1:])
            if b["hw_ptp_us"] > a["hw_ptp_us"]]
    gaps.sort()
    exp_frame = gaps[len(gaps) // 2] if gaps else 33_333
    for i, f in enumerate(frames):
        idx = _bin_index(f["hw_ptp_us"], bins)
        if idx is None:
            continue
        if prev is not None:
            prev_f = frames[prev]
            g = f["hw_ptp_us"] - prev_f["hw_ptp_us"]
            if 0 < g and g > exp_frame * 1.5:
                video_drop[idx] += max(0, int(round(g / exp_frame)) - 1)
            # IMU 丢点：相邻帧最近 IMU 时间差远大于期望 IMU 间隔则计丢
            if prev_f.get("imu_us") and f.get("imu_us"):
                gi = f["imu_us"] - prev_f["imu_us"]
                if 0 < gi and gi > 10_000:  # 期望 IMU ~5ms，>10ms 视为丢点
                    imu_drop[idx] += max(0, int(round(gi / 5_000)) - 1)
        prev = i
    return video_drop, imu_drop


def _bin_index(ts_us: int, bins: List[tuple]):
    for i, (lo, hi) in enumerate(bins):
        if lo <= ts_us < hi:
            return i
    return len(bins) - 1 if bins else None
