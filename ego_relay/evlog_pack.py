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

可取消：collect/stage/pack_staged/discover 均接受可选 should_cancel 回调（默认
不可取消）。回调在收集、复制、压缩的长循环检查点被调用，返回真则抛
OperationCancelled 中断，供 UI 把"用户取消"与"真实失败"分开处理。
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import time
import zipfile
from datetime import datetime

from . import config


class OperationCancelled(Exception):
    """用户请求取消收集/打包时抛出，供 UI 线程识别为"已取消"而非失败。"""


def _check_cancel(should_cancel):
    """在长循环的检查点调用：should_cancel() 为真则中断当前任务。

    仅在调用方传入回调时生效（默认 None = 不可取消）。
    """
    if should_cancel is not None and should_cancel():
        raise OperationCancelled("操作已取消")


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
        # 回退模式需覆盖设备侧三类固件日志（orbbec.log + messages.log* +
        # observice.console.log*），避免目录结构变动后漏收。
        "fallback_pattern": "orbbec*.log*",
        "fallback_patterns": ["orbbec*.log*", "messages.log*", "observice.console.log*"],
        "recursive": True,
        "desc": "相机设备端固件日志（orbbec.log / messages.log* / observice.console.log*）",
    },
]


def _matches(fn: str, pattern: str) -> bool:
    return pattern == "*" or fnmatch.fnmatch(fn, pattern)


def _version_key(name: str) -> tuple:
    """从 EGOViewer 目录名解析版本号（形如 v2.0.15），用于按版本排序。

    解析失败时返回 (0,0,0)，使无法识别版本的目录排在最后。
    """
    m = re.search(r"v(\d+)\.(\d+)\.(\d+)", name)
    if m:
        return tuple(int(x) for x in m.groups())
    return (0, 0, 0)


