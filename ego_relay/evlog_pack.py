"""本地 EGOViewer 三类日志收集、筛选与打包导出。

把分散在三个位置的本机日志收集进一个 zip（按 app/ sdk/ firmware/ 子目录分类、
保留原始文件名），以日期时间命名，保存到自定义路径。原始日志文件一律不改动。

三类日志（相对 EGOViewer 安装根目录的常见位置）：
  app      → data\\logs\\EGOViewer_*.log          EGOViewer 应用运行/操作日志
  sdk      → Log\\OrbbecSDK.*.log.txt              Orbbec SDK 在 PC 上运行的底层日志
  firmware → data\\firmware-logs\\**\\orbbec*.log   相机设备端固件日志（按拉取时间戳分子目录）

版本兼容：candidate_rel_dirs 是各分类的“常见”相对路径；若这些目录都不存在或
没找到文件，则回退为在整个安装根目录下按文件名模式递归搜索，以适配未来版本
目录结构调整。是否某目录是 EGOViewer 安装根目录，以存在 EGOViewer.exe（或
data/logs）为稳定判据。
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import time
import zipfile
from datetime import datetime

from . import config

# 三类日志：key / 界面简洁名 / zip 内子目录 / 常见相对路径 / 文件名规则 / 是否递归
# firmware 目录下按拉取时间戳分子目录，递归收集并保留其相对子目录结构。
CATEGORIES = [
    {
        "key": "app",
        "label": "EGOViewer 应用日志",
        "zip_dir": "app",
        "candidate_rel_dirs": ["data/logs"],
        "pattern": "EGOViewer_*.log",
        "recursive": False,
        "desc": "EGOViewer 应用运行/操作日志（设备扫描、配网、连接、码流、参数等）",
    },
    {
        "key": "sdk",
        "label": "Orbbec SDK 日志",
        "zip_dir": "sdk",
        "candidate_rel_dirs": ["Log", "logs", "data/logs"],
        "pattern": "OrbbecSDK.*.log.txt",
        "recursive": False,
        "desc": "Orbbec SDK 在 PC 上运行的底层日志（驱动/SDK 内部）",
    },
    {
        "key": "firmware",
        "label": "设备固件日志",
        "zip_dir": "firmware",
        "candidate_rel_dirs": ["data/firmware-logs", "firmware-logs"],
        "pattern": "*",
        "fallback_pattern": "orbbec*.log*",
        "recursive": True,
        "desc": "相机设备端固件日志（按拉取时间戳分子目录）",
    },
]


def _matches(fn: str, pattern: str) -> bool:
    return pattern == "*" or fnmatch.fnmatch(fn, pattern)


def find_ego_viewer_roots() -> list:
    """在 EGO_VIEWER_SEARCH_ROOTS 下定位 EGOViewer 安装根目录。

    判据：目录名以 EGOViewer_ 开头且含 win_x64，且存在 EGOViewer.exe（或 data/logs）。
    """
    out = []
    for root in config.EGO_VIEWER_SEARCH_ROOTS:
        if not os.path.isdir(root):
            continue
        try:
            entries = list(os.scandir(root))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir() and e.name.startswith("EGOViewer_") and "win_x64" in e.name:
                    has_exe = os.path.isfile(os.path.join(e.path, "EGOViewer.exe"))
                    has_data = os.path.isdir(os.path.join(e.path, "data", "logs"))
                    if has_exe or has_data:
                        out.append(e.path)
            except OSError:
                continue
    return out


def _gather(root: str, cat: dict) -> list:
    """在某安装根目录下收集一个分类的日志文件。

    优先从 candidate_rel_dirs 中首个存在的目录收集；若都为空，则回退为在整个
    根目录下按文件名模式递归搜索（版本兼容）。返回 [{"abs","rel"}]，rel 相对
    cat 的收集基准目录（candidate 目录，或回退时的安装根）。
    """
    found = []
    for rel in cat["candidate_rel_dirs"]:
        base = os.path.join(root, rel)
        if not os.path.isdir(base):
            continue
        if cat.get("recursive"):
            for dirpath, _dirnames, filenames in os.walk(base):
                for fn in filenames:
                    if not _matches(fn, cat["pattern"]):
                        continue
                    full = os.path.join(dirpath, fn)
                    found.append({"abs": full, "rel": os.path.relpath(full, base)})
        else:
            for fn in os.listdir(base):
                full = os.path.join(base, fn)
                if os.path.isfile(full) and _matches(fn, cat["pattern"]):
                    found.append({"abs": full, "rel": fn})
        if found:
            break
    if not found:
        # 回退：整个根目录按模式递归找（firmware 用更精确的回退模式，避免误收非日志文件）
        fb = cat.get("fallback_pattern", cat["pattern"])
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                if not _matches(fn, fb):
                    continue
                full = os.path.join(dirpath, fn)
                found.append({"abs": full, "rel": os.path.relpath(full, root)})
    return found


def _file_stamp(abs_path: str, rel: str) -> float:
    """从文件名/目录名解析日志时间戳（epoch 秒），无法解析时退回文件修改时间。

    命名规则：EGOViewer_YYYYMMDDHHMMSS.log、OrbbecSDK.YYYYMMDDHHMMSS.log.txt、
    固件时间戳目录 20260927-104153 等。
    """
    import re
    m = re.search(r"(\d{8})[-_]?(\d{6})", rel)
    if m:
        try:
            return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").timestamp()
        except ValueError:
            pass
    return os.path.getmtime(abs_path)


def discover(root: str, keys=None) -> dict:
    """返回某安装根目录下各分类发现的所有日志文件（含 stamp），每类按时间倒序。

    供“手动勾选文件”对话框展示用；stamp 用于在界面上显示各文件的日志时间。
    """
    keys = keys or [c["key"] for c in CATEGORIES]
    result = {}
    for cat in CATEGORIES:
        key = cat["key"]
        if key not in keys:
            result[key] = []
            continue
        files = _gather(root, cat)
        for f in files:
            f["stamp"] = _file_stamp(f["abs"], f["rel"])
        files.sort(key=lambda f: f["stamp"], reverse=True)
        result[key] = files
    return result


def _within(stamp: float, max_age_days, start, end) -> bool:
    if max_age_days and stamp < time.time() - float(max_age_days) * 86400:
        return False
    if start is not None and stamp < start:
        return False
    if end is not None and stamp > end:
        return False
    return True


def collect(root: str, keys=None, max_age_days=None, max_count=None,
            start=None, end=None, manual=None) -> dict:
    """收集某安装根目录下各分类匹配到的日志文件（可按多种方式筛选）。

    筛选（可组合）：
      max_age_days  只保留最近 N 天内（按日志时间戳/修改时间）
      start/end     epoch 秒，只保留 [start, end] 时间段内
      manual        {分类key: [绝对路径,...]}，显式指定要打包的文件（此时忽略其它筛选）

    返回 {分类key: [{"abs": 绝对路径, "rel": 相对路径}, ...]}，每类按 rel 排序。
    """
    keys = keys or [c["key"] for c in CATEGORIES]
    if manual is not None:
        result = {}
        for key in keys:
            files = []
            for abs_path in (manual.get(key) or []):
                files.append({"abs": abs_path, "rel": os.path.relpath(abs_path, root)})
            files.sort(key=lambda f: f["rel"])
            result[key] = files
        return result
    disc = discover(root, keys)
    result = {}
    for key in keys:
        files = [f for f in disc.get(key, []) if _within(f["stamp"], max_age_days, start, end)]
        if max_count:
            files = files[:int(max_count)]
        files.sort(key=lambda f: f["rel"])
        result[key] = [{"abs": f["abs"], "rel": f["rel"]} for f in files]
    return result


def _arcname(rel: str, zip_dir: str) -> str:
    """把相对路径归一化成安全 zip 内路径（去盘符/去 .. /防路径穿越），并挂在分类子目录下。"""
    segs = rel.replace("\\", "/").split("/")
    clean = [os.path.basename(s) for s in segs if s and s not in (".", "..")]
    return "/".join([zip_dir] + clean)


def stage(root: str, dest_dir: str, keys=None, max_age_days=None, max_count=None,
          start=None, end=None, manual=None, progress_cb=None) -> tuple:
    """收集（含筛选）并把选中的日志复制到 dest_dir/<分类>/...，供打包与分析复用。

    返回 (dest_dir, collected)。原始文件不改动。
    """
    collected = collect(root, keys, max_age_days, max_count, start, end, manual)
    os.makedirs(dest_dir, exist_ok=True)
    done = 0
    total = sum(len(v) for v in collected.values())
    for cat in CATEGORIES:
        key = cat["key"]
        for f in collected.get(key, []):
            arc = _arcname(f["rel"], cat["zip_dir"])
            target = os.path.join(dest_dir, *arc.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(f["abs"], target)
            done += 1
            if progress_cb and total:
                progress_cb(int(done * 60 / total))
    return dest_dir, collected


def _manifest_lines(root: str, collected: dict, max_age_days) -> tuple:
    manifest = {
        "export_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ego_viewer_root": root,
        "recent_days": int(max_age_days) if max_age_days else None,
        "categories": [],
    }
    lines = ["EGOViewer 日志打包说明", "=" * 30, ""]
    if max_age_days:
        lines.append(f"筛选：仅包含最近 {int(max_age_days)} 天内修改的日志")
        lines.append("")
    for cat in CATEGORIES:
        key = cat["key"]
        n = len(collected.get(key, []))
        src = os.path.join(root, cat["candidate_rel_dirs"][0])
        manifest["categories"].append({
            "key": key, "label": cat["label"], "source": src, "count": n,
        })
        lines.append(f"[{cat['label']}] {n} 个文件")
        lines.append(f"  来源: {src}")
        lines.append(f"  说明: {cat['desc']}")
        lines.append("")
    return manifest, lines


def pack_staged(staged_dir: str, out_dir: str, root: str, collected: dict,
                prefix: str = "EGOViewer_logs", max_age_days=None,
                progress_cb=None) -> str:
    """把已 stage 到 staged_dir 的日志打包成 <out_dir>/<prefix>_<YYYYMMDD_HHMMSS>.zip。

    返回 zip 绝对路径。未找到任何日志时抛 FileNotFoundError。
    """
    total = sum(len(v) for v in collected.values())
    if total == 0:
        raise FileNotFoundError("未找到任何日志文件，请检查 EGOViewer 安装目录或时间窗口")
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = os.path.join(out_dir, f"{prefix}_{ts}.zip")

    manifest, lines = _manifest_lines(root, collected, max_age_days)
    written = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, _dirnames, filenames in os.walk(staged_dir):
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, staged_dir).replace("\\", "/")
                zf.write(full, rel)
                written += 1
                if progress_cb and total:
                    progress_cb(60 + int(written * 40 / total))
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr("说明.txt", "\n".join(lines))
    return zip_path
