"""domain.constants 的实现。"""



US_PER_MS = 1000


FRAME_COUNT_ROW_DIFF_US = 1_000        # 相机帧数对齐: 鱼眼三目 首/尾行时间戳差值 < 1ms


DEPTH_ROW_DIFF_US = 5_000              # 相机帧数对齐: depth 与 depth_rgb 首/尾行时间戳差值 < 5ms


ROW_ALIGN_THRESHOLD_US = 10_000        # 同行时间戳对齐阈值 10ms


CAM_SYNC_THRESHOLD_US = 1_000         # 相机时间戳同步阈值 1ms


IMU_SYNC_THRESHOLD_US = 1_000          # IMU 时间戳同步阈值 1ms


INTERVAL_WAVE_MS = 10                  # 帧间隔波动阈值 10ms


RGB_DROP_MULTIPLIER = 1.5              # RGB 丢帧阈值 = 1.5 × 帧间隔基准


IMU_DROP_MULTIPLIER = 1.5              # IMU 丢帧阈值 = 1.5 × expected (expected = 1/IMU频率)


MIN_VALID_TS = 1_500_000_000_000_000   # 合理时间戳下限 (约 2022 年, µs)


TS_COLUMN_NAMES = ("timestamp_us", "pts_us", "pts", "timestamp", "ts",
                   "timestamp_ms", "t_us", "device_timestamp_us")


FRAMES_SUFFIX = "_frames.csv"


FRAMES_TS_COL = 7                       # 第 8 列 (0 基第 7)


FRAMES_REASON_COL = "drop_reason"


PRESENTATION_COL = "presentation_time_us"


FRAMES_ROLE_MAP = {
    "color_left": "left_rgb",
    "color_right": "right_rgb",
    "color_side_left": "side_left",
    "color_side_right": "side_right",
    "color_front": "front_rgb",
    "color_back": "back_rgb",
    "color_rear": "back_rgb",
    "color_top": "top_rgb",
}


ROLE_LABELS = {
    "left_rgb": "左目", "right_rgb": "右目",
    "side_left": "左侧目RGB", "side_right": "右侧目RGB",
    "front_rgb": "前目RGB", "back_rgb": "后目RGB", "top_rgb": "顶目RGB",
    "up_rgb": "上目RGB", "depth_rgb": "深度RGB", "depth": "深度",
    "color_rgb": "RGB",
    "wrist_left": "左腕带", "wrist_right": "右腕带",
}

ROLE_ORDER = ["left_rgb", "right_rgb", "side_left", "side_right",
              "front_rgb", "back_rgb", "top_rgb", "up_rgb", "depth_rgb",
              "color_rgb", "depth", "wrist_left", "wrist_right"]
