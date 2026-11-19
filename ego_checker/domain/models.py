"""domain.models 的实现。"""
import ego_checker.domain.audio as audio_validation
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Protocol, Union


class SourceRows(Protocol):
    """Read-only timestamp to original source-row lookup."""
    def get(self, timestamp: int, default=None) -> Optional[int]: ...


@dataclass
class CameraData:
    """一路相机的 CSV 时间戳信息."""
    role: str                 # left_rgb / right_rgb / side_left / side_right / cameraN
    label: str                # 显示名 (如 左目RGB)
    file: Path
    timestamps: List[int] = field(default_factory=list)   # 按行序的时间戳
    rows: List[int] = field(default_factory=list)         # 对应的数据行下标 (0 基)
    n_rows: int = 0
    error: str = ""
    retained_count: Optional[int] = None   # 读取完成时记录的原始帧数
    retained_first: Optional[int] = None   # 读取完成时记录的首帧时间戳
    retained_last: Optional[int] = None    # 读取完成时记录的尾帧时间戳

    def record_retained_edges(self) -> None:
        """读取完成后立即记录帧数与首尾时间戳，供释放后报告展示."""
        self.retained_count = len(self.timestamps)
        if self.timestamps:
            self.retained_first = self.timestamps[0]
            self.retained_last = self.timestamps[-1]

    @property
    def count(self) -> int:
        """帧数；原始时间戳已释放时取读取时记录的值."""
        if self.retained_count is not None:
            return self.retained_count
        return len(self.timestamps)


@dataclass
class AbnormalFrame:
    """一条异常帧详情 (CSV 行号 = 表格行号 + 1, 首行为表头)."""
    csv_line: int            # CSV 物理行号 (首行为表头 => 第 1 行; 数据从第 2 行起)
    row_index: int           # 数据行下标 (0 基)
    value_us: int            # 该行对应的差值 / 间隔 (µs)
    threshold_us: int        # 判定阈值 (µs)
    detail: str = ""         # 附加说明 (如 哪两路相机 / 参考目标)
    ts_a: Optional[int] = None   # 左端点时间戳 (行号 i / 首帧 / 基准相机) µs
    ts_b: Optional[int] = None   # 右端点时间戳 (行号 i+1 / 尾帧 / 最近邻或IMU) µs
    lost: Optional[int] = None   # 丢帧数 (仅 RGB/IMU 丢帧)
    cam: str = ""                # 相机角色 (仅 RGB 丢帧 / SEI/跨段对齐)
    cam_label: str = ""          # 相机显示名（跨段匹配保留角色与显示名）
    cam_ts: Dict[str, int] = field(default_factory=dict)  # 同行各目相机时间戳 (仅同行对齐)
    b_role: str = ""             # 对端相机角色 (cam_sync: 最近邻来自哪路相机)
    b_label: str = ""            # 对端相机标签
    b_row_index: Optional[int] = None   # 对端数据行下标 (cam_sync: 对应相机行号; imu_sync: IMU 行号)
    b_csv_line: Optional[int] = None    # 对端 CSV 物理行号
    anomaly_type: str = ""             # 异常类型 (如丢帧/倒退)


@dataclass
class FrameCountResult:
    """相机帧数对齐检查结果."""
    count_diff: int = 0              # 相机间最大帧数差值
    first_row_diff_us: int = 0       # 首行时间戳最大差值
    last_row_diff_us: int = 0        # 尾行时间戳最大差值
    counts: Dict[str, int] = field(default_factory=dict)
    passed: bool = False
    abnormal: List[AbnormalFrame] = field(default_factory=list)
    detail: str = ""


@dataclass
class IntervalResult:
    """单目相机帧间隔检查结果."""
    role: str = ""
    label: str = ""
    expected_us: float = 0.0
    max_interval_us: int = 0
    mean_interval_us: float = 0.0
    min_interval_us: int = 0
    max_dev_us: int = 0              # 偏差 dev 的最大值 (dev=|interval-expected|)
    mean_dev_us: float = 0.0         # 偏差 dev 的平均值
    over_count: int = 0              # |interval-expected| >= 10ms 的帧数
    abnormal: List[AbnormalFrame] = field(default_factory=list)
    passed: bool = False
    detail: str = ""


@dataclass
class RowAlignResult:
    """同行时间戳对齐检查结果."""
    max_diff_us: int = 0
    mean_diff_us: float = 0.0
    over_count: int = 0
    abnormal: List[AbnormalFrame] = field(default_factory=list)
    passed: bool = False
    detail: str = ""


