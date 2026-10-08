# EGO Courier · 桌面端分支说明

本分支**只保留桌面端**：PySide6 桌面 GUI（`EGO Courier.bat` / `python run_desktop.py`）与命令行工具 `ego`（`ego.bat`）。
原网页端（`start.bat`、`run_server.py`、`EGO Courier Web 服务`、`ego_relay/web/`）已从本分支删除。

## 桌面端能做什么

| 页签 | 功能 |
|---|---|
| 日志分析 | 导入 EGOViewer 本地日志/目录/zip，四类时间戳对齐 + 异常检测 + 可视化（丢帧/双目同步/SEI 比对/IMU 同步） |
| 会话记录 | 会话列表、多选删除/对比、导出汇总报告 |
| 设备与日志 | ① 蓝牙配网 + ② 单一合并分组「设备实时拉取 · 本地打包」 |
| 阈值 / Trace 查询 | 阈值配置、trace_id 检索 |

## 「设备与日志」页（② 合并分组）

一个分组内包含两类导出能力：

- **SSH 设备实时拉取 app 日志**：填写设备 IP/用户名/密码 + 远端 app 目录，勾选「app 日志(SSH/SFTP 拉取)」后触发。
- **EGOViewer 三类日志导出**：自动识别本机 EGOViewer 根目录，按勾选分类收集 app/sdk/firmware 日志。
  - **「开始一键导出」**：识别根目录 → 输出目录下新建全新时间戳目录 `EGO_export_<时间戳>/`（内按 `app/ sdk/ firmware/` 子目录），
    勾选 **firmware** 时通过 **Orbbec SDK** 从设备实时拉取最新固件日志（等价 EGOviewer「导出设备日志」，设备需在线、离线整体报错）→ 自动导入分析 + HTML 报告。
  - **「打包导出」**：按筛选（时间段 / 手动勾选文件）把三类日志打包成日期命名的 zip。

> 分类勾选框默认全勾；`data/`、`log/` 等运行数据不入库（见 `.gitignore`）。

## 安装与启动

```bat
:: 依赖（一次）
python -m pip install PySide6 matplotlib imageio paramiko

:: 启动桌面 GUI
EGO Courier.bat      :: 或 python run_desktop.py
```

## 打包成 .exe（另一版启动方式）

除源码启动外，可把桌面端打成**独立 .exe**，目标机器无需安装 Python / PySide6，双击即用：

```bat
:: 默认 onedir：出 dist\EGO Courier\EGO Courier.exe（启动快，推荐分发整个目录）
build_exe.bat

:: 单文件：出 dist\EGO Courier.exe（约 80 MB，首次启动需解压，稍慢）
build_exe.bat onefile
```

`build_exe.bat` 会依次检查/安装 PyInstaller、重新生成标志资源、再调用
`ego_courier.spec` 打包。也可手动执行：

```bat
python -m PyInstaller --noconfirm --clean ego_courier.spec
```

**两种形态的取舍**（PyInstaller 官方建议：能用 onedir 就用 onedir）：

| | onedir（默认） | onefile |
|---|---|---|
| 产物 | `dist\EGO Courier\`（约 190 MB 目录） | 单个 `dist\EGO Courier.exe`（约 80 MB） |
| 启动 | 快 | 慢，每次启动要把内容解压到临时目录 |
| 杀软误报 | 少 | 相对更多 |
| 适合 | 内部分发、拷贝整个目录 | 单文件转交、临时试用 |

**打包相关说明**

- **图标**：`assets\ego_courier.ico` 为 10 帧多分辨率图标（16/20/24/32/40/48/64/96/128/256），
  同时写进 exe 资源（资源管理器/任务栏/开始菜单）并由程序在运行时加载为窗口图标。
- **数据目录**：源码模式数据在项目根的 `data\`；打包后默认放在 **exe 同级目录的 `data\`**，
  随程序目录整体迁移。如需指定别处，启动前设置环境变量 `EGO_DATA_ROOT=<目录>`。
- **ffmpeg / ffprobe 不打进包**：只在做视频分析（MP4/MCAP）时需要，程序启动时会自动从
  PATH 或 winget 安装目录探测；缺失时界面会提示安装方式，日志/CSV 分析不受影响。
- **OrbbecSDK.dll 不打进包**：固件日志实时导出时，从用户机器上已装的 EGOViewer 目录里查找，
  避免随包分发造成 SDK 版本错配。
- **UPX 关闭**：压缩会显著提高杀软误报率，spec 中已禁用。

## 标志（Logo）

标志生成脚本：`assets\make_logo.py`（依赖 Pillow）。改了配色/造型后重新生成：

```bat
python assets\make_logo.py
```

设计要点：深蓝圆角方底板 + 两道朝右上的箭头（白色主箭头 + 青色次级箭头），
箭头取「Courier 送达」语义，尾部短横线抽象三类日志（app/sdk/firmware）；
低分辨率（≤32px）自动走简化路径去掉短横线，保证任务栏 16px 下仍清晰可辨。

产出：

| 文件 | 用途 |
|---|---|
| `assets\ego_courier.ico` | 多分辨率 Windows 图标（exe 资源 + 窗口/任务栏） |
| `assets\logo.png` / `logo_512.png` | 512px 主标志（文档 / 关于页） |
| `assets\logo_<n>.png` | 16–256 各尺寸透明底 PNG（界面内小图标用） |

> 若 `assets\` 缺失（例如只拷了 `.py` 文件），程序会自动退回代码内绘制的矢量图标，
> 窗口图标不会为空。

## 分支约定
- `main`：完整版本（Web + 桌面 + CLI）。
- `desktop`：桌面 GUI 专属分支，基于 `main`，桌面端改动先合到这里（本分支已移除网页端）。