def find_ego_viewer_roots() -> list:
    """在 EGO_VIEWER_SEARCH_ROOTS 下定位 EGOViewer 安装根目录。

    判据：目录名以 EGOViewer_ 开头且含 win_x64，且存在 EGOViewer.exe（或 data/logs）。
    返回结果按版本号从新到旧排序（同一版本内按目录名），保证界面默认选中最新版本。
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
    out.sort(
        key=lambda p: (_version_key(os.path.basename(p)), os.path.basename(p)),
        reverse=True)
    return out


def _gather(root: str, cat: dict, should_cancel=None) -> list:
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
                _check_cancel(should_cancel)
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
        pats = cat.get("fallback_patterns") or [cat.get("fallback_pattern", cat["pattern"])]
        for dirpath, _dirnames, filenames in os.walk(root):
            _check_cancel(should_cancel)
            for fn in filenames:
                if not any(_matches(fn, p) for p in pats):
                    continue
                full = os.path.join(dirpath, fn)
                found.append({"abs": full, "rel": os.path.relpath(full, root)})
    return found


def _name_stamp(rel: str):
    """尝试从文件名/目录名解析日志时间戳（epoch 秒），失败返回 None。

    命名规则（按优先级）：
    - EGO-4B 等设备按天命名的固件日志：orbbec_2026-10-14.log、orbbec_2026-10-14.log.1
      （只有日期没有时刻，取当天 00:00 作为区间起点）
    - 带秒级时间：EGOViewer_YYYYMMDDHHMMSS.log、OrbbecSDK.YYYYMMDDHHMMSS.log.txt、
      固件拉取时间戳目录 20260927-104153
    只认**文件名最后一段**里的时间，避免整条相对路径都命中目录名中的拉取时刻——
    否则同一目录下的所有文件都会被判成同一时刻，时间筛选就失效了。
    """
    name = os.path.basename(rel.replace("\\", "/"))
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})(?!\d)", name)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).timestamp()
        except ValueError:
            pass
    m = re.search(r"(\d{8})[-_]?(\d{6})", name)
    if m:
        try:
            return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").timestamp()
        except ValueError:
            return None
    if rel.replace("\\", "/") != name:
        # 文件名里没有时间，再看所在目录（如 20260927-104153/）
        return _name_stamp(os.path.dirname(rel.replace("\\", "/")))
    return None


def _file_stamp(abs_path: str, rel: str) -> tuple:
    """返回 (start_stamp, end_stamp, from_name)：该日志文件"内容覆盖"的时间区间。

    文件名/目录名中的时间戳是**日志会话开始**的时刻，而 EGOViewer/OrbbecSDK 的会话
    可长时间运行、跨天追加写入，故文件名时间只代表区间起点；文件修改时间(mtime)则是
    最后一次写入的时刻，可作为区间终点。只有两者的区间与筛选窗口有交集才应保留该文件。

    - 有文件名时间戳：start=文件名时间，end=max(文件名时间, mtime)。
      （正常关闭的会话 mtime 略晚于 start；仍在写入的长会话 mtime 会远晚于 start。）
    - 无文件名时间戳：只有一个可靠时点，start=end=mtime，from_name=False，
      界面据此提示"时间取自修改时间，可能与日志真实时间不一致"。
    """
    mtime = os.path.getmtime(abs_path)
    name_stamp = _name_stamp(rel)
    if name_stamp is not None:
        return name_stamp, max(name_stamp, mtime), True
    return mtime, mtime, False


def _overlaps(start_stamp: float, end_stamp: float, start, end) -> bool:
    """判断文件区间 [start_stamp, end_stamp] 是否与筛选窗口 [start, end] 有交集。

    窗口端点可为 None（表示该侧不限）。跨天会话的起点早于窗口起点、但其 mtime 落在
    窗口内时，仍会被判定为命中——这正是修复"长会话被整份丢弃"的关键。
    """
    if start is not None and end_stamp < start:
        return False
    if end is not None and start_stamp > end:
        return False
    return True


def discover(root: str, keys=None, should_cancel=None) -> dict:
    """返回某安装根目录下各分类发现的所有日志文件（含时间区间），每类按时间倒序。

    每个文件带 start_stamp/end_stamp（内容覆盖区间）、stamp（用于列表展示的时点，
    取会话开始时间；无文件名时间戳时取 mtime）与 from_name。供"手动勾选文件"对话框
    展示用；stamp 用于在界面上显示各文件的日志时间。
    """
    keys = keys or [c["key"] for c in CATEGORIES]
    result = {}
    for cat in CATEGORIES:
        _check_cancel(should_cancel)
        key = cat["key"]
        if key not in keys:
            result[key] = []
            continue
        files = _gather(root, cat, should_cancel)
        for f in files:
            f["start_stamp"], f["end_stamp"], f["from_name"] = _file_stamp(f["abs"], f["rel"])
            f["stamp"] = f["start_stamp"]
        files.sort(key=lambda f: f["start_stamp"], reverse=True)
        result[key] = files
    return result


def _within(start_stamp: float, end_stamp: float, max_age_days, start, end) -> bool:
    """按文件时间区间 [start_stamp, end_stamp] 判定是否保留（见 _overlaps）。"""
    if max_age_days and end_stamp < time.time() - float(max_age_days) * 86400:
        return False
    return _overlaps(start_stamp, end_stamp, start, end)


def filter_firmware_pull(fw_paths: list, pulled_at: float,
                         start=None, end=None) -> tuple:
    """按「拉取时间戳」筛选刚拉取的固件日志文件。

    设备实时拉取的固件日志文件名由设备给出（如 orbbec_xxx.log），通常不含时间戳，
    因此不能靠文件名判定时间。这类日志的"日志时间"以本次拉取动作的时刻 pulled_at
    为准，写入按拉取时间戳命名的子目录（YYYYMMDD-HHMMSS/）后即可与 app/sdk 一样
    用同一套 [start, end] 判定。pulled_at 落在窗口内则整批保留，否则整批剔除。

    容差：拉取时刻与界面上的"止"（end）往往只差几毫秒到一两秒（终点跟随当前时刻），
    若严格比较会把刚好落在终点附近的整批固件日志误剔除，故对 end 放宽 2 秒。

    返回 (kept_paths, stamp, within)：stamp 为本次拉取时间戳，within 表示是否命中窗口。
    """
    tol = 2.0
    if _within(pulled_at, pulled_at, None, start, None if end is None else end + tol):
        return list(fw_paths), pulled_at, True
    return [], pulled_at, False


def collect(root: str, keys=None, max_age_days=None, max_count=None,
            start=None, end=None, manual=None, should_cancel=None) -> dict:
    """收集某安装根目录下各分类匹配到的日志文件（可按多种方式筛选）。

    筛选（可组合）：
      max_age_days  只保留最近 N 天内（按日志时间戳/修改时间）
      start/end     epoch 秒，只保留 [start, end] 时间段内
      manual        {分类key: [绝对路径,...]}，显式指定要打包的文件（此时忽略其它筛选）

    返回 {分类key: [{"abs": 绝对路径, "rel": 相对路径, "from_name": 时间是否取自文件名}, ...]}，每类按 rel 排序。
    """
    keys = keys or [c["key"] for c in CATEGORIES]
    if manual is not None:
        result = {}
        for key in keys:
            files = []
            for abs_path in (manual.get(key) or []):
                rel = os.path.relpath(abs_path, root)
                _start, _end, from_name = _file_stamp(abs_path, rel)
                files.append({"abs": abs_path, "rel": rel, "from_name": from_name})
            files.sort(key=lambda f: f["rel"])
            result[key] = files
        return result
    disc = discover(root, keys, should_cancel)
    result = {}
    for key in keys:
        _check_cancel(should_cancel)
        files = [f for f in disc.get(key, [])
                 if _within(f["start_stamp"], f["end_stamp"], max_age_days, start, end)]
        if max_count:
            files = files[:int(max_count)]
        files.sort(key=lambda f: f["rel"])
        result[key] = [{"abs": f["abs"], "rel": f["rel"], "from_name": f.get("from_name", True)}
                       for f in files]
    return result


def _arcname(rel: str, zip_dir: str) -> str:
    """把相对路径归一化成安全 zip 内路径（去盘符/去 .. /防路径穿越），并挂在分类子目录下。

    相对路径里**合法的子目录要保留**（如固件日志的 <拉取时刻>/orbbec.log），只丢掉
    “盘符”“..”“.”这类不安全或无语义的段；只有最后一段是文件名。
    """
    segs = rel.replace("\\", "/").split("/")
    clean = [s for s in segs[:-1] if s and s not in (".", "..") and not s.endswith(":")]
    name = os.path.basename(segs[-1]) if segs else ""
    if not name or name in (".", ".."):
        return zip_dir
    return "/".join([zip_dir] + clean + [name])


def external_category(key: str, local_dir: str, rel_prefix: str = "", max_age_days=None,
                      start=None, end=None) -> tuple:
    """把已落盘的外部日志目录包装成一个分类的 collect 结果（供 stage 一并复制）。

    用于设备实时拉取的固件日志：它们不在 EGOViewer 安装目录下，由调用方先拉取到
    local_dir（通常形如 <导出目录>/20261016-152734），这里只做时间筛选与归档。

    rel_prefix 是归档时挂在文件名前的子目录（通常就是拉取时刻），最终 zip 内路径形如
    firmware/<拉取时刻>/orbbec.log，与 EGOViewer 的 firmware-logs/<拉取时刻>/ 对应；
    若传空串则 zip 内直接是 firmware/orbbec.log，不再多套一层。
    """
    files = []
    if os.path.isdir(local_dir):
        for dirpath, dirnames, filenames in os.walk(local_dir):
            dirnames.sort()
            for fn in sorted(filenames):
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, local_dir).replace("\\", "/")
                if rel_prefix:
                    rel = f"{rel_prefix.strip('/')}/{rel}"
                files.append({"abs": full, "rel": rel})
    kept = []
    for f in files:
        start_stamp, end_stamp, from_name = _file_stamp(f["abs"], f["rel"])
        if _within(start_stamp, end_stamp, max_age_days, start, end):
            kept.append({"abs": f["abs"], "rel": f["rel"], "from_name": from_name,
                         "start_stamp": start_stamp, "end_stamp": end_stamp,
                         "stamp": start_stamp})
    kept.sort(key=lambda f: f["start_stamp"], reverse=True)
    return key, kept


def stage(root: str, dest_dir: str, keys=None, max_age_days=None, max_count=None,
          start=None, end=None, manual=None, progress_cb=None, should_cancel=None,
          external=None) -> tuple:
    """收集（含筛选）并把选中的日志复制到 dest_dir/<分类>/...，供打包与分析复用。

    返回 (dest_dir, collected)。原始文件不改动。should_cancel 在复制每个文件前检查，
    便于长任务在收集/复制阶段被用户及时中断。

    external：{分类key: [(abs, rel)]}，已在外部落盘的日志（如设备实时拉取的固件日志），
    与 root 下收集到的同分类文件合并。rel 是相对该分类 zip 子目录的路径。
    """
    collected = collect(root, keys, max_age_days, max_count, start, end, manual,
                        should_cancel=should_cancel)
    for key, items in (external or {}).items():
        if key not in collected:
            continue
        have = {os.path.abspath(f["abs"]) for f in collected[key]}
        for abs_path, rel in items:
            if os.path.abspath(abs_path) in have:
                continue
            start_stamp, end_stamp, from_name = _file_stamp(abs_path, rel)
            collected[key].append({
                "abs": abs_path, "rel": rel, "from_name": from_name,
                "start_stamp": start_stamp, "end_stamp": end_stamp,
                "stamp": start_stamp,
            })
    os.makedirs(dest_dir, exist_ok=True)
    done = 0
    total = sum(len(v) for v in collected.values())
    for cat in CATEGORIES:
        key = cat["key"]
        for f in collected.get(key, []):
            _check_cancel(should_cancel)
            arc = _arcname(f["rel"], cat["zip_dir"])
            target = os.path.join(dest_dir, *arc.split("/"))
            if os.path.abspath(target) == os.path.abspath(f["abs"]):
                # 源与目标同一路径（外部日志直接拉在 dest_dir 内）：无需复制，
                # Windows 上 shutil.copy2 会报 WinError 32。
                done += 1
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(f["abs"], target)
            done += 1
            if progress_cb and total:
                progress_cb(int(done * 60 / total))
    return dest_dir, collected


def _manifest_lines(root: str, collected: dict, max_age_days, start=None, end=None) -> tuple:
    manifest = {
        "export_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ego_viewer_root": root,
        "recent_days": int(max_age_days) if max_age_days else None,
        "time_range": None,
        "mtime_based_files": [],
        "categories": [],
    }
    lines = ["EGOViewer 日志打包说明", "=" * 30, ""]
    if start is not None or end is not None:
        def _fmt(v):
            return datetime.fromtimestamp(v).strftime("%Y-%m-%d %H:%M:%S") if v is not None else "不限"
        rng = f"{_fmt(start)} ～ {_fmt(end)}"
        manifest["time_range"] = {"start": start, "end": end, "text": rng}
        lines.append(f"筛选：包含日志会话时间与 {rng} 有交集的日志文件")
        lines.append("说明：日志文件名中的时间戳是「会话开始时刻」，而 EGOViewer/OrbbecSDK 的")
        lines.append("      会话可持续运行并跨天追加写入。因此「开始于窗口之前、但仍在窗口内")
        lines.append("      写入」的长会话也会被包含（其内容覆盖了所选时间段）。")
        lines.append("")
    if max_age_days:
        lines.append(f"筛选：仅包含最近 {int(max_age_days)} 天内修改的日志")
        lines.append("")
    # 时间戳来自文件修改时间（文件名无法解析出时间）的文件：时间可能不准，单独列出提醒
    mtime_files = []
    for cat in CATEGORIES:
        for f in collected.get(cat["key"], []):
            if not f.get("from_name", True):
                mtime_files.append(f["rel"])
    if mtime_files:
        manifest["mtime_based_files"] = mtime_files
        lines.append(f"注意：以下 {len(mtime_files)} 个文件的文件名不含时间戳，"
                     "其时间取自文件修改时间，可能与日志真实时间不一致：")
        for rel in mtime_files:
            lines.append(f"  - {rel}")
        lines.append("")
    for cat in CATEGORIES:
        key = cat["key"]
        n = len(collected.get(key, []))
        src = os.path.join(root, cat["candidate_rel_dirs"][0])
        if key == "firmware" and n:
            # 固件日志来自设备实时拉取（不在安装目录下），来源说明要写清楚
            src = "设备实时拉取（Orbbec SDK 固件日志接口）"
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
                start=None, end=None, progress_cb=None, should_cancel=None) -> str:
    """把已 stage 到 staged_dir 的日志打包成 <out_dir>/<prefix>_<YYYYMMDD_HHMMSS>.zip。

    返回 zip 绝对路径。未找到任何日志时抛 FileNotFoundError。should_cancel 在每个
    文件写入前检查；被取消时删除写了一半的 zip，不留残缺产物。
    """
    total = sum(len(v) for v in collected.values())
    if total == 0:
        raise FileNotFoundError("未找到任何日志文件，请检查 EGOViewer 安装目录或时间窗口")
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = os.path.join(out_dir, f"{prefix}_{ts}.zip")

    manifest, lines = _manifest_lines(root, collected, max_age_days, start, end)
    written = 0
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for dirpath, _dirnames, filenames in os.walk(staged_dir):
                for fn in filenames:
                    _check_cancel(should_cancel)
                    full = os.path.join(dirpath, fn)
                    rel = os.path.relpath(full, staged_dir).replace("\\", "/")
                    zf.write(full, rel)
                    written += 1
                    if progress_cb and total:
                        progress_cb(60 + int(written * 40 / total))
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            zf.writestr("说明.txt", "\n".join(lines))
    except BaseException:
        # 取消或异常：不留下写到一半的 zip（打包与导出共用输出目录，残缺包会被误当成结果）
        try:
            if os.path.exists(zip_path):
                os.remove(zip_path)
        except OSError:
            pass
        raise
    return zip_path