@dataclass
class SyncResult:
    """相机/IMU/夹爪时间戳同步检查结果 (某目相机为基准)."""
    base_role: str = ""
    base_label: str = ""
    threshold_us: float = 0
    total_frames: int = 0
    matched_frames: int = 0
    over_count: int = 0              # 超阈帧数
    max_diff_us: int = 0
    mean_diff_us: float = 0.0
    p95_diff_us: int = 0
    p50_diff_us: int = 0
    context_hits: int = 0          # 实际命中的相邻分段参考帧数
    context_matches: List[AbnormalFrame] = field(default_factory=list)  # 实际选中的跨段对齐记录（可为正常帧）
    abnormal: List[AbnormalFrame] = field(default_factory=list)
    passed: bool = False             # release0820_align_v2: 0 超阈 → PASS
    mode: str = "camera"             # camera / imu / grip
    detail: str = ""
    frequency_hz: Optional[float] = None
    frequency_source: str = ""
    # Only failed edge queries need a second lookup. Counts retain exact local
    # statistics without keeping another full per-frame array.
    edge_queries: list = field(default_factory=list, repr=False, compare=False)
    diff_counts: dict = field(default_factory=dict, repr=False, compare=False)
    local_result: Optional["SyncResult"] = field(default=None, repr=False, compare=False)

    @property
    def over_pct(self) -> float:
        """超阈帧占比 (%)."""
        return self.over_count / max(1, self.matched_frames) * 100


@dataclass
class DropResult:
    """RGB / IMU 丢帧检查结果."""
    role: str = ""
    label: str = ""
    threshold_us: float = 0.0
    total_gaps: int = 0
    total_lost_frames: int = 0
    backstep_count: int = 0
    duplicate_count: int = 0
    max_gap_us: int = 0
    mean_gap_us: float = 0.0
    abnormal: List[AbnormalFrame] = field(default_factory=list)
    passed: bool = False             # max < 阈值 → PASS (max 峰值分档)
    total_frames: int = 0            # 该流总帧数 (IMU/音频/夹爪 丢帧详情显示用)
    detail: str = ""


