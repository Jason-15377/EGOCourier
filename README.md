# EGO Courier

轻量日志中转网关：把 EGO 多设备的**硬件 PTP 时间戳、EGOViewer 应用日志、MP4 视频帧内时间戳、IMU** 四类日志收拢到一个工具里，自动按帧对齐、计算两两时间戳差值、超过阈值标记异常，桌面查询 + 一键打包导出。

前端为 **PySide6 桌面 GUI**，另附命令行工具：

| 形态 | 入口 | 说明 |
|---|---|---|
| **桌面版** | `EGO Courier.bat` 或 `python run_desktop.py` | PySide6 桌面 GUI：日志导出工作台（BLE 配网、EGOViewer 三类日志一键导出 / 勾选导出）+ 会话记录 / 日志分析 / Trace 查询 |
| **命令行** | `ego <子命令>` 或 `python -m ego_relay.cli` | 导入/查询/导出/阈值/WiFi 等（`ego.bat` 是对应的批处理包装） |
| **便携版** | `EGO Courier.exe`（见 [GitHub Releases](https://github.com/Jason-15377/EGOCourier/releases)） | 自带 Python 与全部依赖，目标机器无需安装任何东西；解压即用 |

**纯 Python 实现**。核心包零第三方依赖（stdlib）；桌面版依赖 PySide6、matplotlib、numpy、paramiko、mcap（清单见 `requirements.txt`）。

桌面 GUI 共四个页签：**日志导出**（BLE 配网 + EGOViewer 三类日志收集 / 勾选打包）、**会话记录**（多选删除 / 对比 / 导出汇总）、**日志分析**（对齐 + 异常检测 + 可视化）、**Trace 查询**。

## 桌面版（GUI）说明

## 桌面版（GUI）说明

**环境要求：Python 3.10–3.12（推荐 3.12）。** 3.13 及以上暂无 PySide6 / matplotlib
预编译包，装依赖会失败——启动器会提前拦下并提示改用 3.12。

依赖安装：**首次启动自动完成**。双击 `EGO Courier.bat` 后，启动器会自己完成：

1. 找到本机的 Python（优先 3.12 / 3.11 / 3.10）；
2. 在本目录建独立环境 `.venv`（不污染系统 Python）；
3. 按 `requirements.txt` 只补装缺失的依赖（约 200MB，1–10 分钟）；
4. 启动界面。之后再启动会跳过以上步骤，直接打开。

装不上时窗口**不会闪退**，会给出中文原因（Python 版本过新 / 网络不通 / 无写入权限 /
磁盘空间不足）与处理办法，完整输出在 `log\install.log`。

可选：公司网络访问 PyPI 不通时，先设置内网镜像再双击启动：

```bat
set EGO_PIP_INDEX_URL=https://<内网 PyPI 镜像>/simple
```

手动安装（不用启动器时）：

```bat
python -m pip install -r requirements.txt
```

启动：双击 `EGO Courier.bat`（或 `python run_desktop.py`，后者要求依赖已装好）。

启动器诊断命令：

```bat
python bootstrap.py --check          :: 只体检，打印各依赖版本，不安装不启动
python bootstrap.py --no-install     :: 缺依赖直接报错，适合排查环境问题
python bootstrap.py --reinstall      :: 强制重装全部依赖
python bootstrap.py --system-python  :: 不用 .venv，直接用系统 Python
```

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
- **会话记录**：列表按**导出批次**组织——每次「开始一键导出」产生一条（同一来源目录重复分析就地更新，不新增）。列依次为 **导出批次 / 来源 / 设备 SN / 数据 / 帧数 / 异常 / trace_id / 导出时间**：「来源」区分手机日志、电脑端导出、分析目录；「导出批次」把目录名翻译成时间点（如 `导出批次 2026-10-20 10:39:36`）；「设备 SN」只在解析出真实序列号时显示，否则标 `未识别`（不再拿会话名冒充设备）；「数据」标 `可对齐` / `仅日志`，0 帧的会话保留在列表但帧数显示 `无可对齐数据`、异常显示 `—`，表示这批日志只做留存、未参与时间戳对齐。支持**勾选多选 → 删除选中 / 对比选中 / 导出汇总报告**（横向对比帧数、丢帧、左右同步、IMU 同步、异常等）。
- **设备与日志 Tab**：
  - ① 蓝牙配网：扫描 BLE 设备、「确认选择」、下发 Wi-Fi 接入局域网，自动回填设备 IP（例如 `10.9.82.9`）。**Wi-Fi 名称**直接用下拉选择电脑可见的 Wi-Fi（电脑当前连接会自动带出，可「搜索 Wi-Fi」刷新），再填密码。
  - **② EGOViewer 日志导出**（三类日志统一收集，不再细分 app/sdk/firmware 勾选）：自动识别 EGOViewer 版本根目录，一键收集 app/sdk/firmware 三类日志。**开始一键导出**→ 每次在「输出目录」下新建**全新时间戳目录** `EGO_export_YYYYMMDD_HHMMSS/`（内按 `app/ sdk/ firmware/` 子目录），不复用/混入本地旧日志；firmware 通过 Orbbec SDK（复用 EGOviewer 的 `OrbbecSDK.dll`）**从设备实时拉取最新固件日志**到 `firmware/`（设备需在线；设备离线/拉取失败则整体报错）；app/sdk 从 EGOViewer 根目录 `data\logs`、`Log` 收集。随后按选项自动导入分析 + 生成 HTML 报告。
  - **勾选导出**（手动精确打包）：同样收集三类日志，可选筛选范围（**时间段（起~止）**、**手动勾选具体日志文件**——弹出对话框按分类勾选，可按文件名时间戳精确取某次会话，防止不同会话日志混包导致分析不同步），打包成日期时间命名 zip（`EGOViewer_logs_YYYYMMDD_HHMMSS.zip`）；原始日志不改动；若未来版本调整目录结构，会按文件名模式回退搜索以兼容。
  - 两类导出共用「输出目录」与「选项」（导出后自动导入分析 / 自动生成 HTML 报告）。
  - **EGOViewer 手机日志**（② 分组内的一行勾选项，Android / adb 通道）：**勾选即自动扫描**已连接并授权的手机并在下拉框列出（取消勾选再勾回不会重复扫描；未勾选时下拉框显示灰色提示语），随后与固件日志一起由「开始一键导出」统一导出。**导出目录平铺，不区分来源**：
    ```
    EGO_export_YYYYMMDD_HHMMSS/
    ├── app/  sdk/  firmware/     电脑端 EGOViewer / Orbbec SDK 产出
    └── <机型>/                    手机：手机导出包.zip、sdk日志/、固件日志/<SN>-<时刻>/
    ```
    手机侧只拉**最新一个**导出包（不拉历史包，避免目录里堆满重复内容），压缩包原样不拆。固件日志有两条落盘路径，都会解析并计入设备分组：zip 内的 `firmware/<SN>-.../`（或 `firmware/persisted/<SN>-.../`）以及手机侧持久化目录 `固件日志/<SN>-.../`——App 有时把 zip 落在私有 cache（adb 读不到），此时后者是唯一完整副本。勾选手机日志时以手机产物提供固件日志，不再从电脑 USB 重复拉取。注意：手机只拉「导出那一刻仍能导出」的固件日志，设备断开后再导出该分组为空，需现场即时导出。
  - 三类日志分类勾选框默认：**app 不勾、sdk/firmware 勾**（可自行调整），「开始一键导出」与「勾选导出」共用同一组勾选框与输出目录。
  - 手机日志与 app/sdk/firmware 共用同一套「**选项**」：拉取后自动导入分析 / 自动生成 HTML 报告。两个分组放进整页滚动区，内容不会被窗口裁切遮挡；操作栏（开始一键导出 / 勾选导出 / 取消 / 清空）与进度条统一在下方。
- **日志分析 Tab**：除「选择目录…/zip…」外，新增「从 EGOViewer 导入」按钮——自动定位本机 EGOViewer 的 `data` 目录并直接分析（免 SSH，原「设备与日志」页的本地导入已合并至此）。
- 界面：浅灰商务科技风、按钮角色配色（执行=蓝、清空=橙、停止=灰）、表头加粗、表格隔行、底部日志框 `[HH:MM:SS] 级别｜信息`。
- **设备日志协议**：GUI 的「② EGOViewer 日志导出」一键收集三类日志——app/sdk 从本机 EGOViewer 安装目录收集（`data\logs`、`Log`），firmware 通过 Orbbec SDK（复用 EGOviewer 的 `OrbbecSDK.dll`）从设备实时导出到 `firmware/`；「勾选导出」则从本机 EGOViewer 目录打包三类日志（含既有 `data\firmware-logs`）。设备端 SSH/SFTP 拉取 app 日志的通道暂未开通（代码保留，待后续启用）。

## 一键启动

```bat
:: 启动桌面 GUI（首次会自动建 .venv 并安装依赖）
EGO Courier.bat
```

或者命令行（依赖已装好后）：
```bat
python run_desktop.py
```

## 打包成 .exe

把桌面端打成独立多分辨率图标的 Windows 可执行文件，目标机器无需装 Python。
两套配置产出的程序**功能一致**，只是目录结构不同，别混用：

```bat
:: 方式一：PyInstaller 默认形态（_internal\ 目录布局）
build_exe.bat            :: onedir → dist\EGO Courier\EGO Courier.exe（推荐）
build_exe.bat onefile    :: 单文件 → dist\EGO Courier.exe

:: 方式二：便携版（runtime\ 目录布局，对齐 Ego-Vue Simple Evaluate）
build_portable.bat       :: → dist\EGO Courier\EGO Courier.exe + runtime\
```

便携版把依赖摊在 `runtime\`、源码明文放在 `runtime\app\`，**现场改 `.py` 重启即生效、
不用重新打包**，适合交给测试同事；正式分发用 `package_portable_release.bat` 打成
`dist\EGO_Courier_portable_<版本>_win64.zip`，上传到 GitHub Releases。

`onedir` 与 `onefile` 的取舍：onedir 启动快、杀软误报少，推荐分发整个目录；
onefile 只有一个约 80MB 的 exe，但每次启动要解压到临时目录，稍慢且误报相对更多。
UPX 压缩在 spec 中已关闭，避免提高误报率。

### 打包说明

- **图标**：`assets\ego_courier.ico` 含 16/20/24/32/40/48/64/96/128/256 共 10 种尺寸
  （每种尺寸 PNG + BMP 两帧，共 20 帧），同时写进 exe 资源（资源管理器/任务栏/开始菜单）
  并由程序在运行时加载为窗口图标。
- **数据目录**：源码模式数据在项目根的 `data\`；打包后默认放在 **exe 同级目录的 `data\`**
  （不能用临时解压目录，否则退出即丢），随程序目录整体迁移。如需指定别处，启动前设置
  环境变量 `EGO_DATA_ROOT=<目录>`。
- **ffmpeg / ffprobe 不打进包**：只在做视频分析（MP4/MCAP）时需要，程序启动时会自动从
  PATH 或 winget 安装目录探测；缺失时界面会提示安装方式，日志/CSV 分析不受影响。
- **OrbbecSDK.dll 不打进包**：固件日志实时导出时，从用户机器上已装的 EGOViewer 目录里查找，
  避免随包分发造成 SDK 版本错配。

## 标志（Logo）

标志生成脚本：`assets\make_logo.py`（依赖 Pillow）。改了配色/造型后重新生成，两个打包
脚本也会在打包前自动重新生成一次：

```bat
python assets\make_logo.py
```

设计要点：深蓝圆角方底板 + 两道朝右上的箭头（白色主箭头 + 青色次级箭头），
箭头取「Courier 送达」语义，尾部短横线抽象三类日志（app/sdk/firmware）；
低分辨率（≤32px）自动走简化路径去掉短横线，保证任务栏 16px 下仍清晰可辨。

| 文件 | 用途 |
|---|---|
| `assets\ego_courier.ico` | Windows 多分辨率图标（exe 资源 + 窗口/任务栏） |
| `assets\logo.png` / `logo_512.png` | 512px 主标志（文档 / 关于页） |
| `assets\logo_<n>.png` | 16–256 各尺寸透明底 PNG（界面内小图标用） |

> 若 `assets\` 缺失（例如只拷了 `.py` 文件），程序会自动退回代码内绘制的矢量图标，
> 窗口图标不会为空。

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
├── .venv/              首次启动自动创建的运行环境（不入库）
├── requirements.txt    桌面版依赖清单（启动器据此自动安装）
├── bootstrap.py        启动器：找 Python → 建 .venv → 装依赖 → 启动
├── EGO Courier.bat     启动桌面 GUI（推荐入口）
├── run_desktop.py      直接启动 GUI（要求依赖已装好）
├── build_exe.bat       打包 .exe（onedir / onefile）
├── ego_courier.spec    PyInstaller 打包配置
├── ego.bat             CLI 包装
└── README.md
```

## 测试

```bat
python -m unittest tests.test_analysis -v     :: 对齐与四类异常规则单测
python -m pytest tests -q                     :: 全量回归（114 项）
python tests/smoke_gui.py                     :: 桌面 GUI 冒烟（offscreen）
```

## 变更记录

### v1.0.6（2026-10-21）缺陷修复

| 编号 | 现象 | 修复 |
|---|---|---|
| T1 | 「重新检测」永远找不到 EGOViewer | 搜索根改为动态推导（环境变量 `EGO_VIEWER_ROOT` → 本工具所在盘 → 常见工具目录**及下一层**）；判据改为内容特征（`EGOViewer.exe` / `Log/EGOViewer_*.log` / 数据目录），不再依赖 `data/logs` 与 `win_x64` 字样；app 日志按 `Log → logs → data/logs` 查找 |
| T2 | Wi-Fi 密码错误时配网失败无任何提示 | `configure_wifi` 校验设备返回的 `result`，被拒时直接抛 `BleError`；等待 IP 超时改为 `failed` + 红色弹窗；状态栏实时显示 `已等 N/40 秒` |
| T3 | 固件日志报「未找到 OrbbecSDK.dll」 | `_candidate_dlls` 增加回退链（本工具 `runtime/lib` → EGOViewer 安装目录 → 搜索根）；SDK 目录直接沿用「EGOViewer 根目录」（DLL 随 EGOViewer 安装，不再单列输入框），导出前预检 DLL |
| T4 | 配网后的纯网络设备被静默跳过 | `device_host` 真正传给 SDK：`export_all_firmware_logs` 新增 `prefer_ip`/`prefer_port` 按 IP 精确定位设备，`include_network` 随配网 IP 自动开启；导出前 ping 目标设备 |
| T5 | 固件日志缺时间戳层、重复导出互相覆盖 | 先拉到 `_staging`，再按 `firmware/<设备>/<YYYYMMDD-HHMMSS>/` 重组落盘，`rel` 与磁盘严格一致；同名同秒加时间戳后缀 |
| T6 | 「找不到 DLL」被报成「设备未连接」 | `OrbbecSdkError` 增加 `kind`（dll / no_device / busy / fw_disabled / timeout / canceled），界面按类型给不同排查提示 |
| — | 扫描列表混进鼠标/耳机等无关蓝牙设备 | 按名称前缀（`EGO_`/`UMIFinger_`/`WristCam_`/`EGOPro_`/`UMIGripper_`/`EGODepth_`）与 OUI `54:14:FD` 过滤，并按地址去重 |
| — | 「设备 IP」预填 `10.9.82.9` 等测试值误导用户 | `DEVICE_IP` / `EXAMPLE_DEVICE_IP` 改为空，由配网结果或用户填写 |
| — | firmware 分类把 `.lock`/`dmesg.txt`/`device_status.txt` 也收进包 | 改为按 `orbbec*.log*`、`messages.log*`、`observice.console.log*` 精确匹配 |
| — | 便携版 `runtime/lib/EgoLowBle.dll` 可能找不到 | `find_dll` 增加 `<exe>/runtime/lib/` 探测 |

## 已知限制 / 待办

- `ego device pull` 为**预留占位**：设备 WiFi/网络导出通道待确认接口（ADB / SMB / HTTP）后实现。
- 后续迭代：异常自动截帧（对接 `frame_dumps` jpg）、AI 日志总结。
- `app_receive_delay`（应用接收延迟）需要同一会话同时具备 **app 日志（提供接收时刻）+ 硬件 PTP（SEI，提供 hw_ptp_ts）** 才能计算；若仅有 app 日志或仅有 SEI 数据，则该规则自动跳过，不影响其余三条规则。
