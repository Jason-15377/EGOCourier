#!/usr/bin/env python3
# ============================================================================
# video_analyzer.py - 综合视频分析工具 (跨平台版)
# ============================================================================
# 兼容: Windows 10+ / Ubuntu 20.04+ / macOS / 通用 Linux 发行版
#
# 功能:
#   1. SEI 时间戳提取 (两阶段: moov 结构化解析 + 原始 pattern 扫描, 结果合并)
#   2. SEI vs CSV 比对 -> 不一致则跳过后续处理
#   3. 自动检测帧率 & 帧数
#   4. 丢帧检测 (MP4 + MCAP + CSV PTS)
#   5. 左右 Color 时间戳同步检测
#   6. 不同步帧截取 + 随机 10 帧同步采样
#   7. 输出自包含 HTML 报告
#
# 用法:
#   python video_analyzer.py <根目录>
#   python video_analyzer.py /data/recordings --threshold 2.0
#   python video_analyzer.py . --output report.html --no-frame-dump
#
# 依赖:
#   系统: ffmpeg, ffprobe
#   Python: Python 3.8+
#   可选: numpy (加速数值计算), mcap, mcap-ros2-support (MCAP 文件支持)
# ============================================================================

import argparse
import bisect
import csv
import gc
import io
import json
import math
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# numpy 可选 — 不可用时回退到纯 Python 实现
# ---------------------------------------------------------------------------
try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    np = None  # type: ignore
    _HAS_NUMPY = False


# ============================================================================
# 常量
# ============================================================================
COMMON_RATES = [1, 10, 15, 24, 25, 30, 50, 60, 100, 120, 200, 250, 400, 500, 1000, 2000]
MAX_TIMESTAMP = 4102444800  # 2100-01-01


# ============================================================================
# 数据结构
# ============================================================================

@dataclass
class SeiCsvMismatch:
    """SEI 与 CSV 时间戳不匹配的一条记录"""
    index: int = 0
    table: str = ""          # "csv_not_in_sei" / "sei_not_in_csv"
    source_val: int = 0
    closest_val: int = 0
    closest_index: int = 0
    deviation_us: int = 0


@dataclass
class SeiCsvResult:
    """单个 MP4 文件的 SEI vs CSV 比对结果"""
    mp4_path: str = ""
    mp4_name: str = ""
    csv_path: str = ""
    codec: str = "unknown"
    extraction_tier: int = 0
    sei_count: int = 0
    csv_count: int = 0
    count_match: bool = True      # 数量是否一致
    match: bool = True            # 内容是否完全一致
    mismatches: List[SeiCsvMismatch] = field(default_factory=list)
    error: str = ""
    sei_timestamps: List[int] = field(default_factory=list)
    csv_timestamps: List[int] = field(default_factory=list)


@dataclass
class FrameGap:
    """丢帧记录"""
    prev_frame_index: int
    curr_frame_index: int
    prev_timestamp_us: int
    curr_timestamp_us: int
    prev_timestamp_s: float
    curr_timestamp_s: float
    gap_us: int
    expected_interval_us: int
    estimated_lost_frames: int


@dataclass
class StreamResult:
    """单路数据流的分析结果"""
    source_name: str
    source_type: str             # 'mp4_video' / 'mcap_video' / 'video_pts' / 'imu_accel' / ...
    total_frames: int
    expected_rate_hz: float
    expected_interval_us: float
    duration_s: float
    actual_rate_hz: float
    sei_extracted: int
    total_gaps: int
    total_lost_frames: int
    gaps: List[FrameGap] = field(default_factory=list)
    first_ts_us: int = 0
    last_ts_us: int = 0
    timestamps_us: List[int] = field(default_factory=list)
    codec: str = ""
    error: str = ""


@dataclass
class SyncUnpaired:
    """一对无法同步的时间戳"""
    left_ts_us: int = 0
    right_ts_us: int = 0
    left_ts_s: float = 0.0
    right_ts_s: float = 0.0
    diff_us: int = 0
    side: str = ""               # 'left_only' / 'right_only'
    left_idx: int = 0
    right_idx: int = 0
    closest_other_ts_us: int = 0
    closest_other_idx: int = 0


@dataclass
class SyncResult:
    """左右相机时间戳同步检测结果"""
    left_name: str = ""
    right_name: str = ""
    total_left: int = 0
    total_right: int = 0
    matched_pairs: int = 0
    matched_left_idx: List[int] = field(default_factory=list)    # 匹配的左帧序号 (0-based)
    matched_right_idx: List[int] = field(default_factory=list)   # 匹配的右帧序号 (0-based)
    unpaired: List[SyncUnpaired] = field(default_factory=list)
    avg_diff_us: float = 0.0
    max_diff_us: int = 0
    threshold_us: float = 0.0
    is_synced: bool = True
    left_timestamps: List[int] = field(default_factory=list)
    right_timestamps: List[int] = field(default_factory=list)


@dataclass
class ImuSyncOutlier:
    """IMU 与视频同步检测中的一条超标记录"""
    video_ts_us: int = 0
    imu_ts_us: int = 0
    offset_us: int = 0


@dataclass
class ImuSyncResult:
    """IMU 与视频时间戳同步检测结果"""
    imu_name: str = ""
    video_name: str = ""
    imu_total: int = 0
    video_total: int = 0
    avg_offset_us: float = 0.0
    max_offset_us: int = 0
    min_offset_us: int = 0
    threshold_us: float = 0.0
    is_synced: bool = True
    outliers: List[ImuSyncOutlier] = field(default_factory=list)


@dataclass
class FolderResult:
    """单个文件夹的分析结果"""
    folder_name: str
    folder_path: str
    sei_csv_results: List[SeiCsvResult] = field(default_factory=list)
    streams: List[StreamResult] = field(default_factory=list)
    sync_result: Optional[SyncResult] = None
    imu_sync_results: List[ImuSyncResult] = field(default_factory=list)
    count_mismatch_files: List[str] = field(default_factory=list)
    error: str = ""


# ============================================================================
# MP4 Box 解析器 (来自 sei_pts_checker.py)
# ============================================================================

class Mp4Parser:
    """纯 Python MP4/ISO BMFF box 解析器 — 基于文件 seek, 不全部加载到内存"""

    def __init__(self, filepath: str):
        self.filepath = filepath
        self._f = None

    def _open(self):
        if self._f is None:
            self._f = open(self.filepath, "rb")

    def close(self):
        if self._f:
            self._f.close()
            self._f = None

    def _read_at(self, offset: int, size: int) -> bytes:
        self._open()
        self._f.seek(offset)
        return self._f.read(size)

    def _find_box_in_slice(self, start: int, end: int, target_type: str) -> Tuple[Optional[int], Optional[int], Optional[int]]:
        pos = start
        while pos < end - 8:
            hdr_data = self._read_at(pos, 16)
            if len(hdr_data) < 8:
                break
            size = struct.unpack(">I", hdr_data[0:4])[0]
            btype = hdr_data[4:8].decode("ascii", errors="replace")
            hdr = 8
            if size == 1:
                if pos + 16 > end or len(hdr_data) < 16:
                    break
                size = struct.unpack(">Q", hdr_data[8:16])[0]
                hdr = 16
            if size == 0:
                size = end - pos
            if size < hdr or pos + size > end:
                break
            if btype == target_type:
                return pos, size, hdr
            pos += size
        return None, None, None

    def _read_box_data(self, box_start: int, box_size: int, box_hdr: int) -> bytes:
        return self._read_at(box_start + box_hdr, box_size - box_hdr)

    def parse_stbl(self, stbl_start: int, stbl_end: int) -> dict:
        result = {}
        # stts
        offset, size, hdr = self._find_box_in_slice(stbl_start, stbl_end, "stts")
        if offset is not None:
            buf = self._read_box_data(offset, size, hdr)
            if buf and len(buf) >= 8:
                off = 4
                count = struct.unpack(">I", buf[off:off + 4])[0]
                entries = []
                for i in range(count):
                    sc = struct.unpack(">I", buf[off + 4 + i * 8:off + 8 + i * 8])[0]
                    sd = struct.unpack(">I", buf[off + 8 + i * 8:off + 12 + i * 8])[0]
                    entries.append((sc, sd))
                result["stts"] = entries
        # stss
        offset, size, hdr = self._find_box_in_slice(stbl_start, stbl_end, "stss")
        if offset is not None:
            buf = self._read_box_data(offset, size, hdr)
            if buf and len(buf) >= 8:
                off = 4
                count = struct.unpack(">I", buf[off:off + 4])[0]
                sync = []
                for i in range(count):
                    s = struct.unpack(">I", buf[off + 4 + i * 4:off + 8 + i * 4])[0]
                    sync.append(s)
                result["stss"] = sync
        # stsz
        offset, size, hdr = self._find_box_in_slice(stbl_start, stbl_end, "stsz")
        if offset is not None:
            buf = self._read_box_data(offset, size, hdr)
            if buf and len(buf) >= 12:
                off = 4
                const_size = struct.unpack(">I", buf[off:off + 4])[0]
                sample_count = struct.unpack(">I", buf[off + 4:off + 8])[0]
                sizes = []
                if const_size == 0:
                    for i in range(sample_count):
                        s = struct.unpack(">I", buf[off + 8 + i * 4:off + 12 + i * 4])[0]
                        sizes.append(s)
                else:
                    sizes = [const_size] * sample_count
                result["stsz"] = {"const_size": const_size, "count": sample_count, "sizes": sizes}
        # stsc
        offset, size, hdr = self._find_box_in_slice(stbl_start, stbl_end, "stsc")
        if offset is not None:
            buf = self._read_box_data(offset, size, hdr)
            if buf and len(buf) >= 8:
                off = 4
                count = struct.unpack(">I", buf[off:off + 4])[0]
                entries = []
                for i in range(count):
                    fc = struct.unpack(">I", buf[off + 4 + i * 12:off + 8 + i * 12])[0]
                    spc = struct.unpack(">I", buf[off + 8 + i * 12:off + 12 + i * 12])[0]
                    sdi = struct.unpack(">I", buf[off + 12 + i * 12:off + 16 + i * 12])[0]
                    entries.append({"first_chunk": fc, "samples_per_chunk": spc, "sample_desc_index": sdi})
                result["stsc"] = entries
        # stco / co64
        offset, size, hdr = self._find_box_in_slice(stbl_start, stbl_end, "stco")
        if offset is not None:
            buf = self._read_box_data(offset, size, hdr)
            if buf and len(buf) >= 8:
                off = 4
                count = struct.unpack(">I", buf[off:off + 4])[0]
                offsets = []
                for i in range(count):
                    o = struct.unpack(">I", buf[off + 4 + i * 4:off + 8 + i * 4])[0]
                    offsets.append(o)
                result["stco"] = offsets
                result["stco_64bit"] = False
        else:
            offset, size, hdr = self._find_box_in_slice(stbl_start, stbl_end, "co64")
            if offset is not None:
                buf = self._read_box_data(offset, size, hdr)
                if buf and len(buf) >= 8:
                    off = 4
                    count = struct.unpack(">I", buf[off:off + 4])[0]
                    offsets = []
                    for i in range(count):
                        o = struct.unpack(">Q", buf[off + 4 + i * 8:off + 12 + i * 8])[0]
                        offsets.append(o)
                    result["stco"] = offsets
                    result["stco_64bit"] = True
        return result

    def build_sample_table(self, stbl: dict) -> List[Tuple[int, int]]:
        chunk_offsets = stbl.get("stco", [])
        stsc_entries = stbl.get("stsc", [])
        sample_sizes = stbl.get("stsz", {}).get("sizes", [])
        if not chunk_offsets or not stsc_entries:
            return []
        total_chunks = len(chunk_offsets)
        chunk_sample_counts = []
        for ci in range(1, total_chunks + 1):
            spc = 1
            for j, entry in enumerate(stsc_entries):
                fc = entry["first_chunk"]
                next_fc = stsc_entries[j + 1]["first_chunk"] if j + 1 < len(stsc_entries) else total_chunks + 1
                if fc <= ci < next_fc:
                    spc = entry["samples_per_chunk"]
                    break
            chunk_sample_counts.append(spc)
        samples = []
        sample_idx = 0
        for ci in range(total_chunks):
            offset = chunk_offsets[ci]
            n_samples = chunk_sample_counts[ci]
            for _ in range(n_samples):
                if sample_idx < len(sample_sizes):
                    sz = sample_sizes[sample_idx]
                else:
                    sz = 0
                samples.append((offset, sz))
                offset += sz
                sample_idx += 1
        return samples

    def parse(self) -> dict:
        self._open()
        file_size = os.path.getsize(self.filepath)
        head_size = min(65536, file_size)
        head = self._read_at(0, head_size)
        ftyp_str = ""
        moov_offset = None
        moov_size = None
        pos = 0
        while pos < len(head) - 8:
            size = struct.unpack(">I", head[pos:pos + 4])[0]
            btype = head[pos + 4:pos + 8].decode("ascii", errors="replace")
            hdr = 8
            if size == 1:
                if pos + 16 > len(head):
                    break
                size = struct.unpack(">Q", head[pos + 8:pos + 16])[0]
                hdr = 16
            if size == 0:
                break
            if size < hdr or pos + size > len(head):
                break
            if btype == "ftyp":
                ftyp_buf = head[pos + 8:pos + size]
                if len(ftyp_buf) >= 4:
                    ftyp_str = ftyp_buf[:4].decode("ascii", errors="replace")
            if btype == "moov":
                moov_offset = pos
                moov_size = size
                break
            pos += size
        if moov_offset is None:
            tail_size = min(1048576, file_size)
            tail_start = file_size - tail_size
            tail = self._read_at(tail_start, tail_size)
            pos = 0
            while pos < len(tail) - 8:
                size = struct.unpack(">I", tail[pos:pos + 4])[0]
                btype = tail[pos + 4:pos + 8].decode("ascii", errors="replace")
                hdr = 8
                if size == 1:
                    if pos + 16 > len(tail):
                        break
                    size = struct.unpack(">Q", tail[pos + 8:pos + 16])[0]
                    hdr = 16
                if size < hdr or pos + size > len(tail):
                    break
                if btype == "moov":
                    moov_offset = tail_start + pos
                    moov_size = size
                    break
                pos += size
        if moov_offset is None:
            raise ValueError("未找到 moov box")
        moov_data = self._read_at(moov_offset, moov_size)
        moov_end = moov_size
        video_trak_start = video_trak_end = None
        pos = 8
        while pos < moov_end - 8:
            tsize = struct.unpack(">I", moov_data[pos:pos + 4])[0]
            ttype = moov_data[pos + 4:pos + 8].decode("ascii", errors="replace")
            thdr = 8
            if tsize == 1:
                tsize = struct.unpack(">Q", moov_data[pos + 8:pos + 16])[0]
                thdr = 16
            if tsize < thdr or pos + tsize > moov_end:
                break
            if ttype == "trak":
                trak_end = pos + tsize
                mdp, mds, _ = self._find_box_in_data(moov_data, pos + 8, trak_end, "mdia")
                if mdp is not None:
                    mde = mdp + mds
                    hdlr_buf = self._get_box_data_in_buf(moov_data, mdp + 8, mde, "hdlr")
                    if hdlr_buf and len(hdlr_buf) >= 20:
                        handler_type = hdlr_buf[8:12].decode("ascii", errors="replace")
                        if handler_type == "vide":
                            video_trak_start = pos
                            video_trak_end = trak_end
                            break
            pos += tsize
        if video_trak_start is None:
            raise ValueError("未找到视频 trak")
        mdp, mds, _ = self._find_box_in_data(moov_data, video_trak_start + 8, video_trak_end, "mdia")
        mde = mdp + mds
        mfp, mfs, _ = self._find_box_in_data(moov_data, mdp + 8, mde, "minf")
        mfe = mfp + mfs
        stp, sts, _ = self._find_box_in_data(moov_data, mfp + 8, mfe, "stbl")
        ste = stp + sts
        stbl = self._parse_stbl_in_buf(moov_data, stp + 8, ste)
        samples = self.build_sample_table(stbl)
        nal_length_size = self._extract_nal_length_size(moov_data, stp + 8, ste)
        return {
            "ftyp": ftyp_str,
            "stbl": stbl,
            "samples": samples,
            "nal_length_size": nal_length_size,
        }

    @staticmethod
    def _extract_nal_length_size(moov_data: bytes, stbl_start: int, stbl_end: int) -> int:
        stsd_buf = Mp4Parser._get_box_data_in_buf(moov_data, stbl_start, stbl_end, "stsd")
        if not stsd_buf or len(stsd_buf) < 8:
            return 4
        se_start = 8
        if se_start + 8 > len(stsd_buf):
            return 4
        se_size = struct.unpack(">I", stsd_buf[se_start:se_start + 4])[0]
        se_data = stsd_buf[se_start:se_start + se_size]
        for config_type in [b"avcC", b"hvcC"]:
            cpos = se_data.find(config_type)
            if cpos >= 0 and cpos >= 4:
                csize = struct.unpack(">I", se_data[cpos - 4:cpos])[0]
                if csize < 8:
                    continue
                body = se_data[cpos + 8:cpos + csize]
                if config_type == b"avcC":
                    if len(body) >= 5:
                        return (body[4] & 0x03) + 1
                elif config_type == b"hvcC":
                    if len(body) >= 22:
                        return (body[21] & 0x03) + 1
        return 4

    @staticmethod
    def _find_box_in_data(buf: bytes, start: int, end: int, target: str) -> Tuple[Optional[int], Optional[int], Optional[int]]:
        pos = start
        while pos < end - 8:
            size = struct.unpack(">I", buf[pos:pos + 4])[0]
            btype = buf[pos + 4:pos + 8].decode("ascii", errors="replace")
            hdr = 8
            if size == 1:
                if pos + 16 > end:
                    break
                size = struct.unpack(">Q", buf[pos + 8:pos + 16])[0]
                hdr = 16
            if size == 0 or size < hdr or pos + size > end:
                break
            if btype == target:
                return pos, size, hdr
            pos += size
        return None, None, None

    @staticmethod
    def _get_box_data_in_buf(buf: bytes, start: int, end: int, target: str) -> Optional[bytes]:
        offset, size, hdr = Mp4Parser._find_box_in_data(buf, start, end, target)
        if offset is not None:
            return buf[offset + hdr:offset + size]
        return None

    @staticmethod
    def _parse_stbl_in_buf(moov_data: bytes, stbl_start: int, stbl_end: int) -> dict:
        result = {}
        def _read_fb(buf, off):
            return buf[off], struct.unpack(">I", buf[off:off + 4])[0] & 0x00FFFFFF, off + 4
        # stts
        buf = Mp4Parser._get_box_data_in_buf(moov_data, stbl_start, stbl_end, "stts")
        if buf and len(buf) >= 8:
            ver, flags, off = _read_fb(buf, 0)
            count = struct.unpack(">I", buf[off:off + 4])[0]
            entries = []
            for i in range(count):
                sc = struct.unpack(">I", buf[off + 4 + i * 8:off + 8 + i * 8])[0]
                sd = struct.unpack(">I", buf[off + 8 + i * 8:off + 12 + i * 8])[0]
                entries.append((sc, sd))
            result["stts"] = entries
        # stss
        buf = Mp4Parser._get_box_data_in_buf(moov_data, stbl_start, stbl_end, "stss")
        if buf and len(buf) >= 8:
            ver, flags, off = _read_fb(buf, 0)
            count = struct.unpack(">I", buf[off:off + 4])[0]
            sync = []
            for i in range(count):
                s = struct.unpack(">I", buf[off + 4 + i * 4:off + 8 + i * 4])[0]
                sync.append(s)
            result["stss"] = sync
        # stsz
        buf = Mp4Parser._get_box_data_in_buf(moov_data, stbl_start, stbl_end, "stsz")
        if buf and len(buf) >= 12:
            ver, flags, off = _read_fb(buf, 0)
            const_size = struct.unpack(">I", buf[off:off + 4])[0]
            sample_count = struct.unpack(">I", buf[off + 4:off + 8])[0]
            sizes = []
            if const_size == 0:
                for i in range(sample_count):
                    s = struct.unpack(">I", buf[off + 8 + i * 4:off + 12 + i * 4])[0]
                    sizes.append(s)
            else:
                sizes = [const_size] * sample_count
            result["stsz"] = {"const_size": const_size, "count": sample_count, "sizes": sizes}
        # stsc
        buf = Mp4Parser._get_box_data_in_buf(moov_data, stbl_start, stbl_end, "stsc")
        if buf and len(buf) >= 8:
            ver, flags, off = _read_fb(buf, 0)
            count = struct.unpack(">I", buf[off:off + 4])[0]
            entries = []
            for i in range(count):
                fc = struct.unpack(">I", buf[off + 4 + i * 12:off + 8 + i * 12])[0]
                spc = struct.unpack(">I", buf[off + 8 + i * 12:off + 12 + i * 12])[0]
                sdi = struct.unpack(">I", buf[off + 12 + i * 12:off + 16 + i * 12])[0]
                entries.append({"first_chunk": fc, "samples_per_chunk": spc, "sample_desc_index": sdi})
            result["stsc"] = entries
        # stco / co64
        buf = Mp4Parser._get_box_data_in_buf(moov_data, stbl_start, stbl_end, "stco")
        if buf and len(buf) >= 8:
            ver, flags, off = _read_fb(buf, 0)
            count = struct.unpack(">I", buf[off:off + 4])[0]
            offsets = []
            for i in range(count):
                o = struct.unpack(">I", buf[off + 4 + i * 4:off + 8 + i * 4])[0]
                offsets.append(o)
            result["stco"] = offsets
            result["stco_64bit"] = False
        else:
            buf = Mp4Parser._get_box_data_in_buf(moov_data, stbl_start, stbl_end, "co64")
            if buf and len(buf) >= 8:
                ver, flags, off = _read_fb(buf, 0)
                count = struct.unpack(">I", buf[off:off + 4])[0]
                offsets = []
                for i in range(count):
                    o = struct.unpack(">Q", buf[off + 4 + i * 8:off + 12 + i * 8])[0]
                    offsets.append(o)
                result["stco"] = offsets
                result["stco_64bit"] = True
        return result


