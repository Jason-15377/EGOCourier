# 需求请求：安卓 / iOS 端 App 日志实时拉取功能

> 提报人：测试
> 提报日期：2026-10-12
> 仓库地址：**https://github.com/Jason-15377/EGOCourier.git**（最新，分支 `main`，最新提交 `1d76e59 EGO Courier v1.0.1`）

---

## 1. 背景

目前 EGO Courier 的日志采集能力集中在 **Windows 桌面端**（PySide6 GUI）：

- **② EGOViewer 日志导出**：app / sdk 从本机 EGOViewer 安装目录（`data\logs`、`Log`）收集，firmware 通过 Orbbec SDK 从设备实时导出。
- **勾选导出**：从本机 EGOViewer 目录打包三类日志。
- **设备 SSH 拉取 app 日志**：通道当前**暂未开通**（前端分组已隐藏，`ego_relay/sftp_pull.py`、`sftp_client.py` 后端代码保留，待后续启用）。

问题在于：**采集必须回到电脑上跑**。测试同学在现场复现问题时，手机（EGOViewer App 运行端）拿不到实时日志，只能事后回电脑上翻目录、打包、再分析，导致：

1. 现场复现的问题**无法当场取到日志**，事后日志可能已被覆盖 / 轮转，或设备已断电，问题无法复盘。
2. 手机端 App 的日志（Android `logcat` / iOS `os_log`）目前**完全没有采集通道**，只有 EGOViewer 应用自身写出的日志文件，缺少系统级日志与实时崩溃 / ANR 信息。
3. 报缺陷（TB）时需要附日志，手边没有电脑时无法提供。

## 2. 需求目标

开发 **Android / iOS 端 App 日志实时拉取功能**：让测试同学在手机上直接、实时地拉取并导出日志，无需依赖 PC。

核心诉求（按优先级）：

| 优先级 | 诉求 | 说明 |
|---|---|---|
| P0 | **实时拉取** | 手机端 App 运行时的日志能**边跑边取**（流式），不是只在结束时导出文件 |
| P0 | **一键导出** | 手机上操作导出，产物可直接分享 / 上传（zip 或文本）到 TB 缺陷单 |
| P0 | **与现有分析链路对齐** | 导出的日志格式 / 目录结构与桌面端 `app/ sdk/ firmware/` 分类兼容，能直接被 EGO Courier 导入分析 |
| P1 | **时间戳标注** | 每行日志带统一时间戳，便于与硬件 PTP / IMU / MP4 帧时间戳对齐（这是 EGO Courier 的核心能力，移动端日志必须能对上时间轴） |
| P1 | **崩溃 / ANR 捕获** | 系统级 crash、ANR（Android）、crash log（iOS）一并采集 |
| P2 | **阈值告警** | 现场实时标记超时 / 丢帧异常（对齐桌面端阈值分析） |
| P2 | **日志分级过滤** | 现场只看 ERROR 及以上，减少噪声 |

## 3. 建议实现方向（供开发参考，非强制）

### 3.1 Android

- **采集层**：优先走 `logcat`（可用 `Runtime.exec` / `ProcessBuilder` 读取 `logcat -v threadtime`），或 App 内自建日志缓冲（若目标 App 与采集 App 可集成）。
- **实时性**：前台 Service + 通知，保证后台不被杀；流式写入本地文件（滚动切片，避免单文件过大）。
- **权限**：`logcat` 读取在 Android 10+ 受限于应用沙箱，建议：
  - 方案 A：**采集能力内嵌进 EGOViewer App**（同进程直接写日志，无权限问题）—— **推荐**；
  - 方案 B：独立采集 App，需 root 或 `adb` / 无线调试授权，现场成本高。
- **USB / 无线调试**：可作为 PC **未在场**时的补充，PC 在场时仍可走现有 adb 通道。

### 3.2 iOS

- **采集层**：App 内 `os_log` / `OSLogStore`（iOS 15+ 可按时间范围读取本 App 及系统日志）；或集成开源方案（如基于 `OSLogStore` 的日志浏览器）。
- **实时性**：App 内日志写文件 + 前台卡片实时刷新；系统日志受沙箱限制，只能读到本 App 域。
- **导出**：`UIActivityViewController` 分享 zip / 文本；支持 AirDrop、微信、邮件。

### 3.3 与 EGO Courier 的对接

- 移动端导出包建议沿用桌面端目录结构：

```
EGO_mobile_export_YYYYMMDD_HHMMSS/
├── app/          # App 自身日志（含系统 logcat / os_log 合并）
├── sdk/          # SDK 日志（若 SDK 在手机侧输出）
└── firmware/     # 设备固件日志（如手机可经 BLE / WiFi 中转拉取）
```

- 导出 zip 命名建议与桌面端一致：`EGOViewer_logs_YYYYMMDD_HHMMSS.zip`（或加 `_android` / `_ios` 后缀区分平台）。
- 时间戳格式对齐桌面端解析器（见 `ego_relay/parsers.py`），确保 `ego import` 能直接吃下移动端导出的包。
- 可复用桌面端已有的 **BLE 配网**（`ego_relay/ble_provision.py`、`egolowble.py`）与 **WiFi**（`wifi.py`）逻辑思路，在移动端实现设备发现 / 连接。

## 4. 验收标准（建议）

1. 手机 App 运行时可**实时**看到新增日志（延迟 ≤ 1s）。
2. 一键导出 zip，包含 app / sdk / firmware 分类目录（缺失分类可空），文件名带时间戳。
3. 导出的 zip **可直接用 EGO Courier 桌面端导入并分析不报错**（`ego import <zip>` 通过）。
4. 每行日志带可解析的统一定时时间戳，能与硬件 PTP 时间戳对齐。
5. crash / ANR 能自动捕获并落盘，导出包内含。
6. Android 覆盖主流机型（Android 10+），iOS 覆盖 iOS 15+。

## 5. 相关代码位置（仓库内）

| 模块 | 路径 | 说明 |
|---|---|---|
| 桌面 GUI 设备页 | `ego_gui/device_tab.py` | 三类日志导出 UI 逻辑 |
| 日志打包 | `ego_relay/evlog_pack.py` | 收集 / 打包三类日志 |
| SSH 拉取（保留待启用） | `ego_relay/sftp_pull.py`、`sftp_client.py` | 设备侧拉取通道，可参考 |
| Orbbec 固件日志 | `ego_relay/orbbec_sdk.py` | 复用 `OrbbecSDK.dll` 实时拉固件日志 |
| BLE 配网 | `ego_relay/ble_provision.py`、`egolowble.py` | 移动端可复用思路 |
| 日志解析 | `ego_relay/parsers.py` | 时间戳格式对齐基准 |
| 项目文档 | `docs/项目文档.md` | 架构与协议说明 |

## 6. 请求

麻烦开发同学**有空的时候**评估并排期开发，如上实现方向有更优方案欢迎直接调整。优先落地 **P0（实时拉取 + 一键导出 + 与现有分析链路对齐）**，其余可分期。

仓库最新地址：**https://github.com/Jason-15377/EGOCourier.git**
