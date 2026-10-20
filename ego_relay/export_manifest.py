"""导出目录的来源清单 manifest.txt。

导出包按「谁产生的」分成两大类，开发拿到目录先看这一层：

    电脑端/           电脑上运行的 EGOViewer / Orbbec SDK 产出
      app/ sdk/ firmware/
    手机端/<机型>/     手机 EGOViewer 导出后经 adb 搬回
      手机导出包.zip  sdk日志/  固件日志/<SN>-<时刻>/

清单把每条来源的通道、设备、时间与文件数写清楚，并在开头附目录结构说明，
不看界面也能明白每个文件夹是什么。
"""


from __future__ import annotations

import os
from datetime import datetime
from typing import Dict, List, Optional

MANIFEST_NAME = "manifest.txt"


def _fmt_ts(value) -> str:
    if not value:
        return "未记录"
    try:
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return str(value)


def _count_files(root: str) -> int:
    if not os.path.isdir(root):
        return 0
    return sum(len(fs) for _dp, _ds, fs in os.walk(root))


def _group_time(group: str) -> str:
    """手机侧分组名形如 <SN>-20261020_100206_680-<hash>，取其中的时间部分。"""
    import re
    m = re.search(r"-(\d{8})_(\d{6})_", group or "")
    if not m:
        return "未记录"
    d, t = m.group(1), m.group(2)
    return f"{d[:4]}-{d[4:6]}-{d[6:]} {t[:2]}:{t[2:4]}:{t[4:]}"


def write_manifest(export_dir: str, *, keys: List[str], export_ts: str,
                   pc_firmware: Optional[Dict] = None,
                   mobile: Optional[Dict] = None,
                   app_sdk_counts: Optional[Dict[str, int]] = None,
                   sdk_root: str = "") -> str:
    """在导出目录写 manifest.txt，返回其绝对路径。

    pc_firmware: {"at": 时间戳, "devices": {设备目录: 文件数}}，未拉固件时传 None
    mobile:      mobile_adb.summarize() 的结果
    """
    lines: List[str] = []
    lines.append("EGO Courier 导出清单")
    lines.append("=" * 60)
    lines.append(f"导出时间：{export_ts}")
    lines.append(f"导出目录：{os.path.abspath(export_dir)}")
    lines.append(f"勾选来源：{' / '.join(keys)}")
    if sdk_root:
        lines.append(f"EGOViewer 根目录：{sdk_root}")
    lines.append("")

    lines.append("目录结构：")
    lines.append("  EGO_export_<时间戳>/")
    lines.append("  ├── 电脑端/                  电脑上运行的程序产生的日志")
    lines.append("  │   ├── app/                 本机 EGOViewer 应用日志")
    lines.append("  │   ├── sdk/                 Orbbec SDK 日志")
    lines.append("  │   └── firmware/            经 USB 直连设备拉取的固件日志")
    lines.append("  └── 手机端/<机型>/           手机 adb 搬运过来的日志")
    lines.append("      ├── 手机导出包.zip        手机 EGOViewer 导出的原始包（未解压）")
    lines.append("      ├── sdk日志/              手机侧 Orbbec SDK 日志")
    lines.append("      └── 固件日志/<SN>-<时刻>/  手机侧落盘的固件日志")
    lines.append("")

    counts = app_sdk_counts or {}
    if counts:
        lines.append("【电脑端 · 本机 EGOViewer】")
        for cat, n in counts.items():
            lines.append(f"  电脑端/{cat}/：{n} 个文件")
        lines.append("")

    fw_devices = (pc_firmware or {}).get("devices") if isinstance(pc_firmware, dict) else None
    if fw_devices:
        lines.append("【电脑端 · USB 直连设备 · Orbbec SDK 实时拉取】")
        lines.append(f"  拉取时刻：{_fmt_ts((pc_firmware or {}).get('at'))}")
        for grp, n in fw_devices.items():
            lines.append(f"  电脑端/firmware/{grp}/：{n} 个文件")
        lines.append("")

    phones = (mobile or {}).get("phones") or []
    if phones:
        lines.append("【手机端 · adb 搬运（原样，不解包重排、不做帧对齐）】")
        for ph in phones:
            pname = ph.get("phone")
            lines.append(f"  手机：{pname}")
            for b in ph.get("bundles", []):
                if b.get("source") == "firmware_logs":
                    n = sum(d["files"] for d in b.get("devices") or [])
                    grp = os.path.basename(b.get("dir") or "")
                    lines.append(f"    手机端/{pname}/固件日志/{grp}/：{n} 个文件")
                else:
                    lines.append(f"    手机端/{pname}/手机导出包.zip："
                                 f"app {b.get('app_files', 0)} 个日志文件（未解压）")
                for dev in b.get("devices") or []:
                    lines.append(f"      其中设备 {dev['group']}：{dev['files']} 个文件")
            lines.append("")

    has_pc_fw = bool(fw_devices)
    has_mobile_fw = any(
        b.get("devices") for ph in phones for b in ph.get("bundles", []))
    if has_mobile_fw and not has_pc_fw:
        lines.append("固件日志来源：手机 EGOViewer 导出（Wi-Fi 连接设备），未重复从电脑 USB 拉取。")
        lines.append("")
    elif has_pc_fw and not has_mobile_fw:
        lines.append("固件日志来源：电脑 USB 直连设备，由 Orbbec SDK 实时拉取。")
        lines.append("")

    lines.append(f"目录总文件数：{_count_files(export_dir)}")
    lines.append("")
    lines.append("（本清单由 EGO Courier 自动生成，仅作来源说明，不参与分析。）")

    path = os.path.join(export_dir, MANIFEST_NAME)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path