# ============================================================================
# NAL 单元解析 & SEI 提取 (来自 sei_pts_checker.py)
# ============================================================================

def parse_length_prefixed_nals(data: bytes, nal_length_size: int = 4) -> List[dict]:
    nals = []
    pos = 0
    while pos + nal_length_size <= len(data):
        nal_size = 0
        for i in range(nal_length_size):
            nal_size = (nal_size << 8) | data[pos + i]
        pos += nal_length_size
        if nal_size == 0 or pos + nal_size > len(data):
            break
        nal_data = data[pos:pos + nal_size]
        pos += nal_size
        if len(nal_data) < 1:
            continue
        nal_header0 = nal_data[0]
        nal_type_h264 = nal_header0 & 0x1F
        nal_type_h265 = (nal_header0 >> 1) & 0x3F
        if len(nal_data) >= 2:
            payload = nal_data[2:]
        else:
            payload = nal_data[1:]
        nals.append({
            "type_h264": nal_type_h264,
            "type_h265": nal_type_h265,
            "size": nal_size,
            "payload": payload,
        })
    return nals


def parse_sei_payload(payload: bytes) -> List[dict]:
    messages = []
    pos = 0
    while pos < len(payload):
        if pos < len(payload) and payload[pos] == 0x80:
            break
        payload_type = 0
        while pos < len(payload) and payload[pos] == 0xFF:
            payload_type += 255
            pos += 1
        if pos >= len(payload):
            break
        payload_type += payload[pos]
        pos += 1
        payload_size = 0
        while pos < len(payload) and payload[pos] == 0xFF:
            payload_size += 255
            pos += 1
        if pos >= len(payload):
            break
        payload_size += payload[pos]
        pos += 1
        if pos + payload_size > len(payload):
            break
        sei_data = payload[pos:pos + payload_size]
        pos += payload_size
        msg = {"type": payload_type, "size": payload_size, "data": sei_data}
        if payload_type == 5 and len(sei_data) >= 16:
            msg["uuid"] = sei_data[:16]
            msg["user_data"] = sei_data[16:]
        messages.append(msg)
    return messages


def _extract_ts_from_sei_messages(messages: List[dict]) -> Optional[int]:
    for msg in messages:
        if msg.get("type") != 5:
            continue
        if "user_data" not in msg:
            continue
        user_data = msg["user_data"]
        try:
            ud_str = user_data.decode("ascii", errors="ignore")
            if "timestamp_us=" in ud_str:
                idx = ud_str.find("timestamp_us=")
                ts_str = ud_str[idx + len("timestamp_us="):]
                ts_str = "".join(c for c in ts_str if c.isdigit())
                if ts_str:
                    return int(ts_str)
        except (UnicodeDecodeError, ValueError):
            pass
    return None


def _is_sei_nal(type_h264: int, type_h265: int) -> bool:
    return type_h264 == 6 or type_h265 == 39 or type_h265 == 40


def _detect_codec(filepath: str) -> str:
    """综合检测 MP4 文件的编码类型 (H.264 / H.265)。"""
    file_size = os.path.getsize(filepath)
    with open(filepath, "rb") as f:
        head = f.read(min(131072, file_size))
        tail_size = min(2097152, file_size)
        tail = b""
        if tail_size > 0 and file_size > 131072:
            f.seek(max(0, file_size - tail_size))
            tail = f.read(tail_size)
    if b"hvc1" in head or b"hev1" in head:
        return "H.265"
    if b"avc1" in head:
        return "H.264"
    moov_start = head.find(b"moov")
    if moov_start > 0:
        moov_size = struct.unpack(">I", head[moov_start - 4:moov_start])[0]
        moov_data = head[moov_start:min(moov_start + moov_size, len(head))]
        if b"hvcC" in moov_data:
            return "H.265"
        if b"avcC" in moov_data:
            return "H.264"
    moov_tail = tail.find(b"moov")
    if moov_tail > 0:
        moov_tail_size = struct.unpack(">I", tail[moov_tail - 4:moov_tail])[0]
        moov_tail_data = tail[moov_tail:min(moov_tail + moov_tail_size, len(tail))]
        if b"hvcC" in moov_tail_data:
            return "H.265"
        if b"avcC" in moov_tail_data:
            return "H.264"
    if b"hvcC" in head or b"hvcC" in tail:
        return "H.265"
    if b"avcC" in head or b"avcC" in tail:
        return "H.264"
    for sc in [b"\x00\x00\x00\x01\x40", b"\x00\x00\x01\x40"]:
        idx = head.find(sc)
        if idx >= 0:
            nal_type_h265 = (head[idx + len(sc) - 1] >> 1) & 0x3F
            if nal_type_h265 == 32:
                return "H.265"
    for sc in [b"\x00\x00\x00\x01\x67", b"\x00\x00\x01\x67"]:
        if sc in head:
            return "H.264"
    return "unknown"


# ============================================================================
# SEI 提取 (三阶段, 来自 sei_pts_checker.py)
# ============================================================================

def extract_sei_tier1_moov(filepath: str, verbose: bool = False) -> Tuple[List[int], str, str]:
    parser = Mp4Parser(filepath)
    try:
        parsed = parser.parse()
        ftyp = parsed["ftyp"]
        if ftyp in ("hvc1", "hev1"):
            codec = "H.265"
        elif ftyp in ("avc1",):
            codec = "H.264"
        else:
            codec = _detect_codec(filepath)
        nal_length_size = parsed.get("nal_length_size", 4)
        samples = parsed["samples"]
        total = len(samples)
        timestamps = []
        for idx, (offset, size) in enumerate(samples):
            if size < 100:
                continue
            sample_data = parser._read_at(offset, min(size, 256 * 1024))
            if not sample_data:
                continue
            nals = parse_length_prefixed_nals(sample_data, nal_length_size=nal_length_size)
            for nal in nals:
                if _is_sei_nal(nal["type_h264"], nal["type_h265"]):
                    msgs = parse_sei_payload(nal["payload"])
                    ts = _extract_ts_from_sei_messages(msgs)
                    if ts is not None:
                        timestamps.append(ts)
                    break
        return timestamps, codec, f"AVCC/HVCC({nal_length_size}B)"
    finally:
        parser.close()


def extract_sei_tier2_raw_scan(filepath: str, verbose: bool = False) -> Tuple[List[int], str, str]:
    """Tier 2: 原始字节级模式匹配 - 搜索 'timestamp_us=' 二进制模式。"""
    file_size = os.path.getsize(filepath)
    chunk_size = 16 * 1024 * 1024
    overlap = 64
    timestamps = []
    pos = 0
    carry = b""
    pattern = b"timestamp_us="
    with open(filepath, "rb") as f:
        while pos < file_size:
            remaining = min(chunk_size, file_size - pos)
            if remaining <= 0:
                break
            chunk = f.read(remaining)
            if not chunk:
                break
            data = carry + chunk
            search_pos = 0
            while True:
                idx = data.find(pattern, search_pos)
                if idx == -1:
                    break
                num_start = idx + len(pattern)
                digits = []
                for i in range(num_start, min(num_start + 20, len(data))):
                    b = data[i]
                    if 0x30 <= b <= 0x39:
                        digits.append(chr(b))
                    else:
                        break
                if digits:
                    ts_val = int("".join(digits))
                    if ts_val > 1_500_000_000_000_000:
                        timestamps.append(ts_val)
                search_pos = idx + 1
            pos += len(chunk)
            carry = data[-overlap:] if len(data) > overlap else data
    codec = _detect_codec(filepath)
    return timestamps, codec, "raw_pattern"


def extract_sei_from_mp4(filepath: str, verbose: bool = False) -> Tuple[List[int], str, str, int, Optional[str]]:
    """
    两阶段 SEI 提取 — 两个阶段都执行, 结果取并集,
    避免 Tier 1 (moov) 部分成功时丢失 Tier 2 能提取到的时间戳。
    返回: (timestamps, codec, format_type, extraction_tier, error_message)
      tier: 3=两者合并, 2=仅Tier2, 1=仅Tier1, 0=均失败
    """
    t1_ts: List[int] = []
    t2_ts: List[int] = []
    codec = "unknown"
    fmt = "unknown"
    t1_ok = False
    t2_ok = False

    # Tier 1: moov 结构化解析
    try:
        t1_ts, codec, fmt = extract_sei_tier1_moov(filepath, verbose=verbose)
        if t1_ts:
            t1_ok = True
    except Exception:
        pass

    # Tier 2: 原始 pattern 扫描 (始终执行, 不因 Tier 1 成功而跳过)
    try:
        t2_ts, codec2, fmt2 = extract_sei_tier2_raw_scan(filepath, verbose=verbose)
        if not t1_ok:
            codec = codec2
            fmt = fmt2
        if t2_ts:
            t2_ok = True
    except Exception:
        pass

    # 合并去重
    all_ts = sorted(set(t1_ts) | set(t2_ts))

    if all_ts:
        if t1_ok and t2_ok:
            tier = 3  # 两者都有贡献
        elif t1_ok:
            tier = 1
        else:
            tier = 2
        return all_ts, codec, fmt, tier, None
    else:
        return [], codec, fmt, 0, "no moov and no SEI found in bitstream"


# ============================================================================
# CSV 读取 (来自 sei_pts_checker.py)
# ============================================================================

def read_pts_csv(filepath: str) -> List[int]:
    timestamps = []
    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is None:
            return []
        try:
            col_idx = header.index("timestamp_us")
        except ValueError:
            col_idx = 0
        for row in reader:
            if row and col_idx < len(row):
                try:
                    ts = int(row[col_idx].strip())
                    timestamps.append(ts)
                except ValueError:
                    continue
    return timestamps


# ============================================================================
# SEI vs CSV 比对 (来自 sei_pts_checker.py)
# ============================================================================

def _find_closest(sorted_list: List[int], target: int) -> Tuple[int, int]:
    """在有序列表中二分查找最接近 target 的值。返回: (closest_value, closest_index)"""
    idx = bisect.bisect_left(sorted_list, target)
    if idx == 0:
        return sorted_list[0], 0
    if idx >= len(sorted_list):
        return sorted_list[-1], len(sorted_list) - 1
    left_val = sorted_list[idx - 1]
    right_val = sorted_list[idx]
    if abs(left_val - target) <= abs(right_val - target):
        return left_val, idx - 1
    else:
        return right_val, idx


def compare_sei_csv(sei_ts: List[int], csv_ts: List[int]) -> dict:
    """比对 SEI 和 CSV 时间戳。"""
    sei_count = len(sei_ts)
    csv_count = len(csv_ts)
    sei_set = set(sei_ts)
    csv_set = set(csv_ts)
    # CSV 中有但 SEI 中没有的
    csv_not_in_sei = []
    for idx, val in enumerate(csv_ts):
        if val not in sei_set:
            closest_val, closest_idx = _find_closest(sei_ts, val)
            csv_not_in_sei.append(SeiCsvMismatch(
                table="csv_not_in_sei", index=idx,
                source_val=val, closest_val=closest_val,
                closest_index=closest_idx, deviation_us=closest_val - val,
            ))
    # SEI 中有但 CSV 中没有的
    sei_not_in_csv = []
    for idx, val in enumerate(sei_ts):
        if val not in csv_set:
            closest_val, closest_idx = _find_closest(csv_ts, val)
            sei_not_in_csv.append(SeiCsvMismatch(
                table="sei_not_in_csv", index=idx,
                source_val=val, closest_val=closest_val,
                closest_index=closest_idx, deviation_us=closest_val - val,
            ))
    total_mismatch = len(csv_not_in_sei) + len(sei_not_in_csv)
    match = total_mismatch == 0
    return {
        "match": match,
        "sei_count": sei_count,
        "csv_count": csv_count,
        "count_match": sei_count == csv_count,
        "csv_not_in_sei": csv_not_in_sei,
        "sei_not_in_csv": sei_not_in_csv,
    }


# ============================================================================
# 丢帧检测 (来自 batch_frame_checker.py)
# ============================================================================

