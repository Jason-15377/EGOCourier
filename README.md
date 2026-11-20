# EGO Courier

EGO 采集日志的收发与完整性分析工具：从设备 / EGOViewer / 手机收拢日志，并对录制目录做时间同步、丢帧、音频与元数据完整性分析，输出自包含 HTML 报告。

前端为 **PySide6 桌面 GUI**：

| 形态 | 入口 | 说明 |
|---|---|---|
| **桌面版** | `EGO Courier.bat` 或 `python run_desktop.py` | PySide6 桌面 GUI：**日志导出**（BLE 配网 + EGOViewer 三类日志一键导出 / 打包）+ **日志分析**（整目录完整性分析 + 自包含 HTML 报告）+ **会话记录**（分析历史留存与回看） |
| **便携版** | `EGO Courier.exe`（见 [GitHub Releases](https://github.com/Jason-15377/EGOCourier/releases)） | 自带 Python 与全部依赖，目标机器无需安装任何东西；解压即用 |

**纯 Python 实现**。设备导出部分零第三方依赖（stdlib）；桌面版依赖 PySide6、numpy、paramiko、mcap、lz4、zstandard（清单见 `requirements.txt`）。

桌面 GUI 共三个页签：**日志导出**（BLE 配网 + EGOViewer 三类日志收集 / 打包）、**日志分析**（选择目录 → 整目录分析 → HTML 报告）、**会话记录**（每次分析的归档，可筛选、回看、重跑或删除记录）。

## 日志分析

「日志分析」页内嵌 EGOChecker 分析引擎（源码位于 `ego_checker/`），一次分析读取所选目录下的**全部**采集数据：

- CSV：相机 / IMU / 夹爪等时间戳 CSV（`*_pts.csv`、`camera_*.csv`、`imu*.csv`…）；
- 视频：目录内的 MP4 与 MCAP 录制（相机视频流、传感器流；SEI 时间戳提取）；
- 音频：WAV 文件及其配套 CSV；
- 元数据：`meta_*.json` 完整性与准确性。

分析项：相机帧数对齐、帧间隔波动、同行对齐、相机同步、IMU 同步、RGB 丢帧、IMU 丢帧/倒退、音频时长与包完整性、元数据完整性；跨录制段做边界比对。结果**只输出一份自包含 HTML 报告**（不再写数据库、不再生成结果 CSV 或额外证据目录）。

参数区（与整机其它页签同一套浅色风格）按业务分组：会话与文件（单设备/多设备、落盘格式、配对等级、并发）、检查项（Depth / SEI / 音频 / 元数据）、阈值（自适应或固定输入）、帧截取（不同步帧 / 同步帧截取）。支持暂停 / 停止（停止仍出部分报告），分析完成后可「打开 HTML 报告」「打开输出文件夹」，双击结果单元格查看异常明细。完成弹窗会明确列出问题 Part 及失败的检查项，并提供「定位异常 Part」：在结果表中选中该 Part，同时在 HTML 报告中跳到对应明细行；报告顶部也有「问题快速定位」卡片。

「日志导出」完成一键导出后，会把本次导出目录预填到「日志分析」页，切换过去点「开始分析」即可。

### 定时分析

「日志分析」页顶部可在**目录分析**与**定时分析**之间切换：

- 定时分析支持「每天 HH:MM」或「仅一次 YYYY-MM-DD HH:MM」；到点自动用保存时的参数快照启动分析；
- 定时数据目录是一个**列表**，可添加多个目录，按列表顺序逐个分析；每个目录写入独立时间戳输出目录；
- 支持错过补跑（默认 2 小时内），程序启动或电脑睡眠唤醒后仍会按真实时间判断；
- 「启用定时分析」勾选时自动保存当前目录列表与参数；「保存定时设置」可更新规则/参数快照；「暂停定时」立即停用但保留配置；
- 定时运行结束会写入「会话记录」，备注标记为「定时任务」；与手动分析共用同一套结果表和 HTML 报告；多目录批次在「上次运行」里显示成功目录数，悬停可看每个目录的结果；
- 配置保存在 `data/analysis_schedule.json`，同一时间只允许一个 EGO Courier 实例执行定时（`QLockFile`），避免重复跑。

注意：定时分析依赖 EGO Courier 进程保持运行（窗口可最小化，但不要退出）；关闭程序后定时不会触发。不使用时可随时切回「目录分析」，原手动流程不变。

## 桌面版（GUI）说明

## 桌面版（GUI）说明

**环境要求：Python 3.10–3.12（推荐 3.12）。** 3.13 及以上暂无 PySide6
预编译包，装依赖会失败——启动器会提前拦下并提示改用 3.12。

依赖安装：**首次启动自动完成**。双击 `EGO Courier.bat` 后，启动器会自己完成：

1. 找到本机的 Python（优先 3.12 / 3.11 / 3.10）；
2. 在本目录建独立环境 `.venv`（不污染系统 Python）；
3. 按 `requirements.txt` 只补装缺失的依赖（约 150MB，1–10 分钟）；
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

- **日志分析 Tab**：选择本地录制目录 → 开始分析（后台线程，实时日志 + 进度，可暂停 / 停止）。分析读取目录内全部 CSV / MCAP / WAV / meta 数据，做同步、丢帧、音频、元数据与跨段边界检查，只生成一份自包含 HTML 报告。详见上文「日志分析」一节。
- **设备与日志 Tab**：
  - ① 蓝牙配网：扫描 BLE 设备、「确认选择」、下发 Wi-Fi 接入局域网，自动回填设备 IP（例如 `10.9.82.9`）。**Wi-Fi 名称**直接用下拉选择电脑可见的 Wi-Fi（电脑当前连接会自动带出，可「搜索 Wi-Fi」刷新），再填密码。
  - **② EGOViewer 日志导出**（三类日志统一收集，不再细分 app/sdk/firmware 勾选）：自动识别 EGOViewer 版本根目录，一键收集 app/sdk/firmware 三类日志。**开始一键导出**→ 每次在「输出目录」下新建**全新时间戳目录** `EGO_export_YYYYMMDD_HHMMSS/`（内按 `app/ sdk/ firmware/` 子目录），不复用/混入本地旧日志；firmware 通过 Orbbec SDK（复用 EGOviewer 的 `OrbbecSDK.dll`）**从设备实时拉取最新固件日志**到 `firmware/`（设备需在线；设备离线/拉取失败则整体报错）；app/sdk 从 EGOViewer 根目录 `data\logs`、`Log` 收集。导出后可在「日志分析」页对该目录做完整性分析。
  - **勾选导出**（手动精确打包）：同样收集三类日志，可选筛选范围（**时间段（起~止）**、**手动勾选具体日志文件**——弹出对话框按分类勾选，可按文件名时间戳精确取某次会话，防止不同会话日志混包导致分析不同步），打包成日期时间命名 zip（`EGOViewer_logs_YYYYMMDD_HHMMSS.zip`）；原始日志不改动；若未来版本调整目录结构，会按文件名模式回退搜索以兼容。
  - 两类导出共用「输出目录」与「选项」（导出后可在「日志分析」页分析该目录）。
  - **EGOViewer 手机日志**（② 分组内的一行勾选项，Android / adb 通道）：**勾选即自动扫描**已连接并授权的手机并在下拉框列出（取消勾选再勾回不会重复扫描；未勾选时下拉框显示灰色提示语），随后与固件日志一起由「开始一键导出」统一导出。**导出目录平铺，不区分来源**：
    ```
    EGO_export_YYYYMMDD_HHMMSS/
    ├── app/  sdk/  firmware/     电脑端 EGOViewer / Orbbec SDK 产出
    └── <机型>/                    手机：手机导出包.zip、sdk日志/、固件日志/<SN>-<时刻>/
    ```
    手机侧只拉**最新一个**导出包（不拉历史包，避免目录里堆满重复内容），压缩包原样不拆。固件日志有两条落盘路径，都会解析并计入设备分组：zip 内的 `firmware/<SN>-.../`（或 `firmware/persisted/<SN>-.../`）以及手机侧持久化目录 `固件日志/<SN>-.../`——App 有时把 zip 落在私有 cache（adb 读不到），此时后者是唯一完整副本。勾选手机日志时以手机产物提供固件日志，不再从电脑 USB 重复拉取。注意：手机只拉「导出那一刻仍能导出」的固件日志，设备断开后再导出该分组为空，需现场即时导出。
  - 三类日志分类勾选框默认：**app 不勾、sdk/firmware 勾**（可自行调整），「开始一键导出」与「勾选导出」共用同一组勾选框与输出目录。
  - 手机日志与 app/sdk/firmware 共用同一套「**选项**」：
    - 「先压缩再导出zip」：勾选后「开始一键导出」会在导出目录旁生成同名 `.zip`；导出目录保留，仍可直接用于「日志分析」。
    - 「只导新增日志」：仅对 app/SDK 本机日志生效，按上次成功导出的结束时刻只拉新增部分；手机日志和设备固件日志仍全量拉取。勾选框右侧显示当前「增量起点」，可点「清除记录」恢复全量。记录按 EGOViewer 根目录隔离，存于 `data/export_incremental.json`。
    两个分组放进整页滚动区，内容不会被窗口裁切遮挡；操作栏（开始一键导出 / 勾选导出 / 取消 / 清空）与进度条统一在下方。
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

## 分析能力（内嵌 EGOChecker 引擎）

日志分析引擎源码位于 `ego_checker/`，只保留分析内核（数据发现与读取、同步/丢帧检查、跨段边界、元数据完整性、HTML 报告生成），界面由 `ego_gui/analysis_tab.py` 提供，无独立 CLI。原分析流程（会话数据库、四类硬编码异常、Trace 查询、结果 CSV 导出、video_analyzer 独立脚本）已整体移除。

检查项（GUI 可勾选开关）：

| 检查项 | 判定 |
|---|---|
| 相机帧数对齐 | 全部相机帧数相等且首/尾行时间戳差 < 1ms（D3B 分层判定） |
| 帧间隔波动 | 每帧间隔与基准 expected=1/帧率 之差 < 阈值（默认 10ms） |
| 同行对齐 | 同帧相机时间戳最大差值 < 阈值（默认 10ms） |
| 相机同步 | 以各目为基准找最近邻，按 max 峰值分档 |
| IMU 同步 | 以相机为基准匹配 IMU，按 max 峰值分档（默认 1ms） |
| RGB 丢帧 | 帧间隔 > 1.5×基准 计丢帧 |
| IMU 丢帧/倒退 | 正向超周期计丢帧，负间隔判倒退 |
| 音频（WAV） | 时长、包完整性与连续性 |
| 元数据完整性 | meta_*.json 必备区块/字段/分片/取值/跨文件一致 |
| SEI 时间戳对齐（可选） | 视频 SEI 时间戳与相机 CSV 完全匹配 |

阈值可在「阈值自适应」（依据帧率与 IMU 频率自动取阈）与「阈值固定输入」之间切换。结果只生成一份自包含 HTML 报告；用户停止时仍生成部分报告（已完成段保留结果，未完成段标 ERROR）。

## 目录结构

```
EGOCourier/
├── ego_relay/          设备日志导出与配置
│   ├── config.py       路径 / 阈值 / 设备连接配置
│   ├── evlog_pack.py   EGOViewer 本机日志收集与打包
│   ├── mobile_adb.py   Android 手机日志（adb）
│   ├── orbbec_sdk.py   设备固件日志（Orbbec SDK）
│   ├── egolowble.py / ble_provision.py  BLE 配网
│   ├── wifi.py         WiFi 扫描 / ping
│   ├── sftp_client.py / sftp_pull.py    SSH/SFTP 拉取
│   ├── deps.py / deps_check.py          运行依赖检测
│   └── __init__.py
├── ego_gui/            桌面 GUI（PySide6）
│   ├── main.py         入口 + 图标（优先读 assets/，缺失时矢量兜底）
│   ├── app.py          主窗口 + 全局 QSS + 页签
│   ├── device_tab.py   日志导出页（BLE 配网 + 三类日志一键导出 / 打包）
│   ├── analysis_tab.py 日志分析页（内嵌 ego_checker 引擎）
│   ├── export_tab.py   BLE 扫描后台线程
│   └── resources.py    资源定位（源码模式与 PyInstaller 打包）
├── ego_checker/        日志分析引擎（EGOChecker 分析内核）
│   ├── app/            发现分段 / 分析编排 / 并行
│   ├── analysis/       同步、丢帧、区间、音频等检查
│   ├── data/           CSV / MCAP / 音频 / meta 读取与发现
│   ├── media/video.py  SEI 提取、帧截取、ffmpeg
│   ├── reporting/      HTML 报告与结果表格
│   ├── domain/         数据模型与常量
│   └── runtime/        路径、版本、运行日志
├── assets/             标志资源：make_logo.py + ego_courier.ico + logo*.png
├── packaging/entry.py  PyInstaller 打包入口
├── tests/              单元测试
├── data/               运行数据（导出、分析日志、临时解压）
├── .venv/              首次启动自动创建的运行环境（不入库）
├── requirements.txt    桌面版依赖清单（启动器据此自动安装）
├── bootstrap.py        启动器：找 Python → 建 .venv → 装依赖 → 启动
├── EGO Courier.bat     启动桌面 GUI（推荐入口）
├── run_desktop.py      直接启动 GUI（要求依赖已装好）
├── build_exe.bat       打包 .exe（onedir / onefile）
├── ego_courier.spec    PyInstaller 打包配置
└── README.md
```

## 测试

```bat
python -m unittest tests.test_fixes_v107_launcher tests.test_evlog_pack tests.test_mobile_adb -v
python -m unittest discover -s tests -p "test_*.py"
```

## 变更记录

### v1.1.1（2026-11-20）并发分析修复

| 编号 | 现象 | 修复 |
|---|---|---|
| T1 | 打包版「日志分析」选 2/4/6/8 个分析进程后，每启动一个子进程就弹出一个新的 EGO Courier 主窗口；分析停在「Part 1/N」不再继续（975 个 Part 的现场日志只到「[阶段开始] 分析 part0001」） | Windows 上 multiprocessing 的 spawn 子进程是「重新执行同一个 exe + `--multiprocessing-fork`」。PyInstaller 的运行钩子只**替换** `multiprocessing.freeze_support()` 的实现，不会自动调用；三个入口（`packaging/entry.py`、`packaging/entry_portable.py`、`ego_gui.main.main()`）此前都没有调用它，子进程于是完整执行了 GUI 启动流程：开新窗口、进事件循环，父进程永远收不到分析结果。现由三个入口在任何 Qt/界面初始化之前调用 `freeze_support()`，worker 子进程直接在 `spawn_main` 内运行，不再建窗。源码模式（`run_desktop.py` / `python -m ego_gui`）不受影响 |
| T2 | HTML 报告明细表每个相机一列，长文本不换行，表格被撑到几万像素宽，必须左右横移才能看全；「问题 Part 快速定位」标题也偏长 | 明细表仅 HTML 报告改用紧凑列（CSV / 窗口结果表仍保留完整列）：帧间隔波动、相机/IMU/夹爪同步等每相机结果在单元格内**纵向分行**，「相机/帧率/固件」「视频/音频时长」「丢帧」「音频完整性」「同步与对齐」各合并成一列；单元格允许换行，展开的详情表不会再撑宽主表；错误信息里的绝对路径只显示文件名（完整路径放在悬停提示里）。标题改为「问题快速定位」 |

测试：新增 `tests/test_analysis_parallel_frozen.py`（三个入口的 `freeze_support()` 必须先于启动自检与 QApplication 创建）与 `tests/test_report_compact_columns.py`（HTML 紧凑列、单元格纵向分行、换行 CSS、绝对路径缩短）；全套 **201** 项通过。

发布包：`EGO_Courier_portable_v1.1.1_win64.zip`（Python 3.12.10 + PyInstaller 6.22.3 构建，
67 524 818 字节）。按仓库约定 `dist/` 与 `*.zip` 不入库，通过 Release 附件分发。
安装前请注意：便携版必须**整目录解压**，只替换 exe 会因缺少 `runtime/` 而启动失败。

### v1.1.0（2026-11-19）分析引擎重构

| 类别 | 变更 |
|---|---|
| 分析引擎 | 内嵌 **EGOChecker**（`ego_checker/`）替换旧的 EGO 对齐管线与 `video_analyzer`：一次读取目录内的 CSV / MCAP / WAV / `meta_*.json`，做帧数对齐、帧间隔、同行对齐、相机/IMU/夹爪同步、RGB/IMU 丢帧、音频完整性、元数据完整性与跨录制段边界比对，只输出一份自包含 HTML 报告（不写数据库、不生成结果 CSV / 证据目录） |
| 日志分析页 | 重做为「目录分析 + 定时分析」双模式：后台线程 + 实时日志 + 进度、暂停/停止（停止仍出部分报告）、自适应/固定阈值、帧截取；分析完成可直接打开报告或输出目录，双击结果查看异常明细；完成弹窗列出问题 Part 并可定位到结果表与 HTML 报告对应行 |
| 定时分析 | 新增每日 / 单次定时，支持多目录按序批量分析、错过补跑（默认 2 小时）、参数快照、`QLockFile` 单实例执行；配置存 `data/analysis_schedule.json` |
| 会话记录 | 改为分析历史登记簿（`data/history/sessions.json`）：每次分析落一条记录，按结论/设备/数据目录筛选，可回看详情、重跑、删除；只存路径与统计，报告被移动时标灰而不是隐藏 |
| 日志导出页 | 导出完成后自动把目录预填到「日志分析」页；新增「先压缩再导出 zip」与「只导新增日志」（app/SDK 按上次成功导出起点增量，手机/固件仍全量）；移除导出后自动导入/出报告（改由分析页统一完成） |
| 依赖 | 移除 matplotlib；新增 `lz4`、`zstandard`（MCAP 压缩流），依赖总量约 150MB |
| 清理 | 删除旧的会话数据库、Trace 查询、结果 CSV 导出与独立 CLI；修正帧截取分支中未定义的 `_log` 调用；移除仍调用已删除 `import_pulled` 的旧 SFTP 导出页 |

测试：`python -m unittest discover -s tests -p "test_*.py"` 共 **194** 项通过（新增 `test_analysis_schedule.py` 定时调度、`test_history_store.py` 历史登记簿、`test_export_state.py` 增量起点、`test_mobile_fw_picker.py` 固件设备勾选、`test_report_problem_parts.py` 问题 Part 汇总与定位用例）。

### v1.0.9（2026-11-10）缺陷修复

| 编号 | 现象 | 修复 |
|---|---|---|
| T1 | 导出因手机掉线失败（`adb: error: failed to get feature set: device 'XXX' not found`）后，进度条停在失败那一刻的百分比；点「取消任务」只往日志写一句「当前没有运行中的导出任务」，进度条永远卡着 | 失败处理只写日志、从不复位界面，而 `_stop()` 在 worker 已结束（`isRunning()` 为假）时不走任何复位分支。现 `_on_export_fail` / `_on_pack_fail` 与取消、完成一样清空进度条（`_reset_progress`，同时丢弃节流缓存），`_stop()` 在无运行任务时也执行复位并恢复按钮 |
| T2 | 点「取消任务」后要干等很久才停：`adb pull` 单次最多阻塞 900 秒，只在调用前后检查取消标志，进度条长时间不动 | `mobile_adb._run` 增加 `cancel_event` 分支：改为 `Popen` + 300 ms 轮询，取消即 `terminate`（超时兜底 `kill`），worker 立刻收到取消。取消事件贯穿 `pull_all` → `latest_zip` / `_pull_one_dir` / `_pull_firmware_flat` / `_adb_pull_to`，不会再有"点了取消还在拉"的窗口 |
| T3 | 取消或失败后，输出目录里留下一个只装了一半的 `EGO_export_<时间戳>/`，下次导入/人工检查会把它当成一次真实导出 | 新增 `EgoOneClickExportWorker._discard_partial`：取消/失败时删除**本次**创建的目录（只动这一个，不碰输出根目录下别的产物），诊断日志在输出根目录不受影响 |
| T4 | 不勾「EGOViewer 手机日志」做 USB 导出，设备目录没直接放在根目录，而是被并进了输出目录里某个机型文件夹，看起来像「整个 firmware 都被删了」 | USB 固件与手机日志是两套逻辑，此前混在一起：`_resolve_device_dir` 不管本轮是否拉了手机，都会把 USB 产物并进 `<机型>/<SN>/`。输出目录若是上一次的手机导出目录，历史目录就把三台设备吞了。现加 `skip_mobile_serials` 开关：勾了手机 → 并入手机目录（同一台设备只出现一次）；没勾手机 → 一律新建在导出目录根下 `<SN>/`，历史手机目录原样不动。同时修正匹配规则：手机目录名是 `<SN>-<时间戳>-<hash>`，早先按裸 `<SN>` 精确匹配永远找不到，勾了手机也会在根目录多出一份 |
| — | 「固件设备」与「手机日志」隔着一行「日志类型」，看不出两者是一组 | UI 行序调整为 **手机日志 → 固件设备 → 日志类型**。这一行只服务手机日志（PC 端补齐手机连不着的指环/手表等从设备），放在手机日志正下方；启用条件同步改为「勾了手机日志 或 勾了设备固件日志」。手选设备同样作用于「打包导出」 |
| T5 | 只插了 2 台 USB，导出却拉了 5 台：框里留着配网 IP，SDK 就按「同网段」把 192.168.1.x 上的以太网设备也一并拉了 | 用户要求：「电脑 USB 识别到的设备导出，非 USB 识别的不导出」。以太网/Wi-Fi 上的设备属于谁，SDK 信息判断不了（同网段既有自己的从设备也有别人机器的设备），靠网段/延迟/历史都只能是猜，结果时多时少。现在**自动模式只导 USB 直连设备**，以太网/Wi-Fi 一律不导并在日志里列出被跳过的设备；配网 IP 只影响拉取顺序，不再决定拉几台。手动勾选仍是明确指定，不受此限（勾了十几台也一样受以太网限额保护）。一台 USB 都没有（纯网线现场）时退回导出全部可访问设备，避免结果为空 |
| T6 | 某台设备报 `Request failed, statusCode: 8, msg: Device response size(0) < response header size(10), propertyId: 1000`，日志里只有这一句英文，看不出该查什么 | 这是 SDK 原样抛出的**设备通信失败**（设备回了 0 字节，连 10 字节响应头都没有），不是本工具的报错——同一台电脑、同一个 SDK，实测有的设备能导有的导不了；EGOViewer 自己的日志里也用同样的串报过 `device_enumeration_entry_unavailable`，并注明「设备可能已离线」。现在：① 新增 `unresponsive` 错误类型（关键字 `device response size` / `unreachable_exception` 等），界面给出「重新插拔/断电重启设备、确认 EGOViewer 没占用、网线场景确认能 ping 通」的可操作提示，并保留原文便于贴给研发；② 这类**瞬态**错误自动重试一次（间隔 2 秒，可被「取消任务」中断），设备被占用 / 固件日志关闭 / 缺 DLL 等稳定失败不重试，避免白等 |
| T7 | 勾了「手机日志」后，手机导出包里的**从设备**（指环 / 手表）日志看不到：只搬回一个 zip，要人自己解压再翻 `firmware/` | `mobile_adb.pull_all` 拉完 zip 后新增 `extract_bundle_devices`：把包内 `firmware/<SN>-<时间戳>-<hash>/` 的**每台设备**解到 `<机型>/<SN>-<时间戳>-<hash>/` 平铺，不再套 `firmware/` 与「固件日志/」两层；手机上残留的 `firmware_logs/` 副本同样平铺。落地文件逐个走 `_safe_extract_member` 拒绝 zip-slip 路径。zip 仍原样保留一份备查；`summarize` 改为先认平铺设备目录、再读 zip 补报未展开的分组，旧结构（`firmware/`、`固件日志/`）照样能清点 |
| — | 「手机日志」在会话里被识别成「电脑端导出」 | `session_view` 增加两种手机目录特征：新包名 `egoviewer-logs-*`（旧名 `ego-logs-app-*` 继续认），以及平铺后的 `<机型>/<SN>-<YYYYMMDD_HHMMSS>-<hash>/` 分组（排除 `app/`、`sdk/`、`firmware/` 这类电脑端目录） |
| — | BLE 扫不到设备 / 要等约 10 秒 | ① 前缀匹配兼容真实广播名：`EGO-4B_AZL…`、`UMI_Finger-R_…`、`WristCam-R-R_…`（早先只比对裸前缀，这类设备被过滤掉，表现为「设备在，列表里没名字，重启才出现」）；② 名称允许为空——地址属于 EGO OUI（`54:14:FD`）的广播不再丢弃（BLE 首包常只带 MAC），同地址后续广播补上名称；③ 新增 `scan_devices_robust`：单轮 2 秒、**有结果立即返回**（实测 10s → 2.1s），只有一台都没扫到才依次补扫、放宽信号下限、关闭名称白名单（每步写日志说明） |
| — | 「配置 Wi-Fi 并添加设备」要等约 20 秒 | 主因是**直接 connect 走慢速服务发现**：实测直连 17.4 秒、先扫 2 秒再连只要 2.2 秒。连接前先做一次预热扫描（失败不阻断），IP 轮询改为短超时（1.2 秒）+ 0.35 秒高频（`get_ip` 实测 0.1~0.2 秒），连接超时 20s → 15s。整体 17.6s → 4.3s |
| — | 以太网设备一网段七八台，导出被拖到几分钟；自动挑中的「不是我想要的」 | 新增 `FIRMWARE_MAX_NETWORK_DEVICES`（默认 3）：以太网设备限台数，USB 直连不受限、手机已覆盖的不占名额。挑选改为**历史优先**——`data/firmware_devices.json` 记录每台设备成功导出次数与时间（`load_device_memory` / `remember_devices`），次数多、最近导过的排前面，无历史的才按 ping 延迟补位（新增 `wifi.ping_ms`，同一交换机内几毫秒差异纯属抖动，不能当距离用）；配网设备始终保底入选 |
| — | 想精确指定导哪几台设备 | 「日志类型」上方新增**固件设备**行：点「选择设备…」列出在线设备（含 ping 延迟）与导出历史，勾选后只导这些（`only_serials`）；「恢复自动」清除手动选择。手选在「打包导出」同样生效，勾了但已离线的设备会在日志里点名 |
| — | 勾选框只剩下一条灰细线 / 一个 ✓，看不出「这里能勾」 | v1.0.8 把 indicator 交回系统后，Windows 11 原生样式（PySide6 6.8 默认）本就不画方框。现由 QSS 显式绘制：白底 + 1px `#a1a8b3` 灰边 + 3px 圆角，勾选时框内叠加深色 ✓；图案是随包分发的 `assets/check.svg`，构建时由 `_build_style()` 注入绝对路径（QSS `url()` 不认 `data:` URI，实测静默失效）；勾选态不改方框底色，不会退回蓝底实心块，深色调色板下像素一致。新增 `ego_gui/resources.py` 负责资源定位 |
| — | 同一台设备被手机与 USB 各导一份，目录里出现带时间戳后缀的重名文件 | 手机先拉，USB 拉取时跳过手机已覆盖的序列号（`skip_serials`），并在日志里说明跳过的是哪几台 |

测试：新增 `tests/test_cancel_progress.py`（失败/取消复位进度条、`_stop` 在无任务时复位、半成品目录只删本次）与 `tests/test_ble_identify.py`（真实广播名前缀、无名称按 OUI 保留、补扫与放宽顺序）；`test_unified_export.py` 增加「未勾手机时历史手机目录不得吞掉 USB 产物」「勾了手机时 USB 产物并入手机目录」两例；`test_orbbec_sdk.py` 增加「只导 USB」「瞬态错误重试一次 / 只重试一次 / 稳定错误不重试」「历史优先与以太网限额」「手动勾选只导所选」；`test_mobile_adb.py` 增加导出包设备展开与 zip-slip 拒绝；`test_fixes_v106.py` 增加 `unresponsive` 归类与提示；`test_theme_pinning.py` 增加 indicator 绘制覆盖。全套 **206** 项通过

### v1.0.8（2026-10-22）缺陷修复

| 编号 | 现象 | 修复 |
|---|---|---|
| T5 | 勾选框勾上之后变成**蓝色实心方块**，与之前的原生样式不一致（首发 1.0.8 构建的回归） | 首发构建新增的 `QCheckBox::indicator:checked` 把方框整体填成 `#2563eb`，覆盖了系统原生的「白底 + 深色 ✓」。现删除全部 `::indicator` 自定义，方框与勾选标记交回系统绘制；只保留 `QCheckBox, QRadioButton { background:transparent; color:#222; }`，用于把背景钉在浅色。实测浅色/深色调色板下渲染逐像素一致，勾选标记正常显示 |
| T1 | 多设备配对后一键导出，`firmware/` 下三台设备的日志内容**完全一样**（MD5 一致），实际只拉到了其中一台 | `export_all_firmware_logs` 原先只把 `dev["index"]` 传给单台导出，而单台导出在未指定 IP 时按「设备列表第 N 条」取设备；USB 设备 IP 字段为空，三次都走了这条路径，而 SDK 枚举顺序在多次 query 之间并不稳定。现改为把**序列号**一路传下去精确选设备（序列号 → IP → index 三级匹配），命中后用 `_device_info` 复核序列号，不符直接对该台报错，绝不把 A 设备的日志写进 B 设备的目录 |
| T2 | 同型号多台设备的固件日志挤在同一目录，分不清谁是谁 | 目录名由「设备名」改为 **`设备名_序列号`**（如 `UMI Finger_BF8M960008D`）；`device_tab` 落盘时再兜一层序列号校验，同名设备不可能落在同一路径 |
| T3 | 客户机点「重新检查」始终扫不到最新的 EGOViewer 版本 | 原先只扫写死的候选目录（本工具所在盘、各盘根、少数工具目录）且深度仅 2 层，装在别的盘或更深层目录的新版本根本不在范围内。现新增**全盘扫描**：枚举所有本地固定磁盘，广度优先查找 `EGOViewer_vXXXX` 目录（最深 6 层，跳过 Windows/Temp/回收站等，单次上限 25 秒，可用 `EGO_VIEWER_SCAN_BUDGET_S` 调整）。排序改为**版本号 → 构建时间（`_202610202306`）→ 名称**，最新版本固定排第一；过滤临时/备份/副本目录里的同名残留 |
| — | 「重新检查」点完没有任何反馈 | 按钮改为「重新检查」，点击后弹窗列出找到的所有版本、标明哪个最新、当前选中哪个；运行日志同步逐条列出各版本路径 |
| T4 | 客户机开深色模式时，日志勾选框与「选项」勾选框勾选后跟着系统配色走，出现深色小方块 | 整套界面是手写浅色 QSS，但从未声明「只支持浅色」，且 QSS 里**没有任何 `QCheckBox` 规则**——方框按系统面板配色绘制，深色模式即变深。新增 `_pin_light_appearance()`：启动时 `styleHints().setColorScheme(Light)` **并**显式重置应用调色板（只调前者不会把已灌入的深色调色板改回来，实测仍为深底），老版本 Qt 走标准浅色调色板兜底；同时在 `STYLE` 中补齐 `QCheckBox`/`QRadioButton`（勾选态用纯色而非 Qt 内部图片资源）、`QComboBox` 下拉弹层、`QMenu`、`QToolTip`、`QScrollBar` 的显式配色 |
| T6 | 勾选框**根本没有方框**：未勾选时只剩一条灰细线，勾上后只是一个深色 ✓，白卡片上几乎看不出「这里能勾」 | 上一版把勾选样式交回系统后，Windows 11 原生样式（PySide6 6.8 的默认样式）本就不画方框，问题并未解决。现由 QSS 显式绘制 indicator：白底 + 1px `#a1a8b3` 灰边 + 3px 圆角，勾选时框内叠加深色 ✓。勾选图案是 `assets/check.svg`，构建 STYLE 时注入绝对路径——`url()` 不认 `data:` URI（实测静默失效，勾选态与未勾选态渲染完全一致）。勾选态**不改方框底色**，不会退回蓝底实心块；边框与勾选色写死在 QSS，深色调色板下像素逐像素一致 |
| — | 导出失败现场只剩一句弹窗文案，重启后无从追溯 | 启动器安装 `sys.excepthook`，未捕获异常连同堆栈写入 `<exe 同级>/log/export_debug.log`；导出/打包失败均追加 `export_diag.log` 堆栈记录 |

测试：新增 `tests/test_theme_pinning.py`（浅色钉死 + QSS 覆盖度）；`test_fixes_v106.py` 增加版本排序与多设备隔离用例；全套 **167** 项通过。

发布包：`EGO_Courier_portable_v1.0.8_win64.zip`（Python 3.12.10 + PyInstaller 6.22.3 构建，
84 579 918 字节）。按仓库约定 `dist/` 与 `*.zip` 不入库，通过 Release 附件分发。
安装前请注意：便携版必须**整目录解压**，只替换 exe 会因缺少 `runtime/` 而启动失败。

### v1.0.7（2026-10-21）缺陷修复

| 编号 | 现象 | 修复 |
|---|---|---|
| T1 | 明明选了包含固件日志的时间段，导出包里 firmware 仍为空 | `filter_firmware_pull` 不再拿「点按钮那一刻」去和 `[start, end]` 比对：固件日志是设备端当前全量快照，拉取时刻必然晚于用户设定的「止」（出问题 → 调时间段 → 点导出），原来只放宽 2 秒会把整批剔除。现改为只校验起点（`pulled_at >= start`），止侧不限；起点晚于拉取时刻仍剔除并提示 |
| T2 | 任务进度条走系统原生绿色样式，与同行按钮不同高 | `app.py` 的 `STYLE` 补 `QProgressBar#taskProgress` 规则（白底浅灰槽 + `#3b82f6` 蓝色填充、4px 圆角）；进度条与三个按钮统一 `setFixedHeight(34)` |
| T3 | 进度条会往回跳、界面卡顿 | 各段占比收敛到一张 `PROGRESS_STAGES` 表（firmware 0–55 / stage 55–80 / mobile 80–92 / pack 92–96 / import 96–100），不再各自硬编码 `50 + pct*0.5`、`60~100`；`pack_staged` 的合并进度按份额表拆回 stage / pack；UI 侧加 `_ThrottledProgress`（丢回退、16ms 合帧、0/100 立即生效） |
| T4 | 本机只插 3 台 USB，导出却把局域网里别人的设备日志也全拉了一遍 | 「设备 IP」框非空曾让 `include_network=True`，`select_devices` 于是返回「USB + 全部网络设备」。现 UI 固定传 `include_network=False`，IP 只作为 `prefer_ip` 精确命中那一台（该命中不受 USB 优先筛选限制）。「没插 USB 时能导 Ethernet」由 `select_devices` 的无 USB 兜底保证，不依赖放开枚举。实测本机枚举 6 台、只导 3 台 USB |

### v1.0.6（2026-10-21）缺陷修复

| 编号 | 现象 | 修复 |
|---|---|---|
| T1 | 「重新检测」永远找不到 EGOViewer | 搜索根改为动态推导（环境变量 `EGO_VIEWER_ROOT` → 本工具所在盘 → 常见工具目录**及下一层**）；判据改为内容特征（`EGOViewer.exe` / `Log/EGOViewer_*.log` / 数据目录），不再依赖 `data/logs` 与 `win_x64` 字样；app 日志按 `Log → logs → data/logs` 查找 |
| T2 | Wi-Fi 密码错误时配网失败无任何提示 | `configure_wifi` 校验设备返回的 `result`，被拒时直接抛 `BleError`；等待 IP 超时改为 `failed` + 红色弹窗；状态栏实时显示 `已等 N/40 秒` |
| T3 | 固件日志报「未找到 OrbbecSDK.dll」 | `_candidate_dlls` 增加回退链（本工具 `runtime/lib` → EGOViewer 安装目录 → 搜索根）；SDK 目录直接沿用「EGOViewer 根目录」（DLL 随 EGOViewer 安装，不再单列输入框），导出前预检 DLL |
| T4 | 配网后的纯网络设备被静默跳过 | `device_host` 真正传给 SDK：`export_all_firmware_logs` 新增 `prefer_ip`/`prefer_port` 按 IP 精确定位设备，`include_network` 随配网 IP 自动开启；导出前 ping 目标设备 |
| T5 | 固件日志缺时间戳层、重复导出互相覆盖 | 先拉到 `_staging`，再按 `firmware/<设备>/<YYYYMMDD-HHMMSS>/` 重组落盘，`rel` 与磁盘严格一致；同名同秒加时间戳后缀 |
| T6 | 「找不到 DLL」被报成「设备未连接」 | `OrbbecSdkError` 增加 `kind`（dll / no_device / busy / fw_disabled / timeout / canceled），界面按类型给不同排查提示 |
| — | 扫描列表混进鼠标/耳机等无关蓝牙设备 | 按名称前缀（`EGO` / `UMIFinger` / `UMI_Finger` / `WristCam` / `EGOPro` / `UMIGripper` / `EGODepth`，允许其后接 `_`、`-` 或结束，故 `EGO-4B_AZL…`、`UMI_Finger-R_BF8…` 都能认）与 OUI `54:14:FD` 过滤，并按地址去重 |
| — | 设备在线却扫不到名称，重启设备才出现 | 三处修正：① 前缀匹配兼容 `EGO-4B_`、`UMI_Finger-R_` 等真实广播名（早先只比对裸前缀被过滤掉）；② 名称为空但 OUI 属于 EGO 的广播不再丢弃（BLE 首包常只带 MAC）；③ 无结果时逐级放宽信号下限/名称过滤重试 |
| — | BLE 扫描响应慢（约 10 秒） | EgoLowBle 扫描是「阻塞到超时才返回」，改为单轮 2 秒、**有结果立即返回**（实测 10s → 2.1s）；只有一台都没扫到才补扫/放宽 |
| — | 「配置 Wi-Fi 并添加设备」巨慢（约 20 秒） | 主因不是等 IP，而是**直接 connect 要走慢速服务发现**：实测直连 17.4 秒、先扫 2 秒再连只要 2.2 秒。改为连接前先做一次预热扫描；IP 轮询也改成短超时（1.2s）高频（`get_ip` 实测 0.1~0.2 秒）。整体 17.6s → 4.3s |
| — | 勾手机日志后从设备（指环/手表）日志缺失 | ① 手机与 USB/网络两条来源**都拉**（原实现「勾了手机就跳过 USB」）；② `prefer_ip` 改为「优先排序」而非「只拉这台」；③ 显式纳入**与配网 IP 同一 /24 网段**的网络设备（从设备常只走以太网），跨网段别人的设备仍跳过 |
| — | 以太网设备太多导致导出过慢 | 以太网设备最多取 3 台（`FIRMWARE_MAX_NETWORK_DEVICES`）。USB 直连设备不受此限；手机已含的设备不占名额。实测 7 台 → 3 台，整次导出 230s → 121s |
| — | 自动挑的 3 台「不是我想要的」 | 改为**历史优先**：记录每台设备成功导出的次数与时间（`data/firmware_devices.json`），次数多/最近导过的排前面，无历史的才按 ping 延迟补位（同交换机内延迟差几毫秒纯属抖动，不能当距离用）；配网设备始终保底入选 |
| — | 想精确指定导哪几台设备 | 「日志类型」下方新增**固件设备**行：点「选择设备…」列出在线设备与导出历史，手动勾选后只导这些；「恢复自动」清除手动选择 |
| — | 同一台设备被手机与 USB 各导一份，目录里出现带时间戳后缀的重名文件 | 手机先拉，USB 拉取时跳过手机已覆盖的序列号（`skip_serials`） |
| — | 同一设备三份几乎相同的日志 | 手机导出包内同一 SN 的多个历史快照只解**最新**一份 |
| — | SDK 日志出现两份 | 不再解包内顶层 `sdk/`（陈旧副本），只保留实时拉取的 `<机型>/sdk日志/` |
| — | 导出目录「东放一个西放一个」 | 统一为 `<导出目录>/<SN>/`（USB 与手机一致），手机侧为 `<导出目录>/<机型>/<SN>/`；去掉 `firmware/` 与拉取时刻两层 |
| — | 「设备 IP」预填 `10.9.82.9` 等测试值误导用户 | `DEVICE_IP` / `EXAMPLE_DEVICE_IP` 改为空，由配网结果或用户填写 |
| — | firmware 分类把 `.lock`/`dmesg.txt`/`device_status.txt` 也收进包 | 改为按 `orbbec*.log*`、`messages.log*`、`observice.console.log*` 精确匹配 |
| — | 便携版 `runtime/lib/EgoLowBle.dll` 可能找不到 | `find_dll` 增加 `<exe>/runtime/lib/` 探测 |

## 已知限制 / 待办

- 日志分析需要目录内含可识别的采集数据（相机/IMU/夹爪 CSV、MCAP、WAV + 配套 CSV 或 meta_*.json）；纯 app/sdk 文本日志目录会被判定为无可分析分段。
- 帧截取（不同步帧 / 同步帧图片）与 SEI 时间戳提取需要本机安装 ffmpeg/ffprobe；缺失时该两项自动跳过并提示，其余检查照常执行。
- MCAP 压缩流分析需要 lz4 / zstandard / mcap 依赖完整；缺失时仅 MCAP 视频分析不可用。
- 分析参数与报告中「单设备 / 多设备」的配对口径以选定值执行，不按设备组合白名单或包名时间窗口自动配对。
