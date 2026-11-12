"""Android 手机 EGOViewer 日志导出（adb 通道）。

手机 EGOViewer 自己会把日志打包成导出包放在应用外部目录：

    /sdcard/Android/data/com.egoviewer.app/files/
    ├── app-logs-zips/egoviewer-logs-*.zip   ← App 日志 + 诊断 + SDK + 设备固件（连过设备时）
    │   （旧版命名 ego-logs-app-*.zip 两种都认）
    ├── sdk-logs/OrbbecSDK.log.txt
    └── firmware_logs/                        ← 手机侧拉取落盘的固件日志缓存（可能为空）

导出包内部结构：app/ sdk/ diagnostics/ firmware/<SN>-<时间戳>-<hash>/。
其中 `firmware/<SN>-…/` 是「每台设备一个分组」：手机一次导出会把当时连接的
**所有设备**（主设备 + 左右指环 / 手表等从设备）各放一个子目录。

落地到电脑时**展开**这些分组，去掉中间的 firmware/ 层（用户要求的样式）：

    <导出目录>/Pixel 7/
    ├── 手机导出包.zip                      ← 导出包原样保留一份备查
    ├── app/ sdk/ diagnostics/              ← 手机 App 自身日志
    ├── sdk日志/OrbbecSDK.log.txt
    ├── AZL9861004V-<时间戳>-<hash>/         ← 主设备
    └── BF8LA60006M-<时间戳>-<hash>/         ← 从设备，各自独立一份

只把 zip 原样搬回电脑时，读者要自己解压、自己翻 firmware/ 才能看到从设备日志，
现场表现为「勾了手机日志，但从设备的日志没被识别」。展开后每台设备在机型目录
下各占一层，打开即可看到全部设备。
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from typing import Callable, Dict, List, Optional

PKG = "com.egoviewer.app"
REMOTE_FILES = f"/sdcard/Android/data/{PKG}/files"
REMOTE_DIRS = {
    # 本地子目录名 -> 手机上的相对目录
    "app-logs-zips": f"{REMOTE_FILES}/app-logs-zips",
    "sdk-logs": f"{REMOTE_FILES}/sdk-logs",
    "firmware_logs": f"{REMOTE_FILES}/firmware_logs",
}

# 安卓 data 目录在部分系统/机型上 adb 读不到（Android 11+ 对 /sdcard/Android/data
# 的限制因厂商而异），给出可执行的替代办法而不是一句“失败”。
_HINT = ("手机端「开发者选项 → USB 调试」需已授权本机；若提示 Permission denied，"
         "请在手机上用文件管理器把 EGOViewer 导出的 zip 拷到 下载 目录后再导出。")


class MobileExportError(RuntimeError):
    """adb 缺失、无设备、未授权、拉取失败等。"""


def find_adb() -> str:
    """返回可用的 adb 路径；找不到返回空串（PATH 里有就不用管）。"""
    exe = shutil.which("adb")
    if exe:
        return exe
    for pat in (
        os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe"),
        os.path.expandvars(r"%USERPROFILE%\AppData\Local\Android\Sdk\platform-tools\adb.exe"),
        r"C:\platform-tools\adb.exe",
        r"C:\platform-tools\platform-tools\adb.exe",
    ):
        if os.path.isfile(pat):
            return pat
    return ""


def _run(adb: str, args: List[str], timeout: float = 20.0,
         cancel_event=None) -> subprocess.CompletedProcess:
    """执行 adb 命令；给了 cancel_event 时会被轮询，取消即杀进程。

    adb pull 单次最长等 900 秒，若只在调用前后检查取消标志，用户点了「取消任务」
    仍要干等整条命令跑完（进度条因此卡住不动）。这里改成
    Popen + 短间隔轮询，取消时直接 terminate，让 worker 立刻抛出取消。
    取消时返回 returncode=-1 的空结果，由调用方按"任务被取消"处理。
    """
    if cancel_event is None:
        return subprocess.run(
            [adb] + args, capture_output=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    import time
    proc = subprocess.Popen(
        [adb] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    deadline = time.monotonic() + timeout
    while True:
        try:
            out, err = proc.communicate(timeout=0.3)
            return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)
        except subprocess.TimeoutExpired:
            if cancel_event.is_set():
                proc.terminate()
                try:
                    proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
                raise MobileExportError("操作已取消")
            if time.monotonic() > deadline:
                proc.kill()
                proc.communicate()
                raise subprocess.TimeoutExpired(proc.args, timeout)


def list_devices(adb: str = "") -> List[Dict[str, str]]:
    """返回 [{"serial","state","model"}]，只含状态为 device 的已授权设备。"""
    adb = adb or find_adb()
    if not adb:
        raise MobileExportError(
            "未找到 adb。请安装 Android platform-tools 并加入 PATH，"
            "或放到 C:\\platform-tools\\ 下。")
    r = _run(adb, ["devices", "-l"])
    text = r.stdout.decode("utf-8", "replace")
    out = []
    for line in text.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        if state != "device":
            # unauthorized / offline 不列为可导出设备，由调用方汇总提示
            continue
        model = ""
        for p in parts[2:]:
            if p.startswith("model:"):
                model = p.split(":", 1)[1]
        out.append({"serial": serial, "state": state, "model": model})
    return out


def device_status(adb: str = "") -> List[Dict[str, str]]:
    """列全部设备条目（含 unauthorized/offline），供提示用户去手机上点允许。"""
    adb = adb or find_adb()
    if not adb:
        return []
    r = _run(adb, ["devices", "-l"])
    out = []
    for line in r.stdout.decode("utf-8", "replace").splitlines()[1:]:
        parts = line.strip().split()
        if len(parts) >= 2:
            out.append({"serial": parts[0], "state": parts[1]})
    return out


def pull_all(adb: str, serial: str, dest_root: str,
             note: Optional[Callable[[str], None]] = None,
             progress: Optional[Callable[[int], None]] = None,
             cancel_event=None) -> Dict[str, list]:
    """把一台手机的日志拉到 `<dest_root>/<机型>/`，并把导出包里的**每台设备**展开。

    目标结构（去掉多余的 firmware/ 分组层，设备直接平铺在机型下）：

        Pixel 7/
        ├── 手机导出包.zip                     ← 最新那个导出包，原样保留一份备查
        ├── sdk日志/OrbbecSDK.log.txt
        ├── AZL9861004V-20261109_153234_206-6f342b60/   ← 主设备（含从设备快照）
        │   ├── orbbec_2026-11-09.log
        │   ├── dmesg.txt
        │   └── ...
        └── BF8LA60006M-20261109_150910_414-d7997f8b/   ← 从设备，各自独立一份

    为什么展开：手机一次「导出日志」会把当时连接的**所有设备**（主设备 + 左右指环/
    手表等从设备）各打成一个 `firmware/<SN>-<时间戳>-<hash>/` 分组塞进同一个 zip。
    只把 zip 原样搬回电脑，读者要自己解压、自己翻 firmware/ 才能看到从设备日志，
    现场表现为「勾了手机日志，但从设备的日志没被识别」。现在直接解出全部设备分组，
    每台设备一个平铺目录，不再套 firmware/ 这一层（用户要求的样式）。

    兜底：若手机上还留着 `firmware_logs/<SN>-…/`（App 侧持久化副本），也一并平铺进来。
    只拉最新 zip：app-logs-zips 会累积历次导出，全拉回来会让目录堆满重复历史包。
    """
    emit = note or (lambda _t: None)
    prog = progress or (lambda _p: None)

    def _check_cancel():
        if cancel_event is not None and cancel_event.is_set():
            raise MobileExportError("操作已取消")

    _check_cancel()
    model = ""
    try:
        r = _run(adb, ["-s", serial, "shell", "getprop", "ro.product.model"], timeout=10,
                 cancel_event=cancel_event)
        model = r.stdout.decode("utf-8", "replace").strip()
    except MobileExportError:
        raise
    except Exception:  # noqa: BLE001
        pass
    # 目录名不能带下划线（adb pull 会当成路径分隔符），所以 Pixel_7 → Pixel 7；
    # 日志里则用同样可读的显示名。
    label = _folder_name(model, serial)
    dev_root = os.path.join(dest_root, label)
    result: Dict[str, list] = {}

    # 1) 最新导出包：按文件名里的时间戳挑最新，避免依赖 mtime（拷来拷去会变）
    _check_cancel()
    latest = latest_zip(adb, serial, cancel_event=cancel_event)
    if latest:
        os.makedirs(dev_root, exist_ok=True)
        target = os.path.join(dev_root, "手机导出包.zip")
        _adb_pull_to(adb, serial, latest, target, "导出包", label,
                     cancel_event=cancel_event)
        result["zip"] = [target]
        emit(f"手机 {label}：拉取最新导出包 {os.path.basename(latest)}")
        # 展开包内每台设备的固件日志（主设备 + 从设备），平铺到 <机型>/<SN>-<时间戳>/
        groups = extract_bundle_devices(target, dev_root, emit=emit)
        result["devices"] = groups
        emit(f"手机 {label}：导出包内展开 {len(groups)} 组设备日志")
    else:
        result["zip"] = []
        result["devices"] = []
        emit(f"手机 {label}：没有找到导出包，跳过")
    prog(34)

    # 2) sdk 日志
    _check_cancel()
    result["sdk"] = _pull_one_dir(adb, serial, REMOTE_DIRS["sdk-logs"],
                                  os.path.join(dev_root, "sdk日志"), label,
                                  "sdk日志", emit, cancel_event=cancel_event)
    prog(67)

    # 3) 固件日志（手机侧落盘的完整副本；zip 落进 App 私有 cache 时只能靠它）
    #    同样平铺：<机型>/<SN>-<时间戳>/，不再套「固件日志/」这一层。
    _check_cancel()
    result["firmware"] = _pull_firmware_flat(adb, serial, dev_root, label, emit,
                                             cancel_event=cancel_event)
    prog(100)

    if not any(result.values()):
        raise MobileExportError(
            f"手机 {serial} 上没有任何可导出日志。请先在手机 EGOViewer 里点「导出日志」"
            "生成 zip 后再试。")
    return result


def _pull_firmware_flat(adb: str, serial: str, dev_root: str, label: str,
                        emit, cancel_event=None) -> List[str]:
    """拉手机 firmware_logs 并平铺到机型目录下（<SN>-<时间戳>/ 直接挂机型）。

    先拉到纯 ASCII 临时目录，再按分组挪进 dev_root，避免 Windows adb 对中文
    路径的转码问题。目录不存在/为空时静默跳过。
    """
    remote = REMOTE_DIRS["firmware_logs"]
    tmp_root = tempfile.mkdtemp(prefix="adbfw_")
    try:
        tmp_target = os.path.join(tmp_root, "payload")
        r = _run(adb, ["-s", serial, "pull", remote, tmp_target], timeout=900,
                 cancel_event=cancel_event)
        err = r.stderr.decode("utf-8", "replace").strip()
        if r.returncode != 0:
            if "does not exist" in err or "No such file" in err:
                return []
            if "Permission denied" in err:
                emit(f"手机 {label}：读取固件日志被拒绝，跳过")
                return []
            raise MobileExportError(f"手机 {label} 拉取固件日志失败：{err or '未知错误'}")
        src = tmp_target if os.path.isdir(tmp_target) else tmp_root
        got: List[str] = []
        for name in sorted(os.listdir(src)):
            src_dir = os.path.join(src, name)
            if not os.path.isdir(src_dir):
                continue
            dst_dir = os.path.join(dev_root, name)
            if os.path.exists(dst_dir):
                # 与 zip 解开的分组同名：保留先到的一份，其余加后缀，绝不覆盖
                stem, n = name, 2
                while os.path.exists(dst_dir):
                    dst_dir = os.path.join(dev_root, f"{stem}_{n}")
                    n += 1
            shutil.move(src_dir, dst_dir)
            for dp, _d, fs in os.walk(dst_dir):
                got.extend(os.path.join(dp, f) for f in fs)
        if got:
            emit(f"手机 {label}：固件日志缓存平铺 {len(got)} 个文件")
        return got
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def latest_zip(adb: str, serial: str, cancel_event=None) -> str:
    """返回手机上最新那个 ego-logs-app-*.zip 的完整路径；没有则空串。"""
    r = _run(adb, ["-s", serial, "shell", "ls", REMOTE_DIRS["app-logs-zips"]], timeout=30,
             cancel_event=cancel_event)
    if r.returncode != 0:
        return ""
    names = [n.strip() for n in r.stdout.decode("utf-8", "replace").splitlines()
             if n.strip().lower().endswith(".zip")]
    if not names:
        return ""

    def _stamp(name: str) -> str:
        # ego-logs-app-20261020_114243_322.zip -> 20261020_114243
        m = re.search(r"(\d{8})[_](\d{6})", name)
        return (m.group(1) + m.group(2)) if m else ""

    names.sort(key=lambda n: (_stamp(n), n))
    return f"{REMOTE_DIRS['app-logs-zips']}/{names[-1]}"


def _pull_one_dir(adb: str, serial: str, remote: str, local: str,
                  label: str, what: str, emit, cancel_event=None) -> List[str]:
    """拉一个手机目录到 local；不存在视为空，权限问题给出可操作提示。

    _adb_pull_to 已经保证 local 就是源目录的内容本身（不再多套一层），
    所以这里直接用，不能再做压平——那会把 firmware_logs 下的设备分组目录
    （AZLV961004Y-.../）也抹掉。
    """
    try:
        _adb_pull_to(adb, serial, remote, local, what, label,
                     cancel_event=cancel_event)
    except MobileExportError as e:
        if "不存在" in str(e):
            emit(f"手机 {label}：{what} 不存在，跳过")
            return []
        raise
    files = [os.path.join(dp, f) for dp, _d, fs in os.walk(local) for f in fs]
    emit(f"手机 {label}：{what} 拉取 {len(files)} 个文件")
    return files


def _adb_pull_to(adb: str, serial: str, remote: str, target: str,
                 what: str, label: str, cancel_event=None) -> None:
    """把手机上的文件/目录拉到 target（可含中文），返回后 target 已就位。

    Windows 版 adb 用 GBK 转码目标路径，直接给中文目录会报
    "Illegal byte sequence" / "Not a directory"。所以统一先拉到纯 ASCII
    临时目录，再用 Python 挪到最终位置；Python 处理中文路径没有这个问题。
    """
    tmp_root = tempfile.mkdtemp(prefix="adbpull_")
    try:
        tmp_target = os.path.join(tmp_root, "payload")
        r = _run(adb, ["-s", serial, "pull", remote, tmp_target], timeout=900,
                 cancel_event=cancel_event)
        err = r.stderr.decode("utf-8", "replace").strip()
        if r.returncode != 0:
            if "does not exist" in err or "No such file" in err:
                raise MobileExportError("不存在")
            if "Permission denied" in err:
                raise MobileExportError(f"手机 {label} 读取 {remote} 被拒绝。{_HINT}")
            raise MobileExportError(f"手机 {label} 拉取 {what} 失败：{err or '未知错误'}")
        if not os.path.exists(tmp_target):
            raise MobileExportError(f"手机 {label} 拉取 {what} 失败：目标不存在")
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        if os.path.isdir(target):
            shutil.rmtree(target, ignore_errors=True)
        elif os.path.exists(target):
            os.remove(target)
        shutil.move(tmp_target, target)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def extract_bundle_devices(zip_path: str, phone_root: str,
                           emit=None) -> List[str]:
    """把导出包里 `firmware/<SN>-<时间戳>-<hash>/` 的**每台设备**解到
    `<phone_root>/<SN>-<时间戳>-<hash>/`，去掉中间的 firmware/ 分组层。

    这就是「多设备导出」的落地：手机一次导出会把主设备和所有从设备各打一个分组
    进同一个 zip，这里逐组解压，使每台设备在机型目录下各占一层，读者打开机型
    目录即可看到全部设备，无需再翻 firmware/。

    同时把包内顶层 app/ sdk/ diagnostics/（手机 App 自身日志）平铺到机型目录，
    与原来的 app/sdk 位置保持一致；`手机导出包.zip` 本体仍保留一份备查。

    返回解出的设备分组目录绝对路径列表。zip 损坏/无设备分组时不抛异常。
    """
    announce = emit or (lambda _t: None)
    if not os.path.isfile(zip_path):
        return []
    out_dirs: List[str] = []
    root = os.path.abspath(phone_root)
    try:
        with zipfile.ZipFile(zip_path) as z:
            members = z.infolist()
            fw_prefixes = _bundle_device_prefixes(members)
            # 同一台设备在包里可能有多个快照（AZL…-20260804、AZL…-20261109_150910、
            # AZL…-20261109_153234）：它们是历次导出累积的历史副本，内容高度重叠。
            # 全解出来会让目录里出现三份几乎一样的日志（现场表述「三份日志一样吗」）。
            # 因此按设备序列号归并，只保留**时间戳最新**的那一份快照。
            latest_by_sn = _latest_prefix_per_sn(fw_prefixes)
            for sn, prefix in sorted(latest_by_sn.items()):
                group = prefix.rstrip("/").split("/")[-1]
                # 目录名统一用设备序列号（与 USB 侧 <SN> 一致），不再拼整段分组名，
                # 避免「AZL9861004V-20261109_153234_206-6f342b60」这种长目录。
                dst_dir = os.path.join(root, _safe(sn, group))
                if os.path.exists(dst_dir):
                    stem, n = dst_dir, 2
                    while os.path.exists(dst_dir):
                        dst_dir = f"{stem}_{n}"
                        n += 1
                os.makedirs(dst_dir, exist_ok=True)
                base_len = len(prefix)
                for m in members:
                    name = m.filename
                    if not name.startswith(prefix) or m.is_dir():
                        continue
                    rel = name[base_len:]
                    dest = os.path.join(dst_dir, *rel.split("/"))
                    _safe_extract_member(z, m, dest)
                out_dirs.append(dst_dir)
            dropped = len(fw_prefixes) - len(latest_by_sn)
            if dropped:
                announce(f"导出包内 {len(fw_prefixes)} 个设备快照中，"
                         f"{dropped} 个是同一设备的旧副本，已只保留最新一份")
            # 2) 顶层 App 日志：app/ diagnostics/ 平铺到机型目录（不覆盖已有）
            #    **不**解包内顶层 sdk/：pull_all 已单独把手机上实时的 sdk-logs 拉到
            #    <机型>/sdk日志/，那是同一份日志的更新版本；再解开包内那份陈旧副本
            #    会让机型目录里出现两个 SDK 日志（现场表述「sdk 还导出两个」）。
            for m in members:
                name = m.filename
                head = name.split("/", 1)[0]
                if head not in ("app", "diagnostics") or m.is_dir():
                    continue
                dest = os.path.join(root, *name.split("/"))
                if not os.path.exists(dest):
                    _safe_extract_member(z, m, dest)
    except zipfile.BadZipFile:
        announce(f"导出包不是有效的 zip，跳过解包：{os.path.basename(zip_path)}")
        return out_dirs
    groups = sorted(os.path.basename(d) for d in out_dirs)
    if groups:
        announce("导出包内设备：" + "、".join(groups))
    return out_dirs


# 设备分组名：<SN>-<YYYYMMDD>_<HHMMSS>(-<hash>)?，取出 SN 与时间戳用于挑最新
_GROUP_SN_RE = re.compile(r"^([A-Za-z0-9]+?)-(\d{8})_(\d{6})")


def _latest_prefix_per_sn(prefixes: List[str]) -> Dict[str, str]:
    """按设备序列号归并快照，返回 {SN: 时间戳最新的那个分组前缀}。

    分组名形如 AZL9861004V-20261109_153234_206-6f342b60：
    SN=AZL9861004V，时间戳=20261109_153234。按 (时间戳) 取最大者。
    认不出 SN 的分组退化为用整段目录名做键（各算各的，不影响其它设备）。
    """
    best: Dict[str, tuple] = {}
    for prefix in prefixes:
        group = prefix.rstrip("/").split("/")[-1]
        m = _GROUP_SN_RE.match(group)
        if m:
            sn, stamp = m.group(1), m.group(2) + m.group(3)
            key = sn.upper()
        else:
            stamp, key = "", group
        if key not in best or stamp > best[key][0]:
            best[key] = (stamp, prefix)
    return {k: v[1] for k, v in best.items()}


def _bundle_device_prefixes(members) -> List[str]:
    """返回 zip 内所有 `firmware/<设备分组>-<时间戳>/` 的成员前缀（含末尾 /）。"""
    prefixes = set()
    for m in members:
        name = m.filename
        if not name.startswith("firmware/") or name.count("/") < 2:
            continue
        parts = name.split("/")
        idx = 1
        if len(parts) > 3 and parts[1] in _FW_SOURCE_DIRS:
            idx = 2
        if idx >= len(parts):
            continue
        group = parts[idx]
        if not _GROUP_RE.match(group):
            continue
        prefixes.add("/".join(parts[:idx + 1]) + "/")
    return sorted(prefixes)


def _safe_extract_member(z: zipfile.ZipFile, member, dest: str) -> None:
    """把单个成员写到 dest，拒绝逃逸出目标目录的路径（zip slip）。"""
    root = os.path.dirname(os.path.abspath(dest))
    os.makedirs(root, exist_ok=True)
    target = os.path.abspath(dest)
    if not (target == root or target.startswith(root + os.sep)):
        raise ValueError(f"压缩包包含不安全路径: {member.filename}")
    with z.open(member) as src, open(target, "wb") as fh:
        shutil.copyfileobj(src, fh)


def _flatten(local_dir: str, source_name: str = "") -> List[str]:
    """把 `adb pull <源目录> <local>` 多套出来的那一层剥掉。

    adb 的行为：目标不存在时，会把内容放进 `<local>/`；目标已存在时
    会再套一层以源目录名命名的子目录。剥壳只在**那一层名字等于源目录名**时才做，
    否则会把本该保留的设备分组目录（如 firmware_logs/AZLV.../）也压平。

    注：现行 pull_all 走「先拉 ASCII 临时目录再按设备分组挪位」，不再需要本函数；
    保留它是给手工排查/外部脚本处理 adb 多套层时用（有对应单测守行为）。
    """
    if not os.path.isdir(local_dir):
        return []
    if source_name:
        inner = os.path.join(local_dir, source_name)
        if os.path.isdir(inner):
            for name in os.listdir(inner):
                src = os.path.join(inner, name)
                dst = os.path.join(local_dir, name)
                if not os.path.exists(dst):
                    shutil.move(src, dst)
            try:
                os.rmdir(inner)
            except OSError:
                pass
    found = []
    for dirpath, _dirs, files in os.walk(local_dir):
        for f in files:
            found.append(os.path.join(dirpath, f))
    return found


def _folder_name(model: str, serial: str) -> str:
    """手机显示名 → 可用作目录名：Pixel_7 → Pixel 7。"""
    base = (model or "").replace("_", " ").strip() or serial
    return _safe(base, serial)


def _safe(name: str, fallback: str) -> str:
    keep = "-_.() abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    cleaned = "".join(c if c in keep else "_" for c in name).strip() or fallback
    return cleaned


# zip 内固件日志有两种排布，实测都出现过：
#   firmware/<SN>-<时间戳>-<hash>/...              （直接挂设备）
#   firmware/persisted/<SN>-<时间戳>-<hash>/...    （挂在 persisted 下）
# `persisted` 是来源类别，不是设备名，报成设备会误导。
_FW_SOURCE_DIRS = {"persisted"}

# 设备分组名形如 AZLV961004Y-20261020_100206_680-66437f2d：
# 大写序列号开头 + 时间戳。zip 里 firmware/ 下还可能有 app/ sdk/ diagnostics/
# 这类固定子目录，它们不是设备，不能混进设备分组。
_GROUP_RE = re.compile(r"^[A-Za-z0-9]+-\d{8}_\d{6}")


def _device_group_of(zip_name: str) -> str | None:
    """从 `firmware/...` 成员路径取出真实设备分组名；不是设备目录返回 None。"""
    parts = zip_name.split("/")
    idx = 1
    if len(parts) > 2 and parts[1] in _FW_SOURCE_DIRS:
        idx = 2
    if idx >= len(parts):
        return None
    name = parts[idx]
    return name if _GROUP_RE.match(name) else None


def summarize(dest_root: str) -> Dict[str, object]:
    """盘点 `<dest_root>/<机型>/` 下的产物，解析出设备分组。

    结构（见 pull_all）：

        Pixel 7/手机导出包.zip                  手机导出包（原样保留一份备查）
        Pixel 7/<SN>-<时间戳>-<hash>/           从导出包展开的每台设备（含从设备）
        Pixel 7/sdk日志/OrbbecSDK.log.txt

    设备分组直接平铺在机型目录下（不再套 firmware/ 或 固件日志/ 这一层）。
    既认「已经展开」的平铺目录，也兼容旧结构（firmware/、固件日志/ 下的分组），
    避免历史导出目录无法清点。zip 仍会读一遍，仅用于补报未展开的设备分组与
    手机 App 自身日志数量。

    返回 {"phones": [{"phone", "bundles": [{"source", "zip"|"dir",
    "devices":[{group,files}], "app_files"}]}]}；只读，不改动任何文件。
    """
    root = dest_root
    phones = []
    if not os.path.isdir(root):
        return {"phones": phones}
    # 平铺结构下电脑端的 app/ sdk/ firmware/ 与本函数的手机目录同级，
    # 只认带手机产物特征的目录（有导出包或设备分组目录），其余跳过。
    pc_dirs = {"app", "sdk", "firmware", "sdk日志"}
    for phone in sorted(os.listdir(root)):
        phone_dir = os.path.join(root, phone)
        if not os.path.isdir(phone_dir) or phone in pc_dirs:
            continue
        has_zip = bool(glob.glob(os.path.join(phone_dir, "*.zip")))
        device_dirs = _device_dirs_under(phone_dir)
        if not (has_zip or device_dirs):
            continue
        bundles = []
        seen_groups = set()

        # 1) 已平铺展开的设备目录：每台设备一个分组
        for grp_dir in device_dirs:
            grp = os.path.basename(grp_dir)
            files = sum(len(fs) for _dp, _ds, fs in os.walk(grp_dir))
            bundles.append({
                "source": "dir",
                "dir": grp_dir,
                "devices": [{"group": grp, "files": files}],
                "app_files": 0,
            })
            seen_groups.add(grp)

        # 2) 手机导出包（本次只拉最新那一个；补报尚未展开的设备分组）
        for zp in sorted(glob.glob(os.path.join(phone_dir, "*.zip"))):
            entry = {"source": "zip", "zip": zp, "devices": [], "app_files": 0}
            try:
                with zipfile.ZipFile(zp) as z:
                    names = z.namelist()
            except zipfile.BadZipFile:
                entry["error"] = "不是有效的 zip"
                bundles.append(entry)
                continue
            entry["app_files"] = sum(1 for n in names
                                     if n.startswith("app/") and not n.endswith("/"))
            groups: Dict[str, int] = {}
            for n in names:
                if not n.startswith("firmware/") or n.count("/") < 2:
                    continue
                g = _device_group_of(n)
                if not g or g in seen_groups:
                    continue
                groups[g] = groups.get(g, 0) + 1
            entry["devices"] = [{"group": g, "files": c} for g, c in sorted(groups.items())]
            bundles.append(entry)
        phones.append({"phone": phone, "bundles": bundles})
    return {"phones": phones}


def _device_dirs_under(phone_dir: str) -> List[str]:
    """机型目录下的设备分组目录（<SN>-<时间戳>-<hash>），含旧结构兼容。

    新结构直接平铺：<机型>/<SN>-<时间戳>-<hash>/；
    旧结构残留在 firmware/ 或 固件日志/ 下，也照样认出来，避免历史目录清点不到。
    """
    found: List[str] = []
    for name in sorted(os.listdir(phone_dir)):
        p = os.path.join(phone_dir, name)
        if not os.path.isdir(p):
            continue
        if _GROUP_RE.match(name):
            found.append(p)
        elif name in ("firmware", "固件日志"):
            for sub in sorted(os.listdir(p)):
                sp = os.path.join(p, sub)
                if os.path.isdir(sp) and _GROUP_RE.match(sub):
                    found.append(sp)
                elif name == "firmware" and sub in _FW_SOURCE_DIRS:
                    for sub2 in sorted(os.listdir(sp)):
                        sp2 = os.path.join(sp, sub2)
                        if os.path.isdir(sp2) and _GROUP_RE.match(sub2):
                            found.append(sp2)
    return found
