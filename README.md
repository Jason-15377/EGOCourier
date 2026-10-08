# EGO Courier

轻量日志中转网关：把 EGO 多设备的**硬件 PTP 时间戳、EGOViewer 应用日志、MP4 视频帧内时间戳、IMU** 四类日志收拢到一个工具里，自动按帧对齐、计算两两时间戳差值、超过阈值标记异常，桌面查询 + 一键打包导出。

前端为 **PySide6 桌面 GUI**，另附命令行工具：

| 形态 | 入口 | 说明 |
|---|---|---|
| **桌面版** | `EGO Courier.bat` 或 `python run_desktop.py` | PySide6 桌面 GUI：设备与日志工作台（BLE 配网、EGOViewer 三类日志一键导出 / 勾选导出）+ 会话/分析页面 |
| **命令行** | `ego <子命令>` 或 `python -m ego_relay.cli` | 导入/查询/导出/阈值/WiFi 等 |

**纯 Python 实现**。核心包零第三方依赖（stdlib）；桌面版依赖 PySide6、matplotlib、imageio、paramiko（见下）。

## 桌面版（GUI）说明

依赖安装（一次）：
```bat
python -m pip install PySide6 matplotlib imageio paramiko
```

启动：双击 `EGO Courier.bat`（或 `python run_desktop.py`）。