@dataclass
class PartResult:
    """一个 part (录制段) 的完整分析结果 — 结果 CSV 一行."""
    name: str
    path: Path
    data_folder: str = ""          # 报告「数据文件夹名称」显示用 (part 文件上层文件夹; 多设备会话=会话根目录名)
    cameras: Dict[str, CameraData] = field(default_factory=dict)
    imu: Optional[List[int]] = None
    # 同步详情回溯：时间戳 -> 原始源数据行下标（0 基）。CSV 中同一时刻的
    # accel/gyro 成对记录取首次出现行；来源不唯一时不填，避免伪造行号。
    imu_row_by_timestamp: SourceRows = field(default_factory=dict)
    # 同步匹配继续使用上面的排序去重索引；丢帧/倒退检查按原始文件或 MCAP 通道顺序执行。
    imu_drop_sources: List[List[int]] = field(default_factory=list)
    imu_error: str = ""
    imu_frequency_hz: Optional[float] = None
    imu_frequency_source: str = ""
    imu_frequency_error: str = ""
    imu_frequency_hz_by_device: Dict[str, float] = field(default_factory=dict)
    imu_frequency_source_by_device: Dict[str, str] = field(default_factory=dict)
    imu_frequency_error_by_device: Dict[str, str] = field(default_factory=dict)
    imu_frequency_profiles: List[Dict] = field(default_factory=list)
    imu_frequency_audit: List[Dict] = field(default_factory=list)
    imu_frequency_validation_errors: List[str] = field(default_factory=list)
    grip: Optional[List[int]] = None            # 夹爪(grip)时间戳流
    # 夹爪连续性按文件/MCAP 通道保留原始记录顺序；排序去重后的 grip 仅作稳定索引。
    grip_drop_sources: List[List[int]] = field(default_factory=list)
    grip_drop: Optional["DropResult"] = None    # grip 丢帧结果
    grip_by_device: Dict[str, List[int]] = field(default_factory=dict)  # 多设备会话按设备隔离夹爪时钟
    grip_drop_sources_by_device: Dict[str, List[List[int]]] = field(default_factory=dict)
    grip_drop_by_device: Dict[str, "DropResult"] = field(default_factory=dict)
    grip_sync: Dict[str, SyncResult] = field(default_factory=dict)  # 相机角色 -> 所属设备夹爪同步结果
    grip_frequency_hz: Optional[float] = None
    grip_frequency_source: str = ""
    grip_frequency_error: str = ""
    grip_frequency_hz_by_device: Dict[str, float] = field(default_factory=dict)
    grip_frequency_source_by_device: Dict[str, str] = field(default_factory=dict)
    grip_frequency_error_by_device: Dict[str, str] = field(default_factory=dict)
    multi_device: bool = False                                  # 多设备会话禁止回退到合并 IMU 池
    meta_integrity: List = field(default_factory=list)  # meta 完整性/准确性检测项（MetaIssue），独立于传感器判定
    session_info: Dict = field(default_factory=dict)  # 识别依据、元数据来源及冲突警告；不修改传感器判定
    notices: List[str] = field(default_factory=list)  # 非错误分析提示，不参与PASS/FAIL
    camera_device_by_role: Dict[str, str] = field(default_factory=dict)  # 相机角色 -> 所属设备键
    video_duration_by_role: Dict[str, float] = field(default_factory=dict)  # 实际视频/MCAP流时长(s)
    video_duration_error_by_role: Dict[str, str] = field(default_factory=dict)
    audio: Optional[List[int]] = None           # 原始音频时间戳，仅供显示；不合并时间轴做丢帧检查
    audio_drop: Optional["DropResult"] = None   # 旧接口保留，新 PCM 校验不填写此值
    audio_evidence: List[audio_validation.AudioEvidence] = field(default_factory=list)
    audio_enabled: bool = True                  # 用户是否启用音频测试；关闭时报告不显示音频列
    imu_order_valid: bool = True
    # streamed 表示边界样本已固化；释放标记单独维护，避免重复释放覆盖报告统计。
    boundary_samples: Dict = field(default_factory=dict)
    frequency_cache: Dict = field(default_factory=dict)   # 配置帧率解析缓存，按 part 隔离
    streamed: bool = False
    _data_released: bool = field(default=False, repr=False, compare=False)
    boundary_truncation_errors: List[str] = field(default_factory=list)  # 超固定样本数的跨段详情
    # MCAP 相机角色 -> 字段1与SEI时间戳核对结果（总数/缺失/超容差/样本），
    # 供SEI对齐报告展示；不参与同步、丢帧等分析计算。
    sei_align_audit: Dict[str, Dict] = field(default_factory=dict)
    imu_by_device: Dict[str, List[int]] = field(default_factory=dict)   # 设备名 -> IMU 时间戳
    imu_row_by_timestamp_by_device: Dict[str, SourceRows] = field(default_factory=dict)
    imu_drop_sources_by_device: Dict[str, List[List[int]]] = field(default_factory=dict)
    imu_drop_by_device: Dict[str, DropResult] = field(default_factory=dict)  # 设备名 -> 丢帧结果
    imu_groups_diff: Optional["SyncResult"] = None   # 多组 IMU 差值 (max/mean)
    frame_count: FrameCountResult = field(default_factory=FrameCountResult)
    frame_count_groups: Dict[str, FrameCountResult] = field(default_factory=dict)
    # 多设备会话按设备各自的相机组计算帧数对齐/同行对齐；单目设备不参与。
    frame_count_by_device: Dict[str, FrameCountResult] = field(default_factory=dict)
    row_align_by_device: Dict[str, RowAlignResult] = field(default_factory=dict)
    frame_count_skipped_devices: Dict[str, str] = field(default_factory=dict)  # 设备 -> 跳过原因
    intervals: Dict[str, IntervalResult] = field(default_factory=dict)
    row_align: RowAlignResult = field(default_factory=RowAlignResult)
    cam_sync: Dict[str, SyncResult] = field(default_factory=dict)
    cam_sync_groups: Dict[str, Dict[str, SyncResult]] = field(default_factory=dict)
    cam_sync_grouped: bool = False   # 是否用了「同步组」口径 (深度产品)
    depth_sync_enabled: bool = True  # Depth 是否参与相机间同步；关闭后 Depth RGB 仅比较鱼眼
    depth_sync_threshold_us: int = 5000  # 本段已生效的深度共用同步阈值，供跨段补偿复用
    cam_sync_context: Dict[str, "CamBoundarySample"] = field(default_factory=dict)  # 相邻分段提供的相机边界参考
    imu_sync: Dict[str, SyncResult] = field(default_factory=dict)
    imu_sync_context: Dict[str, Union["ImuBoundarySample", Dict[str, "ImuBoundarySample"]]] = field(default_factory=dict)  # 单设备按方向索引，多设备按设备/方向索引
    rgb_drop: Dict[str, DropResult] = field(default_factory=dict)
    imu_drop: Optional[DropResult] = None
    duration_s: float = 0.0
    fps: float = 0.0
    firmware: str = ""
    whitelisted: bool = False
    error: str = ""
    required_streams_missing: List[str] = field(default_factory=list)
    # 各检查项 PASS/FAIL 汇总 (供报告与总判定)
    checks: Dict[str, bool] = field(default_factory=dict)
    checks_completed: bool = False  # 独立检查已执行；ERROR 时仍展示实际算出的结果
    source_read_audits: Dict[str, Dict] = field(default_factory=dict, repr=False)
    unavailable_checks: Dict[str, str] = field(default_factory=dict)  # 缺失依赖，不作 PASS/FAIL
    camera_source_errors: List[str] = field(default_factory=list)  # 容器失败，无法确定完整相机集合
    over_frames_by_check: Dict[str, List[AbnormalFrame]] = field(default_factory=dict)

    def unavailable_check_message(self, key: str) -> str:
        reason = self.unavailable_checks.get(key, '')
        if not reason:
            return ''
        label = '因数据读取异常，未执行：'
        if key == 'rgb_drop':
            label = '部分相机数据读取异常：'
        elif any(key == f'interval_{role}' for role, camera in self.cameras.items()
                 if camera.error or not camera.timestamps):
            label = '数据读取异常：'
        elif key.startswith('grip_sync_') and (self.grip_frequency_error
                                               or self.grip_frequency_error_by_device):
            label = '夹爪频率配置错误，未执行：'
        return label + reason

    def invalidate_imu_results(self) -> None:
        """Drop stale derived conclusions after an input error, retaining sources."""
        self.imu_sync.clear()
        self.imu_sync_context.clear()
        self.imu_drop = None
        self.imu_drop_by_device.clear()
        self.imu_groups_diff = None
        for mapping in (self.checks, self.over_frames_by_check):
            for key in list(mapping):
                if key.startswith("imu_"):
                    del mapping[key]

    @property
    def overall_pass(self) -> bool:
        return self.business_status == "PASS"

    @property
    def business_status(self) -> str:
        if (self.error or self.imu_error or self.imu_frequency_error or self.imu_frequency_error_by_device
                or self.grip_frequency_error or self.grip_frequency_error_by_device
                or self.imu_frequency_validation_errors):
            return "ERROR"
        audio = audio_validation.aggregate(self.audio_evidence)
        if any(not v for v in self.checks.values()) or audio == "FAIL":
            return "FAIL"
        if audio == "ERROR":
            return "ERROR"
        # 音频 SUSPECTED/UNVERIFIED 只保留为诊断信息，不参与业务结论。
        return "PASS" if self.checks else "UNVERIFIED"


