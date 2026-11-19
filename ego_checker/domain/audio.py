"""domain.audio 的实现。"""
from dataclasses import dataclass, field
from typing import Optional


class CorruptAudio(ValueError):
    pass


@dataclass
class AudioContinuityEvent:
    record_number: int
    current_timestamp_us: int
    next_timestamp_us: int
    packet_frames: object
    expected_next_timestamp_us: int
    deviation_us: float
    affected_frames: object
    anomaly_type: str


@dataclass
class AudioEvidence:
    source: str
    status: str = "UNVERIFIED"
    integrity: str = "UNVERIFIED"
    continuity: str = "UNVERIFIED"
    reason: str = ""
    timestamp_error: str = ""  # 原始CSV末尾诊断；保留原记录，不补齐或猜测时间戳
    timestamps: list = field(default_factory=list)
    system_timestamps: list = field(default_factory=list)
    sizes: list = field(default_factory=list)
    pcm_start_samples: list = field(default_factory=list)
    source_rows: list = field(default_factory=list)
    csv_fields: list = field(default_factory=list)
    csv_records: list = field(default_factory=list)
    # 流式分析释放逐包列表后的存在标记；None 表示未释放，按列表实际内容判断。
    had_timestamps: Optional[bool] = None
    had_sizes: Optional[bool] = None
    retained_packets: Optional[int] = None   # 释放前的逐包记录数，供报告统计使用
    retained_first_row: Optional[int] = None  # 释放前的首条 CSV 物理行号
    retained_last_row: Optional[int] = None   # 释放前的末条 CSV 物理行号
    marker_rows: list = field(default_factory=list)
    rate: int = 0
    channels: int = 0
    bits: int = 0
    data_bytes: int = 0
    sample_frames: int = 0
    non_increasing: int = 0
    duplicate_timestamps: int = 0
    backsteps: int = 0
    interval_min_us: int = 0
    interval_max_us: int = 0
    interval_mean_us: float = 0.0
    interval_median_us: float = 0.0
    gap_count: int = 0
    overlap_count: int = 0
    lost_sample_frames: int = 0
    overlap_sample_frames: int = 0
    max_gap_us: int = 0
    max_timing_error_us: int = 0
    timing_tolerance_min_us: int = 0
    timing_tolerance_max_us: int = 0
    suspected_gap_count: int = 0
    suspected_lost_packets: int = 0
    cluster_count: int = 0
    short_interval_count: int = 0
    continuity_method: str = "timestamp_us"
    timestamp_quality: str = "UNVERIFIED"
    clock_reanchor_count: int = 0
    coverage_status: str = "UNVERIFIED"
    coverage_first_sample: object = None
    coverage_end_sample: object = None
    payload_bytes: int = 0
    payload_byte_difference: int = 0
    payload_tolerance_frames: int = 0
    payload_conservation: str = "UNVERIFIED"
    pcm_max_break_frames: int = 0
    pcm_total_break_frames: int = 0
    pcm_single_break_limit_frames: int = 0
    pcm_total_break_limit_frames: int = 0
    waveform: dict = field(default_factory=dict)
    minimum_duration_us: int = 0
    minimum_duration_status: str = "UNVERIFIED"
    csv_span_us: int = 0
    wav_duration_us: float = 0.0
    duration_difference_us: float = 0.0
    duration_consistency: str = "UNVERIFIED"
    duration_tolerance_us: float = 0.0
    target_duration_s: float = 0.0
    target_duration_difference_us: float = 0.0
    target_duration_tolerance_us: float = 0.0
    target_duration_status: str = "N/A"
    csv_sha256: str = ""
    media_sha256: str = ""
    tolerance_frames: int = 2
    continuity_events: list = field(default_factory=list)
    device: str = ""


def aggregate(evidence):
    for status in ("FAIL", "ERROR", "SUSPECTED", "UNVERIFIED"):
        if any(e.status == status for e in evidence):
            return status
    return "PASS" if any(e.status == "PASS" for e in evidence) else "N/A"