- **日志分析 Tab**（导入 + 综合分析 + 可视化，功能化布局、无“概览/KPI 卡片”风格）：
  - 选择本地日志文件夹或 zip → 开始分析。后台同时跑两条管线：
    - **EGO 管道**：解析 SEI/MP4-pts/IMU/EGOViewer 日志并对齐、落库（可在「会话记录」查看）；
    - **video_analyzer**：处理目录内 MP4/MCAP/*_pts.csv，做 SEI 比对、丢帧、左右 Color 同步、IMU 同步。
  - 结果按功能分页展示：**丢帧检测**（曲线图 + 丢帧明细表）、**左右 Color 同步**（曲线图 + 配对/未配对表）、**SEI vs CSV 比对**、**IMU 同步**、**帧统计**、**来源文件**。
  - 工具栏：导出 HTML 报告、导出 zip。
  - **可取消**：分析过程中可点「取消」停止。
  - **结果缓存**：同一目录/日志未变化时复用上次结果（来源签名比对），跳过耗时重算。
  - **数据校验**：自动剔除并统计“垃圾时间戳”（帧号被误当 us 等），在「帧统计」与报告中展示，减少脏数据误判。
  - 目录内无 MP4/MCAP 时自动回退到 EGO 会话数据（frame_entries）做同样的可视化，不报错。
- **会话记录**：支持**勾选多选 → 删除选中 / 对比选中 / 导出汇总报告**（横向对比帧数、丢帧、左右同步、IMU 同步、异常等）。
- **设备与日志 Tab**：
  - ① 蓝牙配网：扫描 BLE 设备、「确认选择」、下发 Wi-Fi 接入局域网，自动回填设备 IP（例如 `10.9.82.9`）。**Wi-Fi 名称**直接用下拉选择电脑可见的 Wi-Fi（电脑当前连接会自动带出，可「搜索 Wi-Fi」刷新），再填密码。
  - **② EGOViewer 日志导出**（三类日志统一收集，不再细分 app/sdk/firmware 勾选）：自动识别 EGOViewer 版本根目录，一键收集 app/sdk/firmware 三类日志。**开始一键导出**→ 每次在「输出目录」下新建**全新时间戳目录** `EGO_export_YYYYMMDD_HHMMSS/`（内按 `app/ sdk/ firmware/` 子目录），不复用/混入本地旧日志；firmware 通过 Orbbec SDK（复用 EGOviewer 的 `OrbbecSDK.dll`）**从设备实时拉取最新固件日志**到 `firmware/`（设备需在线；设备离线/拉取失败则整体报错）；app/sdk 从 EGOViewer 根目录 `data\logs`、`Log` 收集。随后按选项自动导入分析 + 生成 HTML 报告。
  - **勾选导出**（手动精确打包）：同样收集三类日志，可选筛选范围（**时间段（起~止）**、**手动勾选具体日志文件**——弹出对话框按分类勾选，可按文件名时间戳精确取某次会话，防止不同会话日志混包导致分析不同步），打包成日期时间命名 zip（`EGOViewer_logs_YYYYMMDD_HHMMSS.zip`）；原始日志不改动；若未来版本调整目录结构，会按文件名模式回退搜索以兼容。
  - 两类导出共用「输出目录」与「选项」（导出后自动导入分析 / 自动生成 HTML 报告）。
  - **设备 SSH 拉取 app 日志**：通道**暂未开通**（前端分组已隐藏，后端与代码保留），后续开通后再启用。
  - 三类日志分类勾选框默认：**app 不勾、sdk/firmware 勾**（可自行调整），「开始一键导出」与「勾选导出」共用同一组勾选框与输出目录。
  - 共用一套「**选项**」勾选：拉取后自动导入分析 / 自动生成 HTML 报告。三个分组放进整页滚动区，内容不会被窗口裁切遮挡；操作栏（开始一键导出 / 勾选导出 / 取消 / 清空）与进度条统一在下方。
- **日志分析 Tab**：除「选择目录…/zip…」外，新增「从 EGOViewer 导入」按钮——自动定位本机 EGOViewer 的 `data` 目录并直接分析（免 SSH，原「设备与日志」页的本地导入已合并至此）。
- 界面：浅灰商务科技风、按钮角色配色（执行=蓝、清空=橙、停止=灰）、表头加粗、表格隔行、底部日志框 `[HH:MM:SS] 级别｜信息`。
- **设备日志协议**：GUI 的「② EGOViewer 日志导出」一键收集三类日志——app/sdk 从本机 EGOViewer 安装目录收集（`data\logs`、`Log`），firmware 通过 Orbbec SDK（复用 EGOviewer 的 `OrbbecSDK.dll`）从设备实时导出到 `firmware/`；「勾选导出」则从本机 EGOViewer 目录打包三类日志（含既有 `data\firmware-logs`）。设备端 SSH/SFTP 拉取 app 日志的通道暂未开通（代码保留，待后续启用）。

## 一键启动

```bat
:: 启动桌面 GUI
EGO Courier.bat
```

或者命令行：
```bat
python run_desktop.py
```

## 打包成 .exe

把桌面端打成独立多分辨率图标的 Windows 可执行文件，目标机器无需装 Python：

```bat
build_exe.bat            :: onedir → dist\EGO Courier\EGO Courier.exe（推荐）
build_exe.bat onefile    :: 单文件 → dist\EGO Courier.exe
```

详见 [README_桌面端.md](README_桌面端.md#打包成-exe另一版启动方式)。

## 命令行工具（ego）

```bat
ego list                                                     列出会话
ego import "D:\...\EGO_AK8896100BJ_20260915_143309"          导入目录/zip 并分析
ego show 1                                                   会话详情 + 异常汇总
ego query 1 --anomalies                                      查看会话 1 的异常
ego query 1 --side left --anomaly-only                       查看左目异常帧
ego export 1 -o out.zip                                      一键打包导出
ego attach-log 1 "path\ego-viewer-2026-09-15.log"            把 EGOViewer app 日志关联进会话 1 并重算
ego thresholds show                                          查看阈值
ego thresholds set sei_copy_error=2000 app_receive_delay=50000  调整阈值(μs)
ego wifi --scan                                          扫描 WiFi（SSID/信号/安全/连接状态）
ego status                                                   健康检查
ego evlog list                                              列出本机 EGOViewer 安装目录及各类日志数
ego evlog export [-o 目录] [--days 7] [--report] [--root 根]  收集本地 EGOViewer 三类日志并打包 zip（可自动导入分析+报告）
ego device pull ...                                        预留：设备 WiFi 导出（通道未配置）
```

## 异常自动截帧

对每条异常，按**异常帧的硬件 PTP 时间戳**匹配会话目录里的 `frame_dumps/**/*.jpg`（文件名以 `_<hw_ptp_us>.jpg` 结尾、`_left_/_right_` 区分侧）：
- 精确匹配 → 该采样帧；无精确时取最近且在 500ms 容差内的一帧。
- 截帧随会话入库（`anomalies.frame_image`），桌面端异常列表显示缩略图，一键导出时打包进 `anomaly_frames/`，报告内也内嵌图片。
- 说明：`frame_dumps` 是采样帧（非全帧），因此只有落在采样范围内的异常帧才有截图，属于预期行为。

## 关联 EGOViewer app 日志（attach-log）

当会话只有 SEI/MP4/IMU（无 app 日志）时，可单独把 EGOViewer app 日志关联进来，获得 `trace_id(sessionId)` 和应用接收时刻，从而让 `app_receive_delay` 可计算：

```bat
ego attach-log <session_id> "D:\...\ego-viewer-2026-09-15.log"
```

或桌面端会话详情页点「关联 app 日志」输入路径。系统会把日志**复制进附件区**（不修改你原始数据），加入该会话的 `log_files` 并**就地重算**（同一会话更新，不新建）。重复关联同一文件幂等。

> **已知限制（真实数据实测）**：EGOViewer app 日志里的 `frameIndex` 是**设备全局帧计数**，而 SEI 会话的 `frame_index` 是会话内相对编号，二者**不一定时间对齐**。因此只有当 app 日志片段与 SEI 会话属于**同一次录制（时间匹配）**时，`app_receive_delay` 才准确；若 app 日志与 SEI 不同录制时段，计算出的差值会很大并被标记为异常——这本身也能提示"日志与数据不匹配"。

## 时间戳与真值基准

- **真值基准 = 硬件 PTP `hw_ptp_ts`（epoch 微秒 UTC）**。所有来源统一归一化到微秒 epoch UTC。
- 解析器字段名/单位**可配置**，同时兼容真实文件（`frame_index,timestamp_us`、单列 `timestamp_us`、`[2026-09-15 17:53:13.815 ...]`）与规格命名（`hw_ptp_ts`/ns、`sei_hw_ptp_ts`、`recv_hw_frame_idx`、`app_local_receive_ts`、`trace_id`）。时间戳单位自动按数量级判定（ns/us/ms）。

### 关联链路（方案A 强关联）
`EGOViewer.log(frameIndex)` → 匹配硬件 PTP 的 `frame_index` 拿 `hw_ptp_ts` → 再以 `hw_ptp_ts` 匹配 MP4 pts 的 `sei_hw_ptp_ts`。trace_id 取自应用日志 / 会话级。

> **真实字段说明（已用 ego-viewer-*.log 实测）**：EGOViewer 应用日志的 trace 标识是 **`sessionId=<uuid>`**（如 `06bf69fa-32e0-41bb-8d5f-284a8ccddcf2`），帧序号用 `frameIndex` / `leftFirstIndex` / `rightFirstIndex`，硬件时间戳用 `deviceTimestampUs` / `*TimestampUs`，app 接收时刻 = 日志行首时间戳（UTC+8 毫秒）。解析器同时兼容规格命名的 `trace_id:"<32hex>"`、`recv_hw_frame_idx`、`app_local_receive_ts`。

## 异常规则（阈值默认，可配置）

| 规则 | 计算 | 阈值 |
|---|---|---|
| `app_receive_delay` | \|app_local_receive_ts − hw_ptp_ts\| | 50 ms |
| `sei_copy_error` | \|sei_hw_ptp_ts − hw_ptp_ts\|（SEI 嵌入校验） | 2 ms |
| `binocular_ptp_diff` | \|left.hw_ptp − right.hw_ptp\|（同帧号，双目同步） | 1 ms |
| `imu_video_sync` | \|最近 IMU − hw_ptp\|（数采同步） | 2 ms |

## 目录结构

```
EGOCourier/
├── ego_relay/          核心包
│   ├── config.py       阈值 / 字段映射 / 时区 / 文件发现
│   ├── db.py           sqlite 存储层
│   ├── parsers.py      四类解析器 + 时间戳归一化
│   ├── analysis.py     对齐 + 四类异常
│   ├── export.py       一键打包导出（zip + HTML 报告）
│   └── cli.py          命令行工具
├── ego_gui/            桌面 GUI（PySide6）
│   ├── main.py         入口 + 图标（优先读 assets/，缺失时矢量兜底）
│   └── resources.py    资源定位（兼容源码模式与 PyInstaller 打包）
├── assets/             标志资源：make_logo.py + ego_courier.ico + logo*.png
├── packaging/entry.py  PyInstaller 打包入口
├── tests/              单元测试 + GUI 冒烟测试
├── data/               运行数据（DB、导出、临时解压）
├── EGO Courier.bat     启动桌面 GUI
├── run_desktop.py      桌面 GUI 启动器（python run_desktop.py）
├── build_exe.bat       打包 .exe（onedir / onefile）
├── ego_courier.spec    PyInstaller 打包配置
├── ego.bat             CLI 包装
└── README.md
```

## 测试

```bat
python -m unittest tests.test_analysis -v     :: 对齐与四类异常规则单测
python tests/smoke_gui.py                     :: 桌面 GUI 冒烟（offscreen）
```

## 已知限制 / 待办

- `ego device pull` 为**预留占位**：设备 WiFi/网络导出通道待确认接口（ADB / SMB / HTTP）后实现。
- 后续迭代：异常自动截帧（对接 `frame_dumps` jpg）、AI 日志总结。
- `app_receive_delay`（应用接收延迟）需要同一会话同时具备 **app 日志（提供接收时刻）+ 硬件 PTP（SEI，提供 hw_ptp_ts）** 才能计算；若仅有 app 日志或仅有 SEI 数据，则该规则自动跳过，不影响其余三条规则。