@dataclass
class ImuBoundarySample:
    """相邻分段提供的 IMU 同步参考; 不计入当前分段的样本数和丢帧检查.

    与相机跨段一致: 保留相邻段边界处若干时间戳 (前段取尾部/后段取头部),
    timestamps 用于“跨段样本来源”展示边界帧数/首帧/尾帧。
    """
    part: str = ""
    source: Optional[Path] = None
    timestamp_us: int = 0
    timestamps_us: List[int] = field(default_factory=list)
    row_indices: List[Optional[int]] = field(default_factory=list)  # 相邻源文件数据行下标（0 基）；未知为 None

    @property
    def timestamps(self) -> List[int]:
        """V0.1.0 展示接口，保留既有 timestamps_us 数据接口。"""
        return self.timestamps_us


@dataclass
class CamBoundarySample:
    """相邻分段提供的「相机同步」对端参考.

    当本段某相机帧落在分段边界、在本段对端相机池中找不到近邻时, 从相邻分段的
    对端相机时间戳里取最近邻作为参考。extra_ts: {对端角色: [相邻分段该角色边界时间戳]}。
    仅作同步参考, 不计入任何分段的帧数/丢帧检查。
    """
    direction: str = ""            # "previous" / "next"
    neighbor_part: str = ""        # 来源分段名
    source: Optional[Path] = None  # 兼容旧调用：相邻分段任一路来源文件
    sources: Dict[str, Path] = field(default_factory=dict)  # 角色 -> 实际来源 mcap/csv
    extra_ts: Dict[str, List[int]] = field(default_factory=dict)  # 对端角色 -> 边界时间戳
    extra_rows: Dict[str, List[int]] = field(default_factory=dict)  # 对端角色 -> 相邻 Part 原始行号
