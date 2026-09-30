# EGO Courier · 桌面端分支说明

本分支面向 **PySide6 桌面 GUI 版**（`start_gui.bat` / `python run_gui.py`），与 `main`（网页版+CLI）并存。
Web 版走 `start.bat`（http://127.0.0.1:8360），命令行走 `ego`（`ego.bat`）。

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
start_gui.bat        :: 或 python run_gui.py
```

## 分支约定
- `main`：完整版本（Web + 桌面 + CLI）。
- `desktop`：桌面 GUI 专属分支，基于 `main`，桌面端改动先合到这里。
