"""EGO Courier 配置：阈值、字段映射、时区、路径。

全部为纯 Python 标准库依赖，无第三方包。
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# 路径与运行参数
# ---------------------------------------------------------------------------
# 本工具所有数据（原始文件、导出）所在的根目录；随项目可迁移。
DATA_ROOT = Path(__file__).resolve().parent.parent / "data"
DATA_ROOT.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_ROOT / "ego_relay.db"
EXPORT_DIR = DATA_ROOT / "exports"
IMPORT_TMP = DATA_ROOT / "tmp_import"
for _p in (EXPORT_DIR, IMPORT_TMP):
    _p.mkdir(parents=True, exist_ok=True)

DEFAULT_PORT = 8360
DEFAULT_HOST = "127.0.0.1"

# EGOViewer 应用日志的本地时区相对 UTC 偏移（小时）。示例日志为 UTC+8。
VIEWER_TZ_HOURS = 8

# 搜索本机 EGOViewer 安装目录的根路径（找到形如 EGOViewer_*_win_x64 且含 data/logs 的目录）。
EGO_VIEWER_SEARCH_ROOTS = [r"D:\Soft&tools\软件工具", r"D:\Soft&tools", r"D:\file"]


# ---------------------------------------------------------------------------
# 设备连接信息（SSH 远程拉取待拿到真实凭证后再启用）
# 本地导入模式不需要以下三个参数。
#   设备 IP：从 EGOViewer 设备详情 / 路由器 / TTL 串口 ifconfig 获取
#   SSH 用户名/密码：需用户手动输入（暂未知）
# ---------------------------------------------------------------------------
DEVICE_IP = "10.9.82.9"  # 最近一次 BLE 配网拿到的设备 IP；可在界面修改
SSH_USER = ""             # 需用户手动输入
SSH_PASSWORD = ""         # 需用户手动输入
SSH_PORT = 22

# 测试设备示例（用户提供）：wifi=TP-LINK_AC2600_5G  ip=192.168.1.118:8090
EXAMPLE_DEVICE_IP = "10.9.82.9"
EXAMPLE_DEVICE_PORT = 8090   # Orbbec SDK 网络设备端口；SSH 端口仍由 SSH_PORT 控制

# Orbbec SDK firmware-log export. The SDK package can be selected here or via
# ORBBEC_SDK_ROOT; an empty value uses the bundled/default discovery locations.
# 留空：不在界面预填 SDK 路径，由用户在「设备与日志」页手动填写本机 SDK 根目录
# （含 bin\\OrbbecSDK.dll 的目录）。
ORBBEC_SDK_ROOT = ""
ORBBEC_SDK_PORT = 8090


# ---------------------------------------------------------------------------
# BLE 配网（参考 EGOViewer 的 EgoLowBle.dll）
# ---------------------------------------------------------------------------
# EgoLowBle.dll 的显式路径。留空时依次尝试：
#   环境变量 EGOLOWBLE_DLL → 本工具安装目录下 lib/ → 扫描 EGOViewer 安装目录。
# EGOViewer 安装目录形如：
#   D:\\Soft&tools\\软件工具\\EGOViewer_v2.0.10_*_win_x64\\EgoLowBle.dll
EGOLOWBLE_DLL = ""

# 配网协议限制（见 EGOViewer 手册）：Wi-Fi 名称与密码各最多 32 个 UTF-8 字节。
BLE_WIFI_MAX_BYTES = 32
BLE_CONNECT_TIMEOUT_S = 20      # 连接/配网等待超时（秒）
BLE_SCAN_TIMEOUT_S = 5          # 扫描 BLE 设备的等待超时（秒），越短扫描越快
BLE_SCAN_RSSI_MIN = -70         # 扫描时丢弃信号弱于该值(dBm)的设备（距离过远）
BLE_NETWORK_JOIN_TIMEOUT_S = 40 # 配置后等待设备加入局域网并上报 IP 的超时（秒）


# ---------------------------------------------------------------------------
# 异常阈值（单位：微秒 us）。真值基准 = 硬件 PTP hw_ptp_ts（epoch 微秒 UTC）。
# ---------------------------------------------------------------------------
DEFAULT_THRESHOLDS_US = {
    # 软件接收时刻 − 硬件 PTP：应用接收延迟（含传输/驱动/缓存）。>50ms 异常
    "app_receive_delay": 50_000,
    # MP4 SEI 内拷贝的 sei_hw_ptp_ts 与原始硬件 hw_ptp_ts 比对：>2ms 异常（SEI 拷贝出错）
    "sei_copy_error": 2_000,
    # 双目左右相机硬件 PTP 时间差（同 frame_index）：>1ms 异常
    "binocular_ptp_diff": 1_000,
    # 最近 IMU 样本与视频帧硬件 PTP 时间差：>2ms 异常
    "imu_video_sync": 2_000,
}

# 每种规则的来源说明（用于导出报告/详情）
RULE_META = {
    "app_receive_delay": {
        "label": "应用接收延迟",
        "detail": "|app_local_receive_ts − hw_ptp_ts|（软件接收时刻与硬件 PTP 之差，含传输/驱动/缓存延迟）",
    },
    "sei_copy_error": {
        "label": "SEI 拷贝错误",
        "detail": "|sei_hw_ptp_ts − hw_ptp_ts|（MP4 SEI 内拷贝的硬件 PTP 与原始硬件 PTP 之差，SEI 嵌入出错）",
    },
    "binocular_ptp_diff": {
        "label": "双目 PTP 同步差",
        "detail": "|left.hw_ptp − right.hw_ptp|（同 frame_index 下左右相机硬件 PTP 之差，双目同步要求百微秒级）",
    },
    "imu_video_sync": {
        "label": "IMU-视频同步差",
        "detail": "|imu_sample_ts − hw_ptp_ts|（最近 IMU 样本与视频帧硬件 PTP 之差，数采同步指标）",
    },
}

SEVERITY_BY_RULE = {
    "app_receive_delay": "warn",
    "sei_copy_error": "error",
    "binocular_ptp_diff": "error",
    "imu_video_sync": "warn",
}


# ---------------------------------------------------------------------------
# 文件发现：会话目录内各类来源文件的识别规则（glob，相对会话根递归）
# ---------------------------------------------------------------------------
SOURCE_PATTERNS = {
    # 硬件 PTP（SEI 解析输出 CSV）：frame_index,timestamp_us
    "hardware_ptp": ["**/*_sei.csv"],
    # 视频编码帧内时间戳（MP4 pts CSV）：单列 timestamp_us
    "mp4_pts": ["**/*_pts.csv"],
    # IMU CSV：timestamp_us,x,y,z,type
    "imu": ["**/*_imu*.csv"],
    # EGOViewer 应用日志：ego-viewer-*.log（app 目录，含 sessionId 即 trace）
    #   + EGOViewer 本机导出的 EGOViewer_*.log（data/logs）
    #   + 固件 orbbec.log / orbbec_1.log
    "viewer_app": ["**/ego-viewer-*.log", "**/EGOViewer_*.log",
                   "EGOViewer_*.log", "**/orbbec*.log", "orbbec*.log"],
}


# ---------------------------------------------------------------------------
# CSV 字段别名：同时兼容真实文件（frame_index/timestamp_us）与规格命名
# （frame_idx/hw_ptp_ts/sei_hw_ptp_ts/imu_sample_ts...）。
# 值为 (规范字段名, 优先级)；解析时按列名匹配。
# ---------------------------------------------------------------------------
COL_ALIASES = {
    "frame_index": ["frame_index", "frame_idx", "frame_no", "frame", "index"],
    "hw_ptp_ts": ["timestamp_us", "hw_ptp_ts", "hw_ptp_timestamp", "ptp_ts", "timestamp"],
    "sei_hw_ptp_ts": ["sei_hw_ptp_ts", "sei_timestamp_us", "sei_ptp_ts"],
    "imu_sample_ts": ["timestamp_us", "imu_sample_ts", "timestamp"],
}

# 时间戳单位自动判定阈值（无显式单位时按数量级推断）
#   ns: ~1.7e18   us: ~1.7e15   ms: ~1.7e12
UNIT_BOUNDS = {"ns": 1e17, "us": 1e14, "ms": 1e11}