def _clean_timestamps(ts_list: List[int]) -> List[int]:
    """清理异常时间戳: 在异常大间隔处分段, 保留最大连续段。"""
    if len(ts_list) < 10:
        return ts_list
    ts_sorted = sorted(ts_list)
    if _HAS_NUMPY:
        ts_arr = np.asarray(ts_sorted[:200], dtype=np.int64)
        sample_deltas = np.diff(ts_arr)
        sample_deltas = sample_deltas[sample_deltas > 0]
        median_iv = float(np.median(sample_deltas)) if len(sample_deltas) > 0 else 16666
    else:
        sample = ts_sorted[:200]
        deltas = []
        for i in range(1, len(sample)):
            if sample[i] > sample[i - 1]:
                deltas.append(sample[i] - sample[i - 1])
        if deltas:
            deltas.sort()
            median_iv = float(deltas[len(deltas) // 2])
        else:
            median_iv = 16666.0
    max_gap = max(10_000_000, median_iv * 100)
    segments, start = [], 0
    for i in range(len(ts_sorted) - 1):
        if ts_sorted[i + 1] - ts_sorted[i] > max_gap:
            segments.append(ts_sorted[start:i + 1])
            start = i + 1
    segments.append(ts_sorted[start:])
    return max(segments, key=len)


def auto_detect_rate(timestamps_us: List[int]) -> float:
    """从时间戳中位数间隔自动推算采样率 (Hz)。"""
    n = len(timestamps_us)
    if n < 10:
        return 30.0
    if _HAS_NUMPY:
        ts = np.asarray(timestamps_us[:200], dtype=np.int64)
        deltas = np.diff(ts)
        deltas = deltas[deltas > 0]
        if len(deltas) == 0:
            return 30.0
        median_interval = float(np.median(deltas))
    else:
        sample = sorted(timestamps_us[:200])
        deltas = []
        for i in range(1, len(sample)):
            if sample[i] > sample[i - 1]:
                deltas.append(sample[i] - sample[i - 1])
        if not deltas:
            return 30.0
        deltas.sort()
        median_interval = float(deltas[len(deltas) // 2])
    if median_interval <= 0:
        return 30.0
    rate = 1_000_000.0 / median_interval
    return min(COMMON_RATES, key=lambda r: abs(r - rate))


def detect_gaps(timestamps_us: List[int], expected_hz: float, threshold_multiplier: float = 1.5) -> List[FrameGap]:
    """丢帧检测 (numpy 加速, 无 numpy 时回退纯 Python)。"""
    n = len(timestamps_us)
    if n < 2:
        return []
    expected_interval_us = 1_000_000.0 / expected_hz
    threshold_us = expected_interval_us * threshold_multiplier
    max_lost = n  # 丢失帧数封顶为总帧数
    gaps = []
    if _HAS_NUMPY:
        ts = np.asarray(timestamps_us, dtype=np.int64)
        deltas = np.diff(ts)
        gap_idx = np.where(deltas > threshold_us)[0]
        for i in gap_idx:
            i = int(i)
            delta_us = int(deltas[i])
            estimated_lost = min(max(1, round(delta_us / expected_interval_us) - 1), max_lost)
            gaps.append(FrameGap(
                prev_frame_index=i + 1,
                curr_frame_index=i + 2,
                prev_timestamp_us=int(ts[i]),
                curr_timestamp_us=int(ts[i + 1]),
                prev_timestamp_s=float(ts[i]) / 1e6,
                curr_timestamp_s=float(ts[i + 1]) / 1e6,
                gap_us=delta_us,
                expected_interval_us=int(expected_interval_us),
                estimated_lost_frames=estimated_lost,
            ))
    else:
        for i in range(n - 1):
            delta_us = timestamps_us[i + 1] - timestamps_us[i]
            if delta_us > threshold_us:
                estimated_lost = min(max(1, round(delta_us / expected_interval_us) - 1), max_lost)
                gaps.append(FrameGap(
                    prev_frame_index=i + 1,
                    curr_frame_index=i + 2,
                    prev_timestamp_us=timestamps_us[i],
                    curr_timestamp_us=timestamps_us[i + 1],
                    prev_timestamp_s=timestamps_us[i] / 1e6,
                    curr_timestamp_s=timestamps_us[i + 1] / 1e6,
                    gap_us=delta_us,
                    expected_interval_us=int(expected_interval_us),
                    estimated_lost_frames=estimated_lost,
                ))
    return gaps


def normalize_timestamps(ts_list: List[int]) -> List[int]:
    """如果中位数 > 10^16, 除以 1000 (纳秒→微秒转换)。"""
    if not ts_list:
        return ts_list
    if _HAS_NUMPY:
        median = int(np.median(np.asarray(ts_list, dtype=np.int64)))
    else:
        sorted_ts = sorted(ts_list)
        median = sorted_ts[len(sorted_ts) // 2]
    if median > 10_000_000_000_000_000:
        return [t // 1000 for t in ts_list]
    return ts_list


# ============================================================================
# MCAP 解析 (来自 batch_frame_checker.py)
# ============================================================================

# 预编译 IMU 时间戳正则 (C 引擎, 比逐字节扫描快)
_IMU_TS_RE = re.compile(b'"timestamp_us":(\\d+)')


def analyze_mcap(filepath: str, threshold_multiplier: float) -> List[StreamResult]:
    try:
        from mcap.reader import make_reader
    except ImportError:
        return [StreamResult(source_name=Path(filepath).name, source_type="mcap",
                             total_frames=0, expected_rate_hz=0, expected_interval_us=0,
                             duration_s=0, actual_rate_hz=0, sei_extracted=0,
                             total_gaps=0, total_lost_frames=0, error="缺少 mcap 库: pip install mcap")]
    path = Path(filepath)
    results = []

    def parse_compressed_video(data: bytes) -> dict:
        result = {"h264_data": b"", "format": ""}
        pos = 0
        while pos < len(data):
            tag = data[pos]; pos += 1
            fn = tag >> 3; wt = tag & 0x07
            if wt == 0:
                while pos < len(data) and (data[pos] & 0x80): pos += 1
                pos += 1
            elif wt == 2:
                L = 0; s = 0
                while True:
                    b = data[pos]; pos += 1; L |= (b & 0x7f) << s; s += 7
                    if not (b & 0x80): break
                chunk = data[pos:pos+L]; pos += L
                if fn == 3: result["h264_data"] = chunk
                elif fn == 4: result["format"] = chunk.decode("utf-8", errors="replace")
        return result

    def extract_sei_mcap(h264_data: bytes, codec: str = "h264") -> Optional[int]:
        """参考 mcap_sei_extractor.py: 直接用 pattern 搜索提取 SEI 时间戳, 不依赖 NAL 解析。"""
        pattern = b"timestamp_us="
        idx = h264_data.find(pattern)
        if idx == -1:
            return None
        s = idx + len(pattern)
        e = s
        while e < len(h264_data) and 48 <= h264_data[e] <= 57:
            e += 1
        if e > s:
            return int(h264_data[s:e])
        return None

    try:
        file_size_mb = os.path.getsize(filepath) / (1024 * 1024)
        print(f"    MCAP 文件: {file_size_mb:.0f} MB, 打开中...", flush=True)
        with open(filepath, "rb") as f:
            reader = make_reader(f)
            summary = reader.get_summary()
            # ---- 统一 topic 分类 & channel ID 映射 ----
            video_topics = set()
            imu_accel_topics = []
            imu_gyro_topics = []
            video_ts: Dict[str, list] = defaultdict(list)
            imu_accel_ts: List[int] = []
            imu_gyro_ts: List[int] = []
            mcap_codec = "h264"
            codec_scan_frames = 0

            # 支持多种常见的消息编码格式
            _supported_encodings = {"protobuf", "ros2", "cdr", "ros2msg", "json", ""}
            for cid, ch in summary.channels.items():
                enc = (getattr(ch, 'message_encoding', '') or '').lower()
                topic = getattr(ch, 'topic', '')
                if enc not in _supported_encodings and enc != '':
                    continue
                tl = topic.lower()
                if any(kw in tl for kw in ("camera", "video", "color", "rgb", "image")):
                    video_topics.add(topic)
                elif "accel" in tl:
                    imu_accel_topics.append(topic)
                elif "gyro" in tl:
                    imu_gyro_topics.append(topic)

            # 没匹配到视频 topic 时的 fallback: 取所有非 IMU channel
            if not video_topics:
                for cid, ch in summary.channels.items():
                    tl = ch.topic.lower()
                    if "accel" not in tl and "gyro" not in tl:
                        video_topics.add(ch.topic)
                print(f"    未匹配到视频关键词, fallback: {len(video_topics)} 个 channel 当作视频源", flush=True)

            # ---- 构建 channel ID → 类型 快速查找表 (O(1) 整型比对) ----
            video_ch_ids: set = set()
            accel_ch_id = None
            gyro_ch_id = None
            for cid, ch in summary.channels.items():
                if ch.topic in video_topics:
                    video_ch_ids.add(cid)
                if ch.topic in imu_accel_topics:
                    accel_ch_id = cid
                if ch.topic in imu_gyro_topics:
                    gyro_ch_id = cid

            # ---- 合并所有需要遍历的 topic, 预估消息总数 ----
            all_relevant_topics = list(video_topics) + imu_accel_topics + imu_gyro_topics
            total_expected = 0
            for cid, ch in summary.channels.items():
                topic = getattr(ch, 'topic', '')
                if topic in video_topics or topic in imu_accel_topics or topic in imu_gyro_topics:
                    total_expected += getattr(ch, 'message_count', 0)

            if not all_relevant_topics:
                print(f"    无相关 topic 可遍历, 跳过 MCAP", flush=True)
                return results

            print(f"    MCAP: {len(summary.channels)} channels, "
                  f"视频={len(video_topics)} IMU={len(imu_accel_topics)+len(imu_gyro_topics)}, "
                  f"预估 {total_expected:,} 条消息", flush=True)

            # ---- 单次遍历: 视频 SEI + IMU 时间戳 同时提取 (含实时进度) ----
            print(f"    单次遍历提取中...", flush=True)
            msg_count = 0
            last_report = time.time()
            report_interval = 5  # 每 5 秒报告一次进度

            for schema, channel, msg in reader.iter_messages(topics=all_relevant_topics):
                msg_count += 1
                ch_id = msg.channel_id

                # ---- 定时进度报告 (避免 print 过多影响性能) ----
                now = time.time()
                if now - last_report >= report_interval:
                    video_frames = sum(len(v) for v in video_ts.values())
                    if total_expected > 0:
                        pct = msg_count / total_expected * 100
                        print(f"      进度 {msg_count:,} / ~{total_expected:,} 条 "
                              f"({pct:.1f}%), "
                              f"视频帧={video_frames}, "
                              f"IMU={len(imu_accel_ts)+len(imu_gyro_ts)}", flush=True)
                    else:
                        print(f"      已处理 {msg_count:,} 条, "
                              f"视频帧={video_frames}, "
                              f"IMU={len(imu_accel_ts)+len(imu_gyro_ts)}", flush=True)
                    last_report = now

                # ---- 视频 SEI 提取 ----
                if ch_id in video_ch_ids:
                    topic = channel.topic
                    parsed = parse_compressed_video(msg.data)
                    h264 = parsed.get("h264_data", b"")
                    fmt = parsed.get("format", "h264")
                    if codec_scan_frames < 10 and h264:
                        codec_scan_frames += 1
                        if "h265" in fmt.lower() or "hevc" in fmt.lower():
                            mcap_codec = "h265"; codec_scan_frames = 999
                        else:
                            i = 0
                            while i < len(h264) - 3:
                                if h264[i:i+4] == b"\x00\x00\x00\x01": sc = 4
                                elif h264[i:i+3] == b"\x00\x00\x01": sc = 3
                                else: i += 1; continue
                                hp = i + sc
                                if hp < len(h264):
                                    h264_type = h264[hp] & 0x1F
                                    if h264_type == 0: mcap_codec = "h265"; codec_scan_frames = 999; break
                                i = hp + 1
                    sei_ts = extract_sei_mcap(h264, mcap_codec) if h264 else None
                    if sei_ts:
                        video_ts[topic].append(sei_ts)

                # ---- IMU 时间戳提取 ----
                elif ch_id == accel_ch_id:
                    m = _IMU_TS_RE.search(msg.data)
                    if m:
                        imu_accel_ts.append(int(m.group(1)))
                elif ch_id == gyro_ch_id:
                    m = _IMU_TS_RE.search(msg.data)
                    if m:
                        imu_gyro_ts.append(int(m.group(1)))

            # ---- 提取完成统计 ----
            total_video_frames = sum(len(v) for v in video_ts.values())
            print(f"    遍历完成: {msg_count:,} 条 → "
                  f"视频帧={total_video_frames}, "
                  f"IMU accel={len(imu_accel_ts)} gyro={len(imu_gyro_ts)}", flush=True)
            for t in sorted(video_topics):
                n = len(video_ts.get(t, []))
                if n > 0:
                    print(f"      {t}: {n} 帧 SEI", flush=True)
            for topic, ts_list in video_ts.items():
                ts_list = _clean_timestamps(ts_list)
                detected_rate = auto_detect_rate(ts_list) if ts_list else 30.0
                gaps = detect_gaps(ts_list, detected_rate, threshold_multiplier)
                dur = (ts_list[-1] - ts_list[0]) / 1e6 if len(ts_list) >= 2 else 0
                actual = (len(ts_list) - 1) / dur if dur > 0 else 0
                tname = topic.split("/")[-1] if "/" in topic else topic
                results.append(StreamResult(
                    source_name=f"{path.name}/{tname}",
                    source_type="mcap_video",
                    total_frames=len(ts_list),
                    expected_rate_hz=detected_rate,
                    expected_interval_us=1e6 / max(1, detected_rate),
                    duration_s=dur,
                    actual_rate_hz=actual,
                    sei_extracted=len(ts_list),
                    total_gaps=len(gaps),
                    total_lost_frames=min(sum(g.estimated_lost_frames for g in gaps), len(ts_list)),
                    gaps=gaps,
                    first_ts_us=ts_list[0] if ts_list else 0,
                    last_ts_us=ts_list[-1] if ts_list else 0,
                    timestamps_us=list(ts_list),
                    codec=mcap_codec,
                ))
            if imu_accel_ts:
                imu_accel_ts.sort()
                imu_hz = 1000.0
                gaps = detect_gaps(imu_accel_ts, imu_hz, threshold_multiplier)
                dur = (imu_accel_ts[-1] - imu_accel_ts[0]) / 1e6 if len(imu_accel_ts) >= 2 else 0
                actual = (len(imu_accel_ts) - 1) / dur if dur > 0 else 0
                results.append(StreamResult(
                    source_name=f"{path.name}/imu_accel", source_type="imu_accel",
                    total_frames=len(imu_accel_ts), expected_rate_hz=imu_hz,
                    expected_interval_us=1e6 / imu_hz, duration_s=dur, actual_rate_hz=actual,
                    sei_extracted=0, total_gaps=len(gaps),
                    total_lost_frames=min(sum(g.estimated_lost_frames for g in gaps), len(imu_accel_ts)),
                    gaps=gaps, first_ts_us=imu_accel_ts[0] if imu_accel_ts else 0,
                    last_ts_us=imu_accel_ts[-1] if imu_accel_ts else 0,
                    timestamps_us=list(imu_accel_ts),
                ))
            if imu_gyro_ts:
                imu_gyro_ts.sort()
                imu_hz = 1000.0
                gaps = detect_gaps(imu_gyro_ts, imu_hz, threshold_multiplier)
                dur = (imu_gyro_ts[-1] - imu_gyro_ts[0]) / 1e6 if len(imu_gyro_ts) >= 2 else 0
                actual = (len(imu_gyro_ts) - 1) / dur if dur > 0 else 0
                results.append(StreamResult(
                    source_name=f"{path.name}/imu_gyro", source_type="imu_gyro",
                    total_frames=len(imu_gyro_ts), expected_rate_hz=imu_hz,
                    expected_interval_us=1e6 / imu_hz, duration_s=dur, actual_rate_hz=actual,
                    sei_extracted=0, total_gaps=len(gaps),
                    total_lost_frames=min(sum(g.estimated_lost_frames for g in gaps), len(imu_gyro_ts)),
                    gaps=gaps, first_ts_us=imu_gyro_ts[0] if imu_gyro_ts else 0,
                    last_ts_us=imu_gyro_ts[-1] if imu_gyro_ts else 0,
                    timestamps_us=list(imu_gyro_ts),
                ))
    except Exception as e:
        import traceback
        print(f"    [ERROR] MCAP 解析失败: {e}", flush=True)
        if "--verbose" in sys.argv or "-v" in sys.argv:
            traceback.print_exc()
        results.append(StreamResult(
            source_name=path.name, source_type="mcap",
            total_frames=0, expected_rate_hz=0, expected_interval_us=0,
            duration_s=0, actual_rate_hz=0, sei_extracted=0,
            total_gaps=0, total_lost_frames=0, error=str(e),
        ))
    return results


# ============================================================================
# MP4 丢帧分析 (使用 ffmpeg + pattern 扫描, 来自 batch_frame_checker.py)
# ============================================================================

def analyze_mp4_drops(filepath: str, threshold_multiplier: float) -> List[StreamResult]:
    """用 ffmpeg + 流式扫描提取 SEI 时间戳并检测丢帧 (不将 AnnexB 全部加载到内存)。"""
    path = Path(filepath)
    # 检测编码
    codec = "h264"
    path_lower = str(path).lower()
    if "h265" in path_lower or "hevc" in path_lower:
        codec = "h265"
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", os.path.abspath(filepath)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        codec_name = probe.stdout.decode().strip()
        if "hevc" in codec_name: codec = "h265"
        elif "h264" in codec_name: codec = "h264"
    except Exception:
        pass
    bsf = "hevc_mp4toannexb" if codec == "h265" else "h264_mp4toannexb"
    fmt = "hevc" if codec == "h265" else "h264"

    # ---- 流式读取 ffmpeg stdout, 分块扫描 timestamp_us= ----
    ts_list: List[int] = []
    pattern = b"timestamp_us="
    pattern_len = len(pattern)
    chunk_size = 64 * 1024 * 1024  # 64MB 块
    overlap = pattern_len + 20 + 64  # 跨块残留
    sei_count = 0
    ffmpeg_error = ""

    try:
        proc = subprocess.Popen(
            ["ffmpeg", "-i", os.path.abspath(filepath), "-an", "-vcodec", "copy",
             "-bsf:v", bsf, "-f", fmt, "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        return [StreamResult(source_name=path.name, source_type="mp4_video",
                total_frames=0, expected_rate_hz=0, expected_interval_us=0,
                duration_s=0, actual_rate_hz=0, sei_extracted=0,
                total_gaps=0, total_lost_frames=0,
                error="ffmpeg 未安装")]

    carry = b""
    try:
        while True:
            chunk = proc.stdout.read(chunk_size)
            if not chunk:
                break
            data = carry + chunk
            pos = 0
            while True:
                idx = data.find(pattern, pos)
                if idx == -1:
                    break
                s = idx + pattern_len
                e = s
                while e < len(data) and 48 <= data[e] <= 57:
                    e += 1
                if e > s:
                    val = int(data[s:e])
                    ts_list.append(val)
                    sei_count += 1
                pos = e
            carry = data[-overlap:] if len(data) > overlap else data

        proc.wait(timeout=30)
        if proc.returncode != 0:
            ffmpeg_error = f"ffmpeg 返回码 {proc.returncode}"
    except Exception:
        try:
            proc.kill()
            proc.wait()
        except Exception:
            pass
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
    if ffmpeg_error:
        return [StreamResult(source_name=path.name, source_type="mp4_video",
                total_frames=0, expected_rate_hz=0, expected_interval_us=0,
                duration_s=0, actual_rate_hz=0, sei_extracted=0,
                total_gaps=0, total_lost_frames=0,
                error=f"ffmpeg 提取失败: {ffmpeg_error}")]

    ts_list = _clean_timestamps(ts_list)
    if not ts_list:
        return [StreamResult(source_name=path.name, source_type="mp4_video",
                total_frames=0, expected_rate_hz=0, expected_interval_us=0,
                duration_s=0, actual_rate_hz=0, sei_extracted=0,
                total_gaps=0, total_lost_frames=0,
                error=f"未找到 SEI (扫描到 {sei_count} 条, 清理后剩余 {len(ts_list)} 条)")]
    detected_rate = auto_detect_rate(ts_list) if len(ts_list) >= 10 else 30.0
    gaps = detect_gaps(ts_list, detected_rate, threshold_multiplier)
    dur = (ts_list[-1] - ts_list[0]) / 1e6 if len(ts_list) >= 2 else 0
    actual = (len(ts_list) - 1) / dur if dur > 0 else 0
    return [StreamResult(
        source_name=path.name, source_type="mp4_video",
        total_frames=len(ts_list), expected_rate_hz=detected_rate,
        expected_interval_us=1e6 / max(1, detected_rate),
        duration_s=dur, actual_rate_hz=actual, sei_extracted=len(ts_list),
        total_gaps=len(gaps),
        total_lost_frames=min(sum(g.estimated_lost_frames for g in gaps), len(ts_list)),
        gaps=gaps, first_ts_us=ts_list[0] if ts_list else 0,
        last_ts_us=ts_list[-1] if ts_list else 0,
        timestamps_us=list(ts_list), codec=codec,
    )]


# ============================================================================
# 左右相机同步检测 (来自 batch_frame_checker.py)
# ============================================================================

def check_stereo_sync(streams: List[StreamResult]) -> Optional[SyncResult]:
    left_streams = []
    right_streams = []
    for s in streams:
        if s.source_type not in ("mcap_video", "mp4_video", "video_pts"):
            continue
        if not s.timestamps_us:
            continue
        name_lower = s.source_name.lower()
        if any(kw in name_lower for kw in ("left", "camera_left", "/left", "_l_")):
            left_streams.append(s)
        elif any(kw in name_lower for kw in ("right", "camera_right", "/right", "_r_")):
            right_streams.append(s)
    if not left_streams or not right_streams:
        return None
    sync_results = []
    for left_s in left_streams:
        matched_right = None
        for right_s in right_streams:
            if right_s.source_type == left_s.source_type:
                matched_right = right_s
                break
        if matched_right is None:
            matched_right = right_streams[0]
        left_ts = left_s.timestamps_us
        right_ts = matched_right.timestamps_us
        interval_us = left_s.expected_interval_us
        threshold_us = interval_us / 2.0
        matched_pairs = 0
        matched_l_idx = []
        matched_r_idx = []
        unpaired = []
        diffs = []
        li = 0; ri = 0
        while li < len(left_ts) and ri < len(right_ts):
            lts = left_ts[li]; rts = right_ts[ri]
            diff = abs(lts - rts)
            if diff <= threshold_us:
                matched_pairs += 1; diffs.append(diff)
                matched_l_idx.append(li)
                matched_r_idx.append(ri)
                li += 1; ri += 1
            elif lts < rts:
                ri2 = bisect.bisect_left(right_ts, lts)
                closest_ts = 0; closest_idx = 0
                for i in (ri2-1, ri2):
                    if 0 <= i < len(right_ts):
                        c = right_ts[i]
                        if abs(c - lts) < abs((closest_ts or float('inf')) - lts):
                            closest_ts = c; closest_idx = i + 1
                unpaired.append(SyncUnpaired(
                    left_ts_us=lts, side="left_only", left_idx=li+1,
                    left_ts_s=lts/1e6, closest_other_ts_us=closest_ts, closest_other_idx=closest_idx,
                )); li += 1
            else:
                li2 = bisect.bisect_left(left_ts, rts)
                closest_ts = 0; closest_idx = 0
                for i in (li2-1, li2):
                    if 0 <= i < len(left_ts):
                        c = left_ts[i]
                        if abs(c - rts) < abs((closest_ts or float('inf')) - rts):
                            closest_ts = c; closest_idx = i + 1
                unpaired.append(SyncUnpaired(
                    right_ts_us=rts, right_ts_s=rts/1e6, side="right_only", right_idx=ri+1,
                    closest_other_ts_us=closest_ts, closest_other_idx=closest_idx,
                )); ri += 1
        while li < len(left_ts):
            lts = left_ts[li]; ri2 = bisect.bisect_left(right_ts, lts)
            cts=0; cidx=0
            for i in (ri2-1, ri2):
                if 0 <= i < len(right_ts):
                    c = right_ts[i]
                    if abs(c-lts) < abs((cts or float('inf'))-lts): cts=c; cidx=i+1
            unpaired.append(SyncUnpaired(
                left_ts_us=lts, left_ts_s=lts/1e6, side="left_only", left_idx=li+1,
                closest_other_ts_us=cts, closest_other_idx=cidx,
            )); li += 1
        while ri < len(right_ts):
            rts = right_ts[ri]; li2 = bisect.bisect_left(left_ts, rts)
            cts=0; cidx=0
            for i in (li2-1, li2):
                if 0 <= i < len(left_ts):
                    c = left_ts[i]
                    if abs(c-rts) < abs((cts or float('inf'))-rts): cts=c; cidx=i+1
            unpaired.append(SyncUnpaired(
                right_ts_us=rts, right_ts_s=rts/1e6, side="right_only", right_idx=ri+1,
                closest_other_ts_us=cts, closest_other_idx=cidx,
            )); ri += 1
        avg_diff = sum(diffs) / len(diffs) if diffs else 0.0
        max_diff = max(diffs) if diffs else 0
        sync_results.append(SyncResult(
            left_name=left_s.source_name, right_name=matched_right.source_name,
            total_left=len(left_ts), total_right=len(right_ts),
            matched_pairs=matched_pairs,
            matched_left_idx=list(matched_l_idx),
            matched_right_idx=list(matched_r_idx),
            unpaired=unpaired,
            avg_diff_us=avg_diff, max_diff_us=max_diff,
            threshold_us=threshold_us,
            is_synced=len(unpaired) == 0 and matched_pairs > 0,
            left_timestamps=list(left_ts), right_timestamps=list(right_ts),
        ))
        break
    return sync_results[0] if sync_results else None


def _calc_imu_video_offset(imu_ts_list: List[int], vid_ts_list: List[int],
                           threshold_us: float) -> Optional[dict]:
    """计算 IMU 与单个视频流的同步偏移, 并收集超标采样点。

    Args:
        imu_ts_list: IMU 时间戳列表 (已排序)
        vid_ts_list: 视频时间戳列表 (已排序)
        threshold_us: 偏移阈值 (超过此值视为超标)
    Returns:
        dict with avg_off, max_off, min_off, outliers — 或 None
    """
    offsets = []
    outliers = []
    step = max(1, len(vid_ts_list) // 1000)
    for vi in range(0, len(vid_ts_list), step):
        vt = vid_ts_list[vi]
        idx = bisect.bisect_left(imu_ts_list, vt)
        closest = imu_ts_list[min(idx, len(imu_ts_list) - 1)] if idx < len(imu_ts_list) else imu_ts_list[-1]
        if idx > 0 and abs(imu_ts_list[idx - 1] - vt) < abs(closest - vt):
            closest = imu_ts_list[idx - 1]
        off = abs(closest - vt)
        offsets.append(off)
        if off > threshold_us:
            outliers.append(ImuSyncOutlier(
                video_ts_us=vt, imu_ts_us=closest, offset_us=off,
            ))

    if not offsets:
        return None

    if _HAS_NUMPY:
        offsets_arr = np.asarray(offsets, dtype=np.int64)
        return {
            "avg_off": float(np.mean(offsets_arr)),
            "max_off": int(np.max(offsets_arr)),
            "min_off": int(np.min(offsets_arr)),
            "outliers": outliers,
        }
    else:
        return {
            "avg_off": sum(offsets) / len(offsets),
            "max_off": max(offsets),
            "min_off": min(offsets),
            "outliers": outliers,
        }


def check_imu_video_sync(streams: List[StreamResult],
                         all_imu_parts: Optional[Dict[str, List[Tuple[int, List[int]]]]] = None) -> List[ImuSyncResult]:
    """检测 IMU 与左右视频时间戳是否同步。

    按源文件合并 accel+gyro, 分别对左/右 RGB 独立检测。
    当 all_imu_parts 提供时:
      - 视频帧优先匹配同分片 IMU, 不通过再依次匹配其他分片
      - 全部分片都不通过则在所有分片中找最近 IMU 时间戳输出
    """
    imu_streams = [s for s in streams if s.source_type in ("imu_accel", "imu_gyro") and s.timestamps_us]
    video_streams = [s for s in streams if s.source_type in ("mcap_video", "mp4_video", "video_pts") and s.timestamps_us]
    if not video_streams:
        return []
    if not imu_streams and not all_imu_parts:
        return []

    # 按源文件合并 IMU 流 (同一文件夹的 accel + gyro → 合并去重)
    imu_groups: Dict[str, List[int]] = {}
    for imu_s in imu_streams:
        base = _imu_base_key(imu_s.source_name)
        if base not in imu_groups:
            imu_groups[base] = []
        imu_groups[base].extend(imu_s.timestamps_us)
    for base in imu_groups:
        imu_groups[base] = sorted(set(imu_groups[base]))

    # 区分左右视频流, 每个视频流只归入一侧 (按 id 去重)
    seen_ids = set()
    left_videos = []
    right_videos = []
    for vs in video_streams:
        vid = id(vs)
        if vid in seen_ids:
            continue
        seen_ids.add(vid)
        name_lower = vs.source_name.lower()
        if any(kw in name_lower for kw in ("left", "camera_left", "/left", "_l_")):
            left_videos.append(vs)
        elif any(kw in name_lower for kw in ("right", "camera_right", "/right", "_r_")):
            right_videos.append(vs)
        else:
            left_videos.append(vs)
    left_videos.sort(key=lambda s: s.source_name)
    right_videos.sort(key=lambda s: s.source_name)

    # ---- 辅助: 从 all_imu_parts 中找到与 imu_base 匹配的所有分片时间戳 ----
    def _find_all_parts(imu_base: str) -> Optional[List[Tuple[int, List[int]]]]:
        """在 all_imu_parts 中查找匹配的所有分片 IMU 数据。"""
        if not all_imu_parts:
            return None
        if imu_base in all_imu_parts:
            return all_imu_parts[imu_base]
        imu_key = re.sub(r'_part\d+', '', imu_base.replace('.csv', ''),
                         flags=re.IGNORECASE)
        for g_key, g_parts in all_imu_parts.items():
            g_key_clean = g_key.replace('.csv', '')
            if imu_key == g_key_clean or imu_key in g_key_clean or g_key_clean in imu_key:
                return g_parts
        return None

    # ---- 辅助: 在所有分片中为视频帧找最近 IMU 时间戳 ----
    def _find_closest_across_parts(vt: int,
                                   parts: List[Tuple[int, List[int]]]
                                   ) -> Tuple[int, int, int]:
        """返回 (closest_ts, offset_us, part_num)。"""
        best_ts = 0
        best_off = float('inf')
        best_part = -1
        for part_num, imu_ts in parts:
            if not imu_ts:
                continue
            idx = bisect.bisect_left(imu_ts, vt)
            for ci in (idx - 1, idx):
                if 0 <= ci < len(imu_ts):
                    off = abs(imu_ts[ci] - vt)
                    if off < best_off:
                        best_off = off
                        best_ts = imu_ts[ci]
                        best_part = part_num
        return best_ts, int(best_off) if best_off < float('inf') else 0, best_part

    results = []

    for imu_base, imu_ts_local in imu_groups.items():
        imu_name = imu_base + "/imu" if not imu_base.endswith(".csv") else imu_base

        # 获取所有分片的 IMU 数据
        _all_parts = _find_all_parts(imu_base)
        if _all_parts is None and imu_ts_local:
            # 无跨分片数据, 仅用本地
            _all_parts = [(0, imu_ts_local)]

        # 计算总 IMU 数量 (所有分片之和)
        _imu_total = sum(len(ts) for _, ts in _all_parts) if _all_parts else len(imu_ts_local)
        _imu_label = imu_name

        # ---- 计算偏移: 先同分片, 再跨分片, 最后全局找最近 ----
        def _calc_multi_part(vid_ts_list: List[int], threshold_us: float,
                             vid_part: int) -> Optional[dict]:
            """对每个视频帧先匹配同分片 IMU, 不通过再跨分片搜索。"""
            offsets = []
            outliers = []
            step = max(1, len(vid_ts_list) // 1000)
            for vi in range(0, len(vid_ts_list), step):
                vt = vid_ts_list[vi]
                matched = False
                best_off = float('inf')
                best_ts = 0
                best_part = -1

                if _all_parts:
                    # 先试同分片 (part 编号匹配)
                    for part_num, imu_ts in _all_parts:
                        if part_num != vid_part:
                            continue
                        idx = bisect.bisect_left(imu_ts, vt)
                        for ci in (idx - 1, idx):
                            if 0 <= ci < len(imu_ts):
                                off = abs(imu_ts[ci] - vt)
                                if off <= threshold_us:
                                    matched = True
                                if off < best_off:
                                    best_off = off
                                    best_ts = imu_ts[ci]
                                    best_part = part_num

                    # 同分片不通过 → 依次试其他分片
                    if not matched:
                        for part_num, imu_ts in _all_parts:
                            if part_num == vid_part:
                                continue
                            idx = bisect.bisect_left(imu_ts, vt)
                            for ci in (idx - 1, idx):
                                if 0 <= ci < len(imu_ts):
                                    off = abs(imu_ts[ci] - vt)
                                    if off <= threshold_us:
                                        matched = True
                                    if off < best_off:
                                        best_off = off
                                        best_ts = imu_ts[ci]
                                        best_part = part_num
                            if matched:
                                break  # 找到即停

                else:
                    # 无跨分片数据, 用本地
                    idx = bisect.bisect_left(imu_ts_local, vt)
                    closest = imu_ts_local[min(idx, len(imu_ts_local) - 1)] if idx < len(imu_ts_local) else imu_ts_local[-1]
                    if idx > 0 and abs(imu_ts_local[idx - 1] - vt) < abs(closest - vt):
                        closest = imu_ts_local[idx - 1]
                    best_off = abs(closest - vt)
                    best_ts = closest
                    matched = best_off <= threshold_us

                if best_off < float('inf'):
                    offsets.append(int(best_off))
                    if not matched:
                        outliers.append(ImuSyncOutlier(
                            video_ts_us=vt, imu_ts_us=best_ts, offset_us=int(best_off),
                        ))

            if not offsets:
                return None
            if _HAS_NUMPY:
                off_arr = np.asarray(offsets, dtype=np.int64)
                return {"avg_off": float(np.mean(off_arr)), "max_off": int(np.max(off_arr)),
                        "min_off": int(np.min(off_arr)), "outliers": outliers}
            else:
                return {"avg_off": sum(offsets) / len(offsets), "max_off": max(offsets),
                        "min_off": min(offsets), "outliers": outliers}

        # ---- 对左右视频流做同步检测 ----
        _vid_part = _imu_part_number(imu_name)  # 当前文件夹的 part 编号

        for left_vs in left_videos:
            threshold_us = left_vs.expected_interval_us / 2.0
            sync = _calc_multi_part(left_vs.timestamps_us, threshold_us, _vid_part)
            if sync:
                results.append(ImuSyncResult(
                    imu_name=_imu_label,
                    video_name=left_vs.source_name,
                    imu_total=_imu_total,
                    video_total=len(left_vs.timestamps_us),
                    avg_offset_us=sync["avg_off"],
                    max_offset_us=sync["max_off"],
                    min_offset_us=sync["min_off"],
                    threshold_us=threshold_us,
                    is_synced=sync["max_off"] < threshold_us,
                    outliers=sync["outliers"],
                ))

        for right_vs in right_videos:
            threshold_us = right_vs.expected_interval_us / 2.0
            sync = _calc_multi_part(right_vs.timestamps_us, threshold_us, _vid_part)
            if sync:
                results.append(ImuSyncResult(
                    imu_name=_imu_label,
                    video_name=right_vs.source_name,
                    imu_total=_imu_total,
                    video_total=len(right_vs.timestamps_us),
                    avg_offset_us=sync["avg_off"],
                    max_offset_us=sync["max_off"],
                    min_offset_us=sync["min_off"],
                    threshold_us=threshold_us,
                    is_synced=sync["max_off"] < threshold_us,
                    outliers=sync["outliers"],
                ))

    return results


# ============================================================================
# 跨文件夹 IMU 后处理: 丢帧检测 + 全局同步重检
# ============================================================================

def _imu_base_key(source_name: str) -> str:
    """提取 IMU 源的基础名称 (去掉 part 编号和扩展名)。"""
    # MCAP: file.mcap/imu_accel → file (去掉 .mcap 和子路径)
    # CSV:  imu_part0000.csv → imu.csv
    if "/" in source_name:
        base = source_name.rsplit("/", 1)[-1]
    else:
        base = source_name
    # 去掉 part 编号
    base = re.sub(r'_part\d+', '', base, flags=re.IGNORECASE)
    return base


def _imu_part_number(source_name: str) -> int:
    """从源名称中提取 part 编号, 无则返回 0。"""
    m = re.search(r'_part(\d+)', source_name, re.IGNORECASE)
    return int(m.group(1)) if m else 0


def post_process_imu_global(all_results: List[FolderResult],
                            threshold_multiplier: float):
    """跨文件夹 IMU 后处理:

    1. 跨文件夹 IMU 丢帧检测 (partN 末帧 → partN+1 首帧)
    2. 收集各分片 IMU 数据 (不合并, 保持独立)
    3. 重新检测: 视频帧先匹配同分片 IMU, 不通过再依次匹配其他分片,
       全部分片都不通过则从所有分片中找最近时间戳输出
    """
    if len(all_results) < 1:
        return

    # ---- Step 1: 按 base key 收集所有 IMU 流 (跨文件夹) ----
    imu_items: Dict[str, List[Tuple[int, int, int]]] = defaultdict(list)
    for fi, fr in enumerate(all_results):
        for si, s in enumerate(fr.streams):
            if s.source_type not in ("imu_accel", "imu_gyro"):
                continue
            if not s.timestamps_us:
                continue
            base = _imu_base_key(s.source_name)
            part = _imu_part_number(s.source_name)
            imu_items[base].append((fi, si, part))

    # ---- Step 2: 跨文件夹 IMU 丢帧检测 ----
    for base, items in imu_items.items():
        if len(items) < 2:
            continue
        items.sort(key=lambda x: x[2])
        for i in range(len(items) - 1):
            prev_fi, prev_si, prev_part = items[i]
            curr_fi, curr_si, curr_part = items[i + 1]
            prev_s = all_results[prev_fi].streams[prev_si]
            curr_s = all_results[curr_fi].streams[curr_si]
            prev_last = prev_s.timestamps_us[-1]
            curr_first = curr_s.timestamps_us[0]
            delta = curr_first - prev_last
            if delta <= 0:
                continue
            rate = curr_s.expected_rate_hz
            interval = 1_000_000.0 / rate if rate > 0 else 1000.0
            if delta > interval * threshold_multiplier:
                lost = max(1, round(delta / interval) - 1)
                lost = min(lost, curr_s.total_frames)
                gap = FrameGap(
                    prev_frame_index=prev_s.total_frames,
                    curr_frame_index=prev_s.total_frames + 1,
                    prev_timestamp_us=prev_last,
                    curr_timestamp_us=curr_first,
                    prev_timestamp_s=prev_last / 1e6,
                    curr_timestamp_s=curr_first / 1e6,
                    gap_us=delta,
                    expected_interval_us=int(interval),
                    estimated_lost_frames=lost,
                )
                prev_s.gaps.append(gap)
                prev_s.total_gaps += 1
                prev_s.total_lost_frames += lost
                print(f"  [IMU跨文件丢帧] {prev_s.source_name} (part{prev_part}) "
                      f"→ {curr_s.source_name} (part{curr_part}): "
                      f"间隔 {delta}µs, 估计丢 {lost} 帧", flush=True)

    # ---- Step 3: 构建分片 IMU 数据 (不合并, 每个分片独立保留) ----
    # all_imu_parts: base_key -> [(part_num, sorted_timestamps), ...]
    all_imu_parts: Dict[str, List[Tuple[int, List[int]]]] = {}
    for base, items in imu_items.items():
        parts = []
        for fi, si, part in items:
            s = all_results[fi].streams[si]
            parts.append((part, sorted(set(s.timestamps_us))))
        parts.sort(key=lambda x: x[0])  # 按 part 编号排序
        all_imu_parts[base] = parts
        total_ts = sum(len(ts) for _, ts in parts)
        print(f"  [IMU分片收集] {base}: {len(parts)} 个分片, "
              f"共 {total_ts:,} 个时间戳 (未合并)", flush=True)

    # ---- Step 4: 用分片 IMU 数据重新检测 (先同分片, 再跨分片, 最后全局找最近) ----
    for fi, fr in enumerate(all_results):
        if not fr.imu_sync_results:
            continue

        print(f"\n  [IMU分片重检] 文件夹: {fr.folder_name}", flush=True)
        new_results = check_imu_video_sync(fr.streams, all_imu_parts=all_imu_parts)
        if new_results:
            fr.imu_sync_results = new_results
            for imu_r in fr.imu_sync_results:
                status = "[OK]" if imu_r.is_synced else "[FAIL]"
                print(f"    [IMU同步] {imu_r.imu_name} vs {imu_r.video_name}: "
                      f"{status} 平均偏移={imu_r.avg_offset_us:.0f}µs", flush=True)


# ============================================================================
# 帧截取
# ============================================================================

def _extract_frame(mp4_path: str, frame_idx: int, output_path: str) -> bool:
    """
    用 ffmpeg select=eq(n,帧号) 精准提取指定帧号。
    Popen + proc.wait(timeout=60) + proc.kill() — video_analyzer730.py 验证通过。
    """
    try:
        proc = subprocess.Popen(
            ["ffmpeg", "-i", mp4_path,
             "-vf", f"select=eq(n\\,{frame_idx})",
             "-vframes", "1", "-q:v", "2", "-y", output_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return False
        return proc.returncode == 0 and \
            Path(output_path).exists() and Path(output_path).stat().st_size > 100
    except Exception:
        return False


def dump_frame_pairs(sync_result, folder_path: str, output_base: str,
                     random_synced: bool = True, do_extract: bool = True):
    """
    截取帧对:
    - 不同步: 截取前 10 组 prev/curr/next PNG
    - 同步: 对每个左帧二分查找最接近右帧, 随机选 10 对输出 JPG
    输出到 {output_base}_frames/
    """
    if sync_result is None: return

    # MCAP 源无需截取
    is_mcap = ".mcap" in sync_result.left_name.lower() or ".mcap" in sync_result.right_name.lower()
    if is_mcap:
        print(f"    MCAP 源无需图像截取, 跳过", flush=True)
        return

    # 找源 MP4 文件
    left_mp4 = right_mp4 = None
    for f in Path(folder_path).iterdir():
        if not f.is_file() or f.suffix.lower() != '.mp4': continue
        n = f.name.lower()
        if "left" in n: left_mp4 = str(f)
        elif "right" in n: right_mp4 = str(f)
    if not (left_mp4 or right_mp4):
        print(f"    未找到 MP4 源文件, 跳过截取", flush=True)
        return

    dump_dir = f"{output_base}_frames"
    os.makedirs(dump_dir, exist_ok=True)

    left_ts_arr = sync_result.left_timestamps
    right_ts_arr = sync_result.right_timestamps

    print(f"    同步状态: {'已同步' if sync_result.is_synced else '未同步'}, "
          f"匹配={sync_result.matched_pairs}对, "
          f"未配对={len(sync_result.unpaired)}处", flush=True)

    meta = {
        "sync_status": "synced" if sync_result.is_synced else "out_of_sync",
        "left_name": sync_result.left_name, "right_name": sync_result.right_name,
        "threshold_us": sync_result.threshold_us, "pairs": [],
    }

    # ---- 不同步帧截取 ----
    MISMATCH_LIMIT = 10
    if not sync_result.is_synced and sync_result.unpaired:
        total_unpaired = len(sync_result.unpaired)
        extract_n = min(MISMATCH_LIMIT, total_unpaired)
        print(f"    不同步帧: {total_unpaired} 处, 截取前 {extract_n} 组...", flush=True)
        total_ok = 0
        total_left = sync_result.total_left
        total_right = sync_result.total_right
        for ui, u in enumerate(sync_result.unpaired):
            if ui >= MISMATCH_LIMIT: break
            side = u.side
            ts = u.left_ts_us if u.left_ts_us else u.right_ts_us
            idx = len(meta["pairs"]) + 1
            meta["pairs"].append({
                "index": idx, "type": side, "timestamp_us": ts,
                "timestamp_s": ts / 1e6,
                "note": f"不同步帧 ({side})",
            })
            if do_extract:
                for cam, mp4_path in [("left", left_mp4), ("right", right_mp4)]:
                    if not mp4_path: continue
                    total = total_left if cam == "left" else total_right
                    frame_i = max(0, min(total - 1,
                        round(idx * total / max(1, total_left + total_right) * 2)))
                    png_dir = os.path.join(dump_dir, f"mismatch_{cam}_{ts}")
                    os.makedirs(png_dir, exist_ok=True)
                    for label, offset in [("prev", -1), ("curr", 0), ("next", 1)]:
                        fi = frame_i + offset
                        if fi < 0: continue
                        out_path = os.path.join(png_dir, f"{label}.png")
                        if _extract_frame(mp4_path, fi, out_path):
                            total_ok += 1
        print(f"    不同步帧截取: {total_ok} 张", flush=True)

    # ---- 同步帧截取: 完全参考 tn2608 _run_video_sync 阶段1 ----
    #     has_match 二分匹配 → 从匹配对中随机选 10 组 → 精准提取
    if random_synced and do_extract and left_ts_arr and right_ts_arr:
        threshold_us = sync_result.threshold_us
        rlen = len(right_ts_arr)

        # has_match (tn2608): 每个左帧二分查找最接近的右帧
        match_res = []
        nearest_idx = []
        for lts in left_ts_arr:
            pos = bisect.bisect_left(right_ts_arr, lts)
            best_idx = None
            best_diff = float('inf')
            for cp in (pos - 1, pos):
                if 0 <= cp < rlen:
                    d = abs(right_ts_arr[cp] - lts)
                    if d < best_diff:
                        best_diff = d
                        best_idx = cp
            match_res.append(best_diff < threshold_us)
            nearest_idx.append(best_idx)

        # 阶段1 (tn2608): 提取匹配帧, 最多10组, 超时自动跳过换下一对
        matched_indices = [i for i, m in enumerate(match_res) if m]
        if len(matched_indices) == 0:
            print(f"    同步帧: 无匹配对 (阈值={threshold_us:.0f}µs), 跳过", flush=True)
        else:
            # 从前 500 帧随机选 (帧号越低解码越快, 500 帧池兼顾随机性与速度)
            pool = matched_indices[:min(500, len(matched_indices))]
            random.shuffle(pool)
            print(f"    同步帧: {len(matched_indices)} 对匹配, 抽取中...", flush=True)

            sync_dir = os.path.join(dump_dir, "pair_1")
            os.makedirs(sync_dir, exist_ok=True)
            lbase = Path(left_mp4).stem if left_mp4 else "left"
            rbase = Path(right_mp4).stem if right_mp4 else "right"

            synced_count = 0
            for idx in pool:
                if synced_count >= 10:
                    break
                right_idx = nearest_idx[idx]
                if right_idx is None:
                    continue
                lts = left_ts_arr[idx]
                rts = right_ts_arr[right_idx]

                left_ok = right_ok = True
                if left_mp4:
                    left_img = os.path.join(sync_dir, f"{lbase}_{int(lts)}.jpg")
                    if not _extract_frame(left_mp4, idx, left_img):
                        left_ok = False
                if right_mp4:
                    right_img = os.path.join(sync_dir, f"{rbase}_{int(rts)}.jpg")
                    if not _extract_frame(right_mp4, right_idx, right_img):
                        right_ok = False

                if left_ok and right_ok:
                    synced_count += 1
                    diff_us = abs(lts - rts)
                    meta["pairs"].append({
                        "index": len(meta["pairs"]) + 1,
                        "type": "synced_sample",
                        "sample_position": f"L#{idx}/R#{right_idx}",
                        "diff_us": diff_us,
                        "note": f"同步采样 左#{idx}(ts={lts}) 右#{right_idx}(ts={rts}) diff={diff_us}us",
                    })
                else:
                    for ip in (left_img if left_mp4 else "", right_img if right_mp4 else ""):
                        if ip and os.path.exists(ip):
                            try: os.remove(ip)
                            except OSError: pass

            print(f"    同步采样帧: {synced_count} 组 (共 {synced_count * 2} 张)",
                  flush=True)

    meta_path = os.path.join(dump_dir, "frame_pairs_meta.json")
    with open(meta_path, "w", encoding="utf-8") as ff:
        json.dump(meta, ff, ensure_ascii=False, indent=2)
    return dump_dir


# ============================================================================
# 目录扫描 & 综合分析
# ============================================================================

def find_mp4_pts_pairs(subdir: Path) -> List[Tuple[str, Optional[str]]]:
    """在子文件夹中找 MP4 和对应的 PTS CSV 配对。"""
    pairs = []
    # 同时扫描 .mp4 和 .MP4 (Linux 区分大小写)
    mp4_files = sorted(set(
        list(subdir.glob("*.mp4")) + list(subdir.glob("*.MP4"))
    ), key=lambda p: p.name.lower())
    for mp4_path in mp4_files:
        stem = mp4_path.stem
        # 尝试 *_{stem}_pts.csv 和 {stem}_pts.csv 两种命名模式
        csv_candidates = [
            mp4_path.with_name(stem + "_pts.csv"),
        ]
        # 也尝试全局搜索匹配的 CSV
        csv_path = None
        for c in csv_candidates:
            if c.exists():
                csv_path = str(c)
                break
        if csv_path is None:
            # 模糊匹配: 找含有相同方向关键词的 CSV
            for csv_f in sorted(subdir.glob("*_pts.csv")):
                csv_stem = csv_f.stem.replace("_pts", "")
                if csv_stem in stem or stem in csv_stem:
                    csv_path = str(csv_f)
                    break
            # 也尝试非 _pts 命名的 CSV
            for csv_f in sorted(subdir.glob("*.csv")):
                csv_stem = csv_f.stem
                if csv_stem in stem or stem in csv_stem:
                    if "sei" not in csv_f.name.lower():
                        csv_path = str(csv_f)
                        break
        pairs.append((str(mp4_path), csv_path))
    return pairs


def _check_split_file_gaps(fr: FolderResult, threshold_multiplier: float):
    """分片文件跨文件丢帧检测: part0001 最后一帧 vs part0002 第一帧。"""
    part_streams = [s for s in fr.streams
                    if s.source_type == "mp4_video" and "_part" in s.source_name.lower()]
    if len(part_streams) < 2:
        return
    # 按 base name 分组
    groups = defaultdict(list)
    for s in part_streams:
        base = re.sub(r'_part\d+', '', s.source_name, flags=re.IGNORECASE)
        m = re.search(r'_part(\d+)', s.source_name, re.IGNORECASE)
        seq = int(m.group(1)) if m else 0
        groups[base].append((seq, s))
    for base, items in groups.items():
        if len(items) < 2:
            continue
        items.sort(key=lambda x: x[0])
        for i in range(len(items) - 1):
            prev_s = items[i][1]
            curr_s = items[i + 1][1]
            if not prev_s.timestamps_us or not curr_s.timestamps_us:
                continue
            prev_last = prev_s.timestamps_us[-1]
            curr_first = curr_s.timestamps_us[0]
            delta = curr_first - prev_last
            if delta <= 0:
                continue
            rate = max(prev_s.expected_rate_hz, curr_s.expected_rate_hz)
            interval = 1_000_000.0 / rate if rate > 0 else 33333
            if delta > interval * threshold_multiplier:
                lost = max(1, round(delta / interval) - 1)
                # 封顶: 丢失帧数不应超过下一片的总帧数
                lost = min(lost, curr_s.total_frames)
                gap = FrameGap(
                    prev_frame_index=prev_s.total_frames,
                    curr_frame_index=prev_s.total_frames + 1,
                    prev_timestamp_us=prev_last,
                    curr_timestamp_us=curr_first,
                    prev_timestamp_s=prev_last / 1e6,
                    curr_timestamp_s=curr_first / 1e6,
                    gap_us=delta,
                    expected_interval_us=int(interval),
                    estimated_lost_frames=lost,
                )
                prev_s.gaps.append(gap)
                prev_s.total_gaps += 1
                prev_s.total_lost_frames += lost
                print(f"    分片丢帧: {prev_s.source_name} → {curr_s.source_name}: "
                      f"间隔 {delta}µs, 估计丢 {lost} 帧", flush=True)


def scan_and_analyze(root_path: str, threshold_multiplier: float = 1.5,
                     verbose: bool = False, skip_sync: bool = False,
                     should_cancel: Optional[Callable[[], bool]] = None) -> List[FolderResult]:
    """扫描并分析根目录。

    should_cancel: 可选回调，返回 True 时停止处理并返回已完成的中间结果（支持 GUI 取消）。
    """
    root = Path(root_path).resolve()
    if not root.exists():
        raise FileNotFoundError(f"路径不存在: {root_path}")
    if root.is_dir():
        def _is_valid_dir(p: Path) -> bool:
            if p.name.startswith('.'): return False
            if p.name == '__pycache__': return False
            if p.name.endswith('_output'): return False
            if 'output' in p.name.lower(): return False
            if p.name.endswith('_frames'): return False
            if p.name.startswith('mismatch_'): return False
            if p.name.startswith('synced_'): return False
            if p.name == 'frame_dumps': return False
            if p.name == 'sei_extracted': return False
            return True
        subdirs = sorted([p for p in root.rglob('*')
                                   if p.is_dir() and _is_valid_dir(p)])
    else:
        subdirs = [root.parent]
    all_results = []
    for sd in subdirs:
        if should_cancel and should_cancel():
            print(f"  已取消：停止分析，返回已完成的 {len(all_results)} 个文件夹", flush=True)
            break
        print(f"\n{'='*60}")
        print(f"  分析文件夹: {sd.name}")
        print(f"{'='*60}")
        fr = FolderResult(folder_name=sd.name, folder_path=str(sd))
        # ---- 阶段 1: SEI 提取 & CSV 比对 ----
        mp4_pts_pairs = find_mp4_pts_pairs(sd)
        # 收集所有有 SEI 的 MP4 时间戳
        mp4_sei_map: Dict[str, List[int]] = {}  # mp4_path -> sei_timestamps
        for mp4_path, csv_path in mp4_pts_pairs:
            mp4_name = os.path.relpath(mp4_path, str(sd))
            print(f"  [SEI提取] {mp4_name}")
            try:
                sei_ts, codec, fmt, tier, error = extract_sei_from_mp4(mp4_path, verbose=verbose)
            except Exception as e:
                fr.sei_csv_results.append(SeiCsvResult(
                    mp4_path=mp4_path, mp4_name=mp4_name,
                    codec="unknown", error=f"SEI提取失败: {e}",
                ))
                continue
            if not sei_ts:
                fr.sei_csv_results.append(SeiCsvResult(
                    mp4_path=mp4_path, mp4_name=mp4_name, csv_path=csv_path or "",
                    codec=codec, extraction_tier=tier,
                    error=error or "未提取到 SEI",
                ))
                continue
            mp4_sei_map[mp4_path] = sei_ts
            sei_ts_sorted = sorted(sei_ts)
            if csv_path:
                try:
                    csv_ts = read_pts_csv(csv_path)
                except Exception as e:
                    fr.sei_csv_results.append(SeiCsvResult(
                        mp4_path=mp4_path, mp4_name=mp4_name, csv_path=csv_path,
                        codec=codec, extraction_tier=tier,
                        sei_timestamps=sei_ts_sorted,
                        error=f"CSV读取失败: {e}",
                    ))
                    continue
                csv_ts_sorted = sorted(csv_ts)
                # 数量比对
                sei_count = len(sei_ts_sorted)
                csv_count = len(csv_ts_sorted)
                if sei_count != csv_count:
                    print(f"    [WARN] 数量不一致! SEI: {sei_count}, CSV: {csv_count}")
                    fr.sei_csv_results.append(SeiCsvResult(
                        mp4_path=mp4_path, mp4_name=mp4_name, csv_path=csv_path,
                        codec=codec, extraction_tier=tier,
                        sei_count=sei_count, csv_count=csv_count,
                        count_match=False, match=False,
                        sei_timestamps=sei_ts_sorted, csv_timestamps=csv_ts_sorted,
                    ))
                    fr.count_mismatch_files.append(mp4_name)
                    continue  # 跳过后续处理
                # 数量一致，逐条比对
                cmp = compare_sei_csv(sei_ts_sorted, csv_ts_sorted)
                mismatches = cmp["csv_not_in_sei"] + cmp["sei_not_in_csv"]
                if mismatches:
                    print(f"    [MISMATCH] 时间戳不一致: {len(mismatches)} 处差异")
                else:
                    print(f"    [OK] SEI vs CSV: 完全一致 ({sei_count} 帧)")
                fr.sei_csv_results.append(SeiCsvResult(
                    mp4_path=mp4_path, mp4_name=mp4_name, csv_path=csv_path,
                    codec=codec, extraction_tier=tier,
                    sei_count=sei_count, csv_count=csv_count,
                    count_match=True, match=cmp["match"],
                    mismatches=mismatches,
                    sei_timestamps=sei_ts_sorted, csv_timestamps=csv_ts_sorted,
                ))
            else:
                # 无 CSV 文件，只有 SEI
                fr.sei_csv_results.append(SeiCsvResult(
                    mp4_path=mp4_path, mp4_name=mp4_name,
                    codec=codec, extraction_tier=tier,
                    sei_count=len(sei_ts_sorted), csv_count=0,
                    count_match=False, match=False,
                    sei_timestamps=sei_ts_sorted,
                ))
        # ---- 保存 SEI 时间戳到文件夹 ----
        sei_output_dir = os.path.join(str(sd), "sei_extracted")
        for scr in fr.sei_csv_results:
            if scr.sei_timestamps:
                os.makedirs(sei_output_dir, exist_ok=True)
                stem = Path(scr.mp4_path).stem
                sei_csv_path = os.path.join(sei_output_dir, f"{stem}_sei.csv")
                try:
                    with open(sei_csv_path, "w", encoding="utf-8", newline="") as sf:
                        writer = csv.writer(sf)
                        writer.writerow(["frame_index", "timestamp_us"])
                        for idx, ts in enumerate(scr.sei_timestamps):
                            writer.writerow([idx + 1, ts])
                    print(f"    SEI 已保存: {sei_csv_path} ({len(scr.sei_timestamps)} 条)")
                except Exception as e:
                    print(f"    SEI 保存失败: {e}")
        # ---- 阶段 2: 丢帧检测 ----
        # MCAP 文件
        mcap_files = sorted(set(
            list(sd.glob("*.mcap")) + list(sd.glob("*.MCAP"))
        ), key=lambda p: p.name.lower())    # 查找目录下的所有mcap文件
        for mf in sorted(mcap_files):
            print(f"  [丢帧检测] MCAP: {mf.name}")
            try:
                mcap_results = analyze_mcap(str(mf), threshold_multiplier)
                fr.streams.extend(mcap_results)
                # 保存 MCAP 视频 SEI 时间戳到 CSV
                sei_output_dir = os.path.join(str(sd), "sei_extracted")
                for s in mcap_results:
                    if s.source_type == "mcap_video" and s.timestamps_us:
                        os.makedirs(sei_output_dir, exist_ok=True)
                        tname = s.source_name.split("/")[-1] if "/" in s.source_name else s.source_name
                        sei_csv_path = os.path.join(sei_output_dir, f"{mf.stem}_{tname}_sei.csv")
                        try:
                            with open(sei_csv_path, "w", encoding="utf-8", newline="") as sf:
                                writer = csv.writer(sf)
                                writer.writerow(["frame_index", "timestamp_us"])
                                for idx, ts in enumerate(s.timestamps_us):
                                    writer.writerow([idx + 1, ts])
                            print(f"    MCAP SEI 已保存: {sei_csv_path} ({len(s.timestamps_us)} 条)")
                        except Exception as e:
                            print(f"    MCAP SEI 保存失败: {e}")
            except Exception as e:
                fr.streams.append(StreamResult(
                    source_name=mf.name, source_type="mcap",
                    total_frames=0, expected_rate_hz=0, expected_interval_us=0,
                    duration_s=0, actual_rate_hz=0, sei_extracted=0,
                    total_gaps=0, total_lost_frames=0, error=str(e),
                ))
        # MP4 丢帧检测 (对所有 MP4 文件, 不因 SEI/CSV 数量不一致而跳过)
        for mp4_path, csv_path in mp4_pts_pairs:
            mp4_rel = os.path.relpath(mp4_path, str(sd))
            print(f"  [丢帧检测] MP4: {mp4_rel}")
            try:
                mp4_streams = analyze_mp4_drops(mp4_path, threshold_multiplier)
                for s in mp4_streams:
                    s.source_type = "mp4_video"
                fr.streams.extend(mp4_streams)
            except Exception as e:
                fr.streams.append(StreamResult(
                    source_name=mp4_rel, source_type="mp4_video",
                    total_frames=0, expected_rate_hz=0, expected_interval_us=0,
                    duration_s=0, actual_rate_hz=0, sei_extracted=0,
                    total_gaps=0, total_lost_frames=0, error=str(e),
                ))
        # CSV PTS 丢帧检测 (跳过音频 以及 IMU 类 CSV)
        _imu_keywords = ("imu", "accel", "gyro")
        for f in sorted(sd.glob("*_pts.csv")):
            if "audio" in f.name.lower():
                continue
            if any(kw in f.name.lower() for kw in _imu_keywords):
                continue  # IMU 类 CSV 由下方单独处理
            print(f"  [丢帧检测] CSV: {f.name}")
            try:
                ts_list = read_pts_csv(str(f))
                if ts_list:
                    ts_list = normalize_timestamps(ts_list)
                    ts_list = _clean_timestamps(ts_list)
                    rate = auto_detect_rate(ts_list)
                    gaps = detect_gaps(ts_list, rate, threshold_multiplier)
                    dur = (ts_list[-1] - ts_list[0]) / 1e6 if len(ts_list) >= 2 else 0
                    actual = (len(ts_list) - 1) / dur if dur > 0 else 0
                    fr.streams.append(StreamResult(
                        source_name=f.name, source_type="video_pts",
                        total_frames=len(ts_list), expected_rate_hz=rate,
                        expected_interval_us=1e6 / max(1, rate),
                        duration_s=dur, actual_rate_hz=actual,
                        sei_extracted=len(ts_list),
                        total_gaps=len(gaps),
                        total_lost_frames=min(sum(g.estimated_lost_frames for g in gaps), len(ts_list)),
                        gaps=gaps,
                        first_ts_us=ts_list[0] if ts_list else 0,
                        last_ts_us=ts_list[-1] if ts_list else 0,
                        timestamps_us=list(ts_list),
                    ))
            except Exception as e:
                fr.streams.append(StreamResult(
                    source_name=f.name, source_type="video_pts",
                    total_frames=0, expected_rate_hz=0, expected_interval_us=0,
                    duration_s=0, actual_rate_hz=0, sei_extracted=0,
                    total_gaps=0, total_lost_frames=0, error=str(e),
                ))

        # ---- 补充: 将 SEI/CSV 比对中已读取的左右 CSV 时间戳也加入 video_pts 流 ----
        # 原因: find_mp4_pts_pairs 能找到非 _pts 命名的 CSV, 但其时间戳只存在
        #        fr.sei_csv_results 中, 未加入 fr.streams, 导致 check_imu_video_sync
        #        无法检测 IMU 与左右 CSV 的同步。
        _existing_pts_names = {s.source_name for s in fr.streams
                               if s.source_type == "video_pts"}
        _left_right_kw = ("left", "right", "camera_left", "camera_right",
                          "/left", "/right", "_l_", "_r_")
        for scr in fr.sei_csv_results:
            if not scr.csv_timestamps or not scr.csv_path:
                continue
            _csv_name = os.path.basename(scr.csv_path)
            if _csv_name in _existing_pts_names:
                continue  # 已由上方 *_pts.csv 分支添加, 跳过
            # 只对包含左右关键词的 CSV 做 IMU 同步检测
            if not any(kw in _csv_name.lower() for kw in _left_right_kw):
                continue
            _ts = sorted(scr.csv_timestamps)
            _rate = auto_detect_rate(_ts) if len(_ts) >= 10 else 30.0
            _dur = (_ts[-1] - _ts[0]) / 1e6 if len(_ts) >= 2 else 0
            _actual = (len(_ts) - 1) / _dur if _dur > 0 else 0
            _gaps = detect_gaps(_ts, _rate, threshold_multiplier)
            fr.streams.append(StreamResult(
                source_name=_csv_name, source_type="video_pts",
                total_frames=len(_ts), expected_rate_hz=_rate,
                expected_interval_us=1e6 / max(1, _rate),
                duration_s=_dur, actual_rate_hz=_actual,
                sei_extracted=len(_ts),
                total_gaps=len(_gaps),
                total_lost_frames=min(sum(g.estimated_lost_frames for g in _gaps), len(_ts)),
                gaps=_gaps,
                first_ts_us=_ts[0] if _ts else 0,
                last_ts_us=_ts[-1] if _ts else 0,
                timestamps_us=list(_ts),
            ))
            _existing_pts_names.add(_csv_name)
            print(f"  [IMU同步补录] CSV: {_csv_name} ({len(_ts)} 条) → video_pts 流", flush=True)

        # IMU CSV 检测 (imu.csv / accel.csv / gyro.csv 等)
        imu_csv_patterns = ["imu.csv", "*_imu.csv", "*imu_*.csv",
                           "*accel*.csv", "*gyro*.csv", "*imu*.csv"]
        imu_csv_seen = set()
        for pat in imu_csv_patterns:
            for f in sorted(sd.glob(pat)):
                f_str = str(f)
                if f_str in imu_csv_seen:
                    continue
                imu_csv_seen.add(f_str)
                # 确定类型
                f_lower = f.name.lower()
                if "gyro" in f_lower:
                    imu_type = "imu_gyro"
                else:
                    imu_type = "imu_accel"
                print(f"  [IMU检测] CSV: {f.name} (类型={imu_type})")
                try:
                    ts_list = read_pts_csv(f_str)
                    if ts_list:
                        ts_list = normalize_timestamps(ts_list)
                        ts_list = sorted(ts_list)
                        rate = 1000.0  # IMU 默认 1000Hz
                        gaps = detect_gaps(ts_list, rate, threshold_multiplier)
                        dur = (ts_list[-1] - ts_list[0]) / 1e6 if len(ts_list) >= 2 else 0
                        actual = (len(ts_list) - 1) / dur if dur > 0 else 0
                        fr.streams.append(StreamResult(
                            source_name=f.name, source_type=imu_type,
                            total_frames=len(ts_list), expected_rate_hz=rate,
                            expected_interval_us=1e6 / rate,
                            duration_s=dur, actual_rate_hz=actual,
                            sei_extracted=0,
                            total_gaps=len(gaps),
                            total_lost_frames=min(sum(g.estimated_lost_frames for g in gaps), len(ts_list)),
                            gaps=gaps,
                            first_ts_us=ts_list[0] if ts_list else 0,
                            last_ts_us=ts_list[-1] if ts_list else 0,
                            timestamps_us=list(ts_list),
                        ))
                except Exception as e:
                    fr.streams.append(StreamResult(
                        source_name=f.name, source_type=imu_type,
                        total_frames=0, expected_rate_hz=0, expected_interval_us=0,
                        duration_s=0, actual_rate_hz=0, sei_extracted=0,
                        total_gaps=0, total_lost_frames=0, error=str(e),
                    ))

        # ---- 检查分片文件跨文件丢帧 ----
        _check_split_file_gaps(fr, threshold_multiplier)

        # ---- 阶段 3: 左右同步检测 (仅 MP4 文件夹, MCAP 不参与) ----
        if skip_sync:
            print(f"  [同步检测] 左右 Color... 已跳过 (--no-sync-check)")
        else:
            has_mp4_files = len(mp4_pts_pairs) > 0
            has_mcap_files = len(mcap_files) > 0
            if has_mp4_files or has_mcap_files:
                print(f"  [同步检测] 左右 Color...")
                fr.sync_result = check_stereo_sync(fr.streams)
                if fr.sync_result:
                    status = "[OK] 同步" if fr.sync_result.is_synced else f"[FAIL] 不同步 ({len(fr.sync_result.unpaired)} 处)"
                    print(f"    {status}")
                else:
                    print(f"    未找到左右配对")
        # ---- IMU 与视频同步检测 ----
        fr.imu_sync_results = check_imu_video_sync(fr.streams)
        # 分片局部 IMU 同步检测 (仅作参考, 最终结果由跨文件夹全局重检覆盖)
        if fr.imu_sync_results:
            for imu_r in fr.imu_sync_results:
                status = "[OK]" if imu_r.is_synced else "[FAIL]"
                print(f"  [IMU同步(局部)] {imu_r.imu_name} vs {imu_r.video_name}: "
                      f"{status} 平均偏移={imu_r.avg_offset_us:.0f}µs", flush=True)

        all_results.append(fr)

        # ★ 每个文件夹处理完后强制回收内存
        gc.collect()

    return all_results


# ============================================================================
# HTML 报告生成
# ============================================================================

def _ts_to_str(ts_us: int) -> str:
    if ts_us <= 0:
        return "N/A"
    s = ts_us / 1e6
    if s < 0 or s > 4102444800:
        return f"{ts_us:,} µs"
    try:
        dt = datetime.fromtimestamp(s, tz=timezone.utc)
        return f"{ts_us:,} µs ({dt.strftime('%Y-%m-%d %H:%M:%S')}.{int((s % 1) * 1e6):06d})"
    except (OSError, OverflowError, ValueError):
        return f"{ts_us:,} µs"


def _badge(ok: bool) -> str:
    if ok:
        return '<span class="badge pass">PASS</span>'
    return '<span class="badge fail">FAIL</span>'


def _progress_bar(ratio: float) -> str:
    pct = min(100, max(0, ratio * 100))
    color = "#4caf50" if pct < 5 else ("#ff9800" if pct < 20 else "#f44336")
    return f'<div class="bar-bg"><div class="bar-fill" style="width:{pct:.1f}%;background:{color};"></div></div>'


def generate_html(all_results: List[FolderResult], output_path: str, root_path: str,
                   skip_sync: bool = False):
    total_folders = len(all_results)
    total_mp4 = sum(len(fr.sei_csv_results) for fr in all_results)
    total_sei_csv_match = sum(1 for fr in all_results for r in fr.sei_csv_results if r.match and r.count_match)
    total_sei_csv_mismatch = sum(1 for fr in all_results for r in fr.sei_csv_results if not r.match)
    total_count_mismatch = sum(1 for fr in all_results for r in fr.sei_csv_results if not r.count_match)
    total_streams = sum(len(fr.streams) for fr in all_results)
    total_gaps = sum(s.total_gaps for fr in all_results for s in fr.streams)
    total_lost = sum(s.total_lost_frames for fr in all_results for s in fr.streams)
    sync_pairs = sum(1 for fr in all_results if fr.sync_result is not None)
    sync_issues = sum(1 for fr in all_results if fr.sync_result is not None and not fr.sync_result.is_synced)
    error_count = sum(1 for fr in all_results for s in fr.streams if s.error)

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    html = io.StringIO()
    html.write(f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>丢帧检测-比对报告 — {Path(root_path).name}</title>
<style>
  :root {{ --bg: #1a1a2e; --card: #16213e; --text: #e0e0e0; --muted: #8892b0;
          --accent: #64ffda; --red: #f44336; --green: #4caf50; --orange: #ff9800; }}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family: 'Segoe UI', system-ui, sans-serif; background:var(--bg); color:var(--text); line-height:1.6; overflow-x:auto; }}
  .container {{ margin:0 auto; padding:24px; overflow-x:auto; }}
  h1 {{ color:var(--accent); font-size:28px; margin-bottom:4px; }}
  h2 {{ color:var(--accent); font-size:20px; margin:24px 0 12px; border-bottom:2px solid #333; padding-bottom:8px; }}
  h3 {{ font-size:16px; margin:16px 0 8px; color:#ccc; }}
  .subtitle {{ color:var(--muted); font-size:14px; margin-bottom:24px; }}
  .summary {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:12px; margin-bottom:32px; }}
  .sum-card {{ background:var(--card); border-radius:8px; padding:16px; text-align:center; }}
  .sum-card .num {{ font-size:32px; font-weight:700; }}
  .sum-card .label {{ color:var(--muted); font-size:13px; margin-top:4px; }}
  .num.ok {{ color:var(--green); }} .num.warn {{ color:var(--orange); }} .num.err {{ color:var(--red); }}
  .folder {{ background:var(--card); border-radius:10px; margin-bottom:20px; overflow-x:auto; }}
  .folder-header {{ padding:14px 20px; display:flex; justify-content:space-between; align-items:center; cursor:pointer; border-bottom:1px solid #2a2a4a; }}
  .folder-header:hover {{ background:#1a2740; }}
  .folder-name {{ font-weight:600; font-size:16px; }}
  .folder-stats {{ display:flex; gap:16px; font-size:13px; color:var(--muted); flex-wrap:wrap; }}
  .folder-body {{ padding:0; display:none; overflow-x:auto; }}
  .folder.open .folder-body {{ display:block; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th {{ background:#0d1b33; color:var(--muted); font-weight:600; padding:8px 12px; text-align:left; white-space:nowrap; }}
  td {{ padding:8px 12px; border-bottom:1px solid #1a1a3a; }}
  tr:hover {{ background:#1a2740; }}
  .badge {{ display:inline-block; padding:2px 10px; border-radius:10px; font-size:11px; font-weight:700; text-transform:uppercase; }}
  .badge.pass {{ background:#1b5e20; color:#81c784; }}
  .badge.fail {{ background:#b71c1c; color:#ef9a9a; }}
  .badge.warn {{ background:#e65100; color:#ffcc80; }}
  .bar-bg {{ width:80px; height:6px; background:#333; border-radius:3px; display:inline-block; vertical-align:middle; margin-right:4px; }}
  .bar-fill {{ height:6px; border-radius:3px; }}
  .section-box {{ background:#16213e; border-radius:10px; margin:16px 0; padding:20px; overflow-x:auto; }}
  .section-box.match {{ border:1px solid #4caf50; }}
  .section-box.mismatch {{ border:1px solid #f44336; }}
  .section-box.count-mismatch {{ border:2px solid #ff9800; }}
  .gap-detail {{ background:#0d1b33; border-radius:6px; margin:8px 0; padding:12px; overflow-x:auto; }}
  .gap-detail summary {{ cursor:pointer; color:var(--orange); font-weight:600; }}
  .gap-detail table {{ margin-top:8px; }}
  .error-msg {{ color:var(--red); font-size:12px; padding:8px 12px; }}
  .type-tag {{ display:inline-block; font-size:10px; padding:1px 6px; border-radius:3px; margin-right:4px; text-transform:uppercase; }}
  .type-tag.mcap {{ background:#1565c0; color:#90caf9; }}
  .type-tag.mp4 {{ background:#6a1b9a; color:#ce93d8; }}
  .type-tag.pts {{ background:#00695c; color:#80cbc4; }}
  .type-tag.imu {{ background:#00695c; color:#80cbc4; }}
  .toggle-btn {{ font-size:12px; color:var(--accent); cursor:pointer; background:none; border:1px solid #333; border-radius:4px; padding:4px 10px; margin:2px; }}
  .toggle-btn:hover {{ background:#1a2740; }}
  .count-warn {{ background:#3e2723; color:#ffcc80; padding:4px 8px; border-radius:4px; font-weight:600; }}
  .mismatch-table {{ margin-top:8px; }}
  .mismatch-table summary {{ color:var(--orange); cursor:pointer; font-weight:600; }}
  footer {{ text-align:center; color:var(--muted); font-size:12px; margin-top:40px; padding-top:20px; border-top:1px solid #333; }}
</style>
</head>
<body>
<div class="container">
<h1>🔍 丢帧检测-比对报告</h1>
<p class="subtitle">根目录: <code>{root_path}</code> &nbsp;|&nbsp; 生成时间: {now_str}</p>

<h2>📊 全局汇总</h2>
<div class="summary">
  <div class="sum-card"><div class="num">{total_folders}</div><div class="label">扫描文件夹</div></div>
  <div class="sum-card"><div class="num">{total_mp4}</div><div class="label">MP4 文件总数</div></div>
  <div class="sum-card"><div class="num {'ok' if total_sei_csv_match>0 else ''}">{total_sei_csv_match}</div><div class="label">SEI/CSV 一致</div></div>
  <div class="sum-card"><div class="num {'err' if total_sei_csv_mismatch>0 else 'ok'}">{total_sei_csv_mismatch}</div><div class="label">SEI/CSV 不一致</div></div>
  <div class="sum-card"><div class="num {'err' if total_count_mismatch>0 else 'ok'}">{total_count_mismatch}</div><div class="label">SEI/CSV 数量不同</div></div>
  <div class="sum-card"><div class="num">{total_streams}</div><div class="label">数据流总数</div></div>
  <div class="sum-card"><div class="num {'err' if total_gaps>0 else 'ok'}">{total_gaps}</div><div class="label">丢帧事件总数</div></div>
  <div class="sum-card"><div class="num {'err' if total_lost>0 else 'ok'}">{total_lost}</div><div class="label">估计丢帧总数</div></div>
  <div class="sum-card"><div class="num">{sync_pairs if not skip_sync else '—'}</div><div class="label">左右配对组{' (已跳过)' if skip_sync else ''}</div></div>
  <div class="sum-card"><div class="num">{sync_issues if not skip_sync else '—'}</div><div class="label">同步异常{' (已跳过)' if skip_sync else ''}</div></div>
  <div class="sum-card"><div class="num {'err' if error_count>0 else 'ok'}">{error_count}</div><div class="label">解析错误</div></div>
</div>
""")

    # 全局折叠控制
    html.write('<div style="margin-bottom:16px;">')
    html.write('<button class="toggle-btn" onclick="document.querySelectorAll(\'.folder\').forEach(el=>el.classList.toggle(\'open\'))">展开/折叠 全部</button> ')
    html.write('<button class="toggle-btn" onclick="document.querySelectorAll(\'.folder\').forEach(el=>el.classList.add(\'open\'))">全部展开</button> ')
    html.write('<button class="toggle-btn" onclick="document.querySelectorAll(\'.folder\').forEach(el=>el.classList.remove(\'open\'))">全部折叠</button>')
    html.write('</div>\n')

    # ---- 每个文件夹 ----
    for fr in all_results:
        is_empty = len(fr.streams) == 0 and len(fr.sei_csv_results) == 0
        has_issue = any(s.total_gaps > 0 for s in fr.streams)
        has_sei_issue = any(not r.match for r in fr.sei_csv_results)
        has_count_issue = len(fr.count_mismatch_files) > 0
        has_sync_issue = fr.sync_result is not None and not fr.sync_result.is_synced
        has_imu_sync_issue = any(not ir.is_synced for ir in fr.imu_sync_results)
        has_error = any(s.error for s in fr.streams) or any(r.error for r in fr.sei_csv_results)
        show_fail = (is_empty or has_issue or has_error or has_sync_issue
                     or has_imu_sync_issue or has_sei_issue or has_count_issue)
        folder_open = " open" if show_fail else ""

        html.write(f"""
<div class="folder{folder_open}">
<div class="folder-header" onclick="this.parentElement.classList.toggle('open')">
  <div>
    <span class="folder-name">{fr.folder_name}</span>
    {_badge(not show_fail)}
    <span style="font-size:12px;color:var(--muted);margin-left:8px;">{fr.folder_path}</span>
  </div>
  <div class="folder-stats">
    <span>{len(fr.sei_csv_results)} MP4</span>
    <span>SEI/CSV: {sum(1 for r in fr.sei_csv_results if r.match)}✓ / {sum(1 for r in fr.sei_csv_results if not r.match)}✗</span>
    <span>丢帧: {sum(s.total_gaps for s in fr.streams)}</span>
    {"<span>同步: ✗</span>" if has_sync_issue else ("<span>同步: ✓</span>" if fr.sync_result else "")}
    {"<span>IMU同步: ✗</span>" if has_imu_sync_issue else ("<span>IMU同步: ✓</span>" if fr.imu_sync_results else "")}
  </div>
</div>
<div class="folder-body">
""")

        # ---- SEI vs CSV 比对结果 ----
        if fr.sei_csv_results:
            html.write('<h3>📋 SEI 时间戳 vs CSV 比对</h3>')
            for scr in fr.sei_csv_results:
                if scr.error and not scr.sei_timestamps:
                    html.write(f"""
<div class="section-box mismatch">
  <p><strong>{scr.mp4_name}</strong></p>
  <p class="error-msg">错误: {scr.error}</p>
</div>""")
                    continue

                if not scr.count_match:
                    # 数量不一致
                    border = "count-mismatch"
                    status_icon = "⚠"
                    status_text = f"数量不一致 — SEI: {scr.sei_count} 帧, CSV: {scr.csv_count} 帧"
                    status_color = "var(--orange)"
                elif scr.match:
                    border = "match"
                    status_icon = "✓"
                    status_text = f"完全一致 — SEI: {scr.sei_count} 帧, CSV: {scr.csv_count} 帧"
                    status_color = "var(--green)"
                else:
                    border = "mismatch"
                    status_icon = "✗"
                    status_text = f"时间戳不一致 — SEI: {scr.sei_count} 帧, CSV: {scr.csv_count} 帧"
                    status_color = "var(--red)"

                html.write(f"""
<div class="section-box {border}">
  <p style="font-size:15px;"><strong>{status_icon} {scr.mp4_name}</strong></p>
  <p style="color:{status_color}; margin:8px 0;"><strong>{status_text}</strong></p>
  <table style="margin-bottom:8px;">
    <tr><td style="width:120px;color:var(--muted);">编码类型</td><td>{scr.codec}</td></tr>
    <tr><td style="color:var(--muted);">SEI 数量</td><td><strong>{scr.sei_count}</strong></td></tr>
    <tr><td style="color:var(--muted);">CSV 数量</td><td><strong>{scr.csv_count if scr.csv_path else '无 CSV'}</strong></td></tr>
""")
                if scr.mismatches:
                    csv_not_sei = [m for m in scr.mismatches if m.table == "csv_not_in_sei"]
                    sei_not_csv = [m for m in scr.mismatches if m.table == "sei_not_in_csv"]
                    if csv_not_sei:
                        html.write(f"""
  <details class="mismatch-table" open>
    <summary>CSV 中未找到对应 SEI 的时间戳（共 {len(csv_not_sei)} 条）</summary>
    <table><thead><tr><th>#</th><th>CSV 时间戳</th><th>最接近 SEI</th><th>偏差(µs)</th></tr></thead><tbody>
""")
                        for mi, m in enumerate(csv_not_sei[:100]):
                            html.write(f"""<tr><td>{mi+1}</td><td style="font-size:11px;">{_ts_to_str(m.source_val)}</td><td style="font-size:11px;">{_ts_to_str(m.closest_val)}</td><td>{m.deviation_us:+,}</td></tr>""")
                        if len(csv_not_sei) > 100:
                            html.write(f'<tr><td colspan="4" style="color:var(--muted);text-align:center;">... 还有 {len(csv_not_sei)-100} 条</td></tr>')
                        html.write("</tbody></table></details>\n")
                    if sei_not_csv:
                        html.write(f"""
  <details class="mismatch-table" open>
    <summary>SEI 中未找到对应 CSV 的时间戳（共 {len(sei_not_csv)} 条）</summary>
    <table><thead><tr><th>#</th><th>SEI 时间戳</th><th>最接近 CSV</th><th>偏差(µs)</th></tr></thead><tbody>
""")
                        for mi, m in enumerate(sei_not_csv[:100]):
                            html.write(f"""<tr><td>{mi+1}</td><td style="font-size:11px;">{_ts_to_str(m.source_val)}</td><td style="font-size:11px;">{_ts_to_str(m.closest_val)}</td><td>{m.deviation_us:+,}</td></tr>""")
                        if len(sei_not_csv) > 100:
                            html.write(f'<tr><td colspan="4" style="color:var(--muted);text-align:center;">... 还有 {len(sei_not_csv)-100} 条</td></tr>')
                        html.write("</tbody></table></details>\n")
                if scr.error:
                    html.write(f'<p class="error-msg">错误: {scr.error}</p>')
                html.write("</div>\n")

        # ---- 丢帧检测结果 ----
        if fr.streams:
            video_streams = [s for s in fr.streams if s.source_type in ("mp4_video", "mcap_video", "video_pts")]
            if video_streams:
                html.write('<h3>🎬 丢帧检测</h3>')
                html.write('<div style="overflow-x:auto;">')
                html.write("""<table>
<thead><tr><th>数据源</th><th>类型</th><th>总帧数</th><th>预期速率</th><th>时长</th><th>实际速率</th><th>SEI提取</th><th>丢帧事件</th><th>丢失帧</th><th>丢帧率</th><th>状态</th></tr></thead><tbody>
""")
                for s in video_streams:
                    if s.error:
                        html.write(f"<tr><td>{s.source_name}</td><td colspan='10' class='error-msg'>错误: {s.error}</td></tr>")
                        continue
                    rate_str = f"{s.expected_rate_hz:.1f} Hz"
                    gap_rate = s.total_lost_frames / max(1, s.total_frames) * 100
                    type_tag = "mp4" if s.source_type == "mp4_video" else ("mcap" if s.source_type == "mcap_video" else "pts")
                    html.write(f"""<tr>
  <td><strong>{s.source_name}</strong></td>
  <td><span class="type-tag {type_tag}">{s.source_type}{' (' + s.codec + ')' if s.codec else ''}</span></td>
  <td>{s.total_frames:,}</td>
  <td>{rate_str}</td>
  <td>{s.duration_s:.3f}s</td>
  <td>{s.actual_rate_hz:.2f} Hz</td>
  <td>{s.sei_extracted}/{s.total_frames}</td>
  <td style="color:{'var(--red)' if s.total_gaps>0 else 'var(--green)'};">{s.total_gaps}</td>
  <td style="color:{'var(--red)' if s.total_lost_frames>0 else 'var(--green)'};">{s.total_lost_frames}</td>
  <td>{_progress_bar(gap_rate/100)} {gap_rate:.4f}%</td>
  <td>{_badge(s.total_gaps==0)}</td>
</tr>""")
                html.write("</tbody></table>\n")
                html.write('</div>')  # 关闭 overflow-x:auto
                # 丢帧详情
                for s in video_streams:
                    if s.gaps:
                        gap_rate = s.total_lost_frames / max(1, s.total_frames) * 100
                        html.write(f"""
<div class="gap-detail">
  <details>
    <summary>丢帧详情: {s.source_name} — {len(s.gaps)} 处丢帧, 共丢失 {s.total_lost_frames} 帧 ({gap_rate:.4f}%)</summary>
    <table>
      <thead><tr><th>上一帧#</th><th>本帧#</th><th>上一帧时间戳</th><th>本帧时间戳</th><th>间隔(µs)</th><th>预期间隔(µs)</th><th>估计丢帧</th></tr></thead><tbody>
""")
                        for g in s.gaps[:100]:
                            html.write(f"""<tr>
  <td style="color:var(--orange);">#{g.prev_frame_index}</td>
  <td style="color:var(--red);">#{g.curr_frame_index}</td>
  <td style="font-size:11px;">{_ts_to_str(g.prev_timestamp_us)}</td>
  <td style="font-size:11px;">{_ts_to_str(g.curr_timestamp_us)}</td>
  <td>{g.gap_us:,}</td><td>{g.expected_interval_us:,}</td>
  <td style="color:var(--red)">{g.estimated_lost_frames}</td>
</tr>""")
                        if len(s.gaps) > 100:
                            html.write(f'<tr><td colspan="7" style="color:var(--muted);text-align:center;">... 还有 {len(s.gaps)-100} 条</td></tr>')
                        html.write("</tbody></table></details></div>\n")

            # IMU 流
            imu_streams = [s for s in fr.streams if s.source_type in ("imu_accel", "imu_gyro")]
            if imu_streams:
                html.write('<h3>📡 IMU 数据</h3><div style="overflow-x:auto;"><table>')
                html.write('<thead><tr><th>数据源</th><th>类型</th><th>总帧数</th><th>预期速率</th><th>时长</th><th>丢帧事件</th><th>丢失帧</th><th>状态</th></tr></thead><tbody>')
                for s in imu_streams:
                    html.write(f"""<tr>
  <td>{s.source_name}</td><td><span class="type-tag imu">{s.source_type}</span></td>
  <td>{s.total_frames:,}</td><td>{s.expected_rate_hz:.0f} Hz</td><td>{s.duration_s:.3f}s</td>
  <td style="color:{'var(--red)' if s.total_gaps>0 else 'var(--green)'};">{s.total_gaps}</td>
  <td style="color:{'var(--red)' if s.total_lost_frames>0 else 'var(--green)'};">{s.total_lost_frames}</td>
  <td>{_badge(s.total_gaps==0)}</td></tr>""")
                html.write('</tbody></table></div>\n')
                # IMU 丢帧详情
                for s in imu_streams:
                    if s.gaps:
                        gap_rate = s.total_lost_frames / max(1, s.total_frames) * 100
                        html.write(f"""
<div class="gap-detail">
  <details>
    <summary>丢帧详情: {s.source_name} — {len(s.gaps)} 处丢帧, 共丢失 {s.total_lost_frames} 帧 ({gap_rate:.4f}%)</summary>
    <table>
      <thead><tr><th>上一帧#</th><th>本帧#</th><th>上一帧时间戳</th><th>本帧时间戳</th><th>间隔(µs)</th><th>预期间隔(µs)</th><th>估计丢帧</th></tr></thead><tbody>
""")
                        for g in s.gaps[:100]:
                            html.write(f"""<tr>
  <td style="color:var(--orange);">#{g.prev_frame_index}</td>
  <td style="color:var(--red);">#{g.curr_frame_index}</td>
  <td style="font-size:11px;">{_ts_to_str(g.prev_timestamp_us)}</td>
  <td style="font-size:11px;">{_ts_to_str(g.curr_timestamp_us)}</td>
  <td>{g.gap_us:,}</td><td>{g.expected_interval_us:,}</td>
  <td style="color:var(--red)">{g.estimated_lost_frames}</td>
</tr>""")
                        if len(s.gaps) > 100:
                            html.write(f'<tr><td colspan="7" style="color:var(--muted);text-align:center;">... 还有 {len(s.gaps)-100} 条</td></tr>')
                        html.write("</tbody></table></details></div>\n")
        else:
            html.write('<p style="padding:16px;color:var(--muted);">该文件夹中无丢帧检测数据</p>')

        # ---- 左右同步检测 ----
        if fr.sync_result:
            sr = fr.sync_result
            is_ok = sr.is_synced
            border = "match" if is_ok else "mismatch"
            status_text = "✓ 正常 — 左右 Color 时间戳全部同步" if is_ok else "✗ 异常 — 左右 Color 时间戳未同步"
            status_color = "var(--green)" if is_ok else "var(--red)"

            html.write(f"""
<div class="section-box {border}">
  <h3>📷 左右 Color 时间戳同步检测</h3>
  <p style="font-size:15px;color:{status_color};"><strong>{status_text}</strong></p>
  <table style="margin:12px 0;">
    <tr><td style="width:140px;color:var(--muted);">左 Color 源</td><td><strong>{sr.left_name}</strong>（{sr.total_left} 帧）</td></tr>
    <tr><td style="color:var(--muted);">右 Color 源</td><td><strong>{sr.right_name}</strong>（{sr.total_right} 帧）</td></tr>
    <tr><td style="color:var(--muted);">同步阈值</td><td><strong>{sr.threshold_us:.0f} µs</strong>（帧间隔 / 2）</td></tr>
    <tr><td style="color:var(--muted);">成功匹配</td><td><strong style="color:{status_color};">{sr.matched_pairs} 对</strong></td></tr>
    <tr><td style="color:var(--muted);">平均偏差</td><td>{sr.avg_diff_us:.1f} µs &nbsp;|&nbsp; 最大偏差: {sr.max_diff_us} µs</td></tr>
  </table>
""")
            if not is_ok and sr.unpaired:
                left_unpaired = [u for u in sr.unpaired if u.side == "left_only"]
                right_unpaired = [u for u in sr.unpaired if u.side == "right_only"]
                html.write('<p style="color:var(--red);margin-top:12px;">⚠ 以下帧无法配对：</p>')
                if left_unpaired:
                    html.write(f"""
  <details class="mismatch-table" open>
    <summary>左 Color 不能配对的帧（共 {len(left_unpaired)} 帧），输出最接近右帧</summary>
    <table><thead><tr><th>#</th><th>帧序号</th><th>时间戳</th><th>最接近右帧</th><th>偏差(µs)</th></tr></thead><tbody>
""")
                    for ui, u in enumerate(left_unpaired[:100]):
                        ts = u.left_ts_us; cts = u.closest_other_ts_us
                        diff = abs(cts - ts) if cts else 0
                        html.write(f"""<tr><td>{ui+1}</td><td style="color:var(--orange);">#{u.left_idx}</td><td style="font-size:11px;">{_ts_to_str(ts)}</td><td style="font-size:11px;">{_ts_to_str(cts) if cts else '—'}</td><td>{diff if cts else '—'}</td></tr>""")
                    if len(left_unpaired) > 100:
                        html.write(f'<tr><td colspan="5" style="color:var(--muted);">... 还有 {len(left_unpaired)-100} 条</td></tr>')
                    html.write("</tbody></table></details>\n")
                if right_unpaired:
                    html.write(f"""
  <details class="mismatch-table" open>
    <summary>右 Color 不能配对的帧（共 {len(right_unpaired)} 帧），输出最接近左帧</summary>
    <table><thead><tr><th>#</th><th>帧序号</th><th>时间戳</th><th>最接近左帧</th><th>偏差(µs)</th></tr></thead><tbody>
""")
                    for ui, u in enumerate(right_unpaired[:100]):
                        ts = u.right_ts_us; cts = u.closest_other_ts_us
                        diff = abs(cts - ts) if cts else 0
                        html.write(f"""<tr><td>{ui+1}</td><td style="color:var(--orange);">#{u.right_idx}</td><td style="font-size:11px;">{_ts_to_str(ts)}</td><td style="font-size:11px;">{_ts_to_str(cts) if cts else '—'}</td><td>{diff if cts else '—'}</td></tr>""")
                    if len(right_unpaired) > 100:
                        html.write(f'<tr><td colspan="5" style="color:var(--muted);">... 还有 {len(right_unpaired)-100} 条</td></tr>')
                    html.write("</tbody></table></details>\n")
            html.write("</div>\n")

        # ---- IMU 与视频同步检测 ----
        if fr.imu_sync_results:
            for imu_r in fr.imu_sync_results:
                border = "#4caf50" if imu_r.is_synced else "#f44336"
                status_text = "✓ IMU 与视频时间戳同步正常" if imu_r.is_synced else "✗ IMU 与视频时间戳未完全通过同步检测"
                status_color = "var(--green)" if imu_r.is_synced else "var(--red)"
                html.write(f"""
<div class="section-box" style="border:1px solid {border}; margin:16px 0; padding:20px; overflow-x:auto;">
  <h3>📡 IMU 与视频时间戳同步检测</h3>
  <p style="font-size:15px;color:{status_color};"><strong>{status_text}</strong></p>
  <table style="margin:12px 0;">
    <tr><td style="width:140px;color:var(--muted);">IMU 数据源</td><td><strong>{imu_r.imu_name}</strong>（{imu_r.imu_total:,} 帧）</td></tr>
    <tr><td style="color:var(--muted);">视频数据源</td><td><strong>{imu_r.video_name}</strong>（{imu_r.video_total:,} 帧）</td></tr>
    <tr><td style="color:var(--muted);">同步阈值</td><td><strong>{imu_r.threshold_us:.0f} µs</strong></td></tr>
    <tr><td style="color:var(--muted);">平均时间偏移</td><td><strong style="color:{status_color};">{imu_r.avg_offset_us:.0f} µs</strong></td></tr>
    <tr><td style="color:var(--muted);">最大时间偏移</td><td>{imu_r.max_offset_us:,} µs</td></tr>
    <tr><td style="color:var(--muted);">最小时间偏移</td><td>{imu_r.min_offset_us:,} µs</td></tr>
  </table>
""")
                # 不同步时输出超标时间戳详情
                if not imu_r.is_synced and imu_r.outliers:
                    html.write(
                        f'<details class="mismatch-table" open>'
                        f'<summary>⚠ 偏移超标采样点（共 {len(imu_r.outliers)} 处，'
                        f'阈值 {imu_r.threshold_us:.0f} µs）</summary>'
                        f'<div style="overflow-x:auto;"><table>'
                        f'<thead><tr><th>#</th><th>视频时间戳</th>'
                        f'<th>最接近 IMU 时间戳</th><th>偏移(µs)</th></tr></thead><tbody>'
                    )
                    for oi, o in enumerate(imu_r.outliers[:100]):
                        html.write(
                            f"<tr><td>{oi + 1}</td>"
                            f"<td style=\"font-size:11px;\">{_ts_to_str(o.video_ts_us)}</td>"
                            f"<td style=\"font-size:11px;\">{_ts_to_str(o.imu_ts_us)}</td>"
                            f"<td style=\"color:var(--red);\">{o.offset_us:,}</td></tr>"
                        )
                    if len(imu_r.outliers) > 100:
                        html.write(
                            f'<tr><td colspan="4" style="color:var(--muted);text-align:center;">'
                            f'... 还有 {len(imu_r.outliers) - 100} 条</td></tr>'
                        )
                    html.write("</tbody></table></div></details>")
                html.write("</div>\n")

        html.write("</div></div>\n")  # folder-body, folder

    html.write(f"""
<footer>
  由 video_analyzer.py 生成 &nbsp;|&nbsp; {now_str} &nbsp;|&nbsp;
  扫描路径: {root_path} &nbsp;|&nbsp;
  共 {total_folders} 个文件夹, {total_mp4} 个 MP4, {total_streams} 个数据流
</footer>
</div>
<script>
document.querySelectorAll('.folder').forEach(el => {{
  const hasFail = el.querySelector('.badge.fail');
  if (hasFail) el.classList.add('open');
}});
</script>
</body>
</html>
""")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html.getvalue())
    print(f"\nHTML 报告已保存: {output_path}")
    return {
        "total_folders": total_folders, "total_mp4": total_mp4,
        "total_sei_csv_match": total_sei_csv_match, "total_sei_csv_mismatch": total_sei_csv_mismatch,
        "total_count_mismatch": total_count_mismatch,
        "total_streams": total_streams, "total_gaps": total_gaps,
        "total_lost_frames": total_lost,
        "sync_pairs": sync_pairs, "sync_issues": sync_issues,
        "error_count": error_count,
    }


# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="综合视频分析工具 — SEI 提取 & CSV 比对 + 丢帧检测 + 左右同步检测",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python video_analyzer.py /data/recordings
  python video_analyzer.py . --threshold 2.0
  python video_analyzer.py /data --output my_report.html --no-frame-dump

依赖:
  ffmpeg / ffprobe  (必需)
  Python 3.8+
  numpy             (可选 — pip install numpy, 加速数值计算)
  mcap 库           (可选 — pip install mcap mcap-ros2-support)
        """,
    )
    parser.add_argument("root", help="根目录路径 (递归扫描子文件夹)")
    parser.add_argument("--threshold", type=float, default=1.5,
                        help="丢帧判定阈值, 为预期帧间隔的倍数 (默认: 1.5)")
    parser.add_argument("--output", "-o", default=None,
                        help="输出 HTML 文件路径 (默认: <root>/video_analysis_report.html)")
    parser.add_argument("--no-frame-dump", action="store_true",
                        help="不截取帧对图片")
    parser.add_argument("--sync-check", action="store_true", default=None,
                        help="强制进行左右 RGB 同步检测")
    parser.add_argument("--no-sync-check", action="store_true", default=None,
                        help="跳过左右 RGB 同步检测 (加速分析)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="打印详细处理过程")

    args = parser.parse_args()

    # ---- 交互式选择: 左右 RGB 同步检测 ----
    if args.sync_check is True and args.no_sync_check is True:
        print("错误: --sync-check 和 --no-sync-check 不能同时使用", file=sys.stderr)
        sys.exit(1)
    if args.sync_check is None and args.no_sync_check is None:
        # 两个都没指定, 交互式询问
        print()
        ans = input("  是否进行左右 RGB 同步检测? [Y/n]: ").strip().lower()
        skip_sync = ans in ("n", "no")
        print()
    else:
        skip_sync = args.no_sync_check
    root_path = Path(args.root).resolve()
    if not root_path.exists():
        print(f"错误: 路径不存在 — {root_path}", file=sys.stderr)
        sys.exit(1)

    output_path = args.output or str(root_path / "video_analysis_report.html")

    print("=" * 70)
    print("  综合视频分析工具 (跨平台版)")
    print(f"  输入目录: {root_path}")
    print(f"  输出报告: {output_path}")
    print("=" * 70)

    # 检查必需依赖
    missing_deps = []
    for dep in ("ffmpeg", "ffprobe"):
        if not shutil.which(dep):
            missing_deps.append(dep)
    if missing_deps:
        print(f"错误: 缺少必需依赖: {', '.join(missing_deps)}", file=sys.stderr)
        if sys.platform == "win32":
            print("请安装 ffmpeg: https://ffmpeg.org/download.html", file=sys.stderr)
        else:
            print("请安装: sudo apt install ffmpeg  或  brew install ffmpeg", file=sys.stderr)
        sys.exit(1)

    # 检查可选依赖
    try:
        import mcap  # noqa: F401
    except ImportError:
        print("注意: mcap 库不可用, MCAP 文件将跳过分析")
        print("  安装: pip install mcap mcap-ros2-support")

    # 扫描 & 分析
    try:
        results = scan_and_analyze(str(root_path), args.threshold,
                                   verbose=args.verbose,
                                   skip_sync=skip_sync)
    except RuntimeError as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)

    # ---- 跨文件夹 IMU 后处理: 丢帧检测 + 全局同步重检 ----
    print("\n" + "=" * 70)
    print("  跨文件夹 IMU 后处理 (全局丢帧 + 同步重检)")
    print("=" * 70)
    post_process_imu_global(results, args.threshold)

    # 帧对截取 (MCAP 无需截取; 跳过同步检测时也不截取)
    if not args.no_frame_dump and not skip_sync:
        dump_root = str(Path(output_path).parent / "frame_dumps")
        for fr in results:
            if fr.sync_result is not None:
                is_mcap_sync = ".mcap" in fr.sync_result.left_name.lower() or ".mcap" in fr.sync_result.right_name.lower()
                if is_mcap_sync:
                    print(f"\n  跳过帧截取 (MCAP): {fr.folder_name}...", flush=True)
                    continue
                print(f"\n  截取帧对: {fr.folder_name}...", flush=True)
                dump_frame_pairs(fr.sync_result, fr.folder_path,
                                 f"{dump_root}/{fr.folder_name}",
                                 random_synced=True, do_extract=True)

    # 生成 HTML
    stats = generate_html(results, output_path, str(root_path),
                          skip_sync=skip_sync)

    # 终端摘要
    print()
    print("=" * 70)
    print("  分析摘要")
    print("=" * 70)
    print(f"  文件夹: {stats['total_folders']}")
    print(f"  MP4 文件: {stats['total_mp4']}")
    print(f"  SEI/CSV 一致: {stats['total_sei_csv_match']}")
    print(f"  SEI/CSV 不一致: {stats['total_sei_csv_mismatch']}")
    print(f"  SEI/CSV 数量不同: {stats['total_count_mismatch']}")
    print(f"  数据流: {stats['total_streams']}")
    print(f"  丢帧事件: {stats['total_gaps']} 处, 估计丢失 {stats['total_lost_frames']} 帧")
    if skip_sync:
        print(f"  同步: 已跳过")
    elif stats.get('sync_pairs', 0) > 0:
        print(f"  同步: {stats['sync_pairs']} 对立体配对, {stats['sync_issues']} 对异常")
    if stats['error_count'] > 0:
        print(f"  错误: {stats['error_count']} 个数据流解析失败")
    print("=" * 70)


if __name__ == "__main__":
    main()
