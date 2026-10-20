"""Android 手机 EGOViewer 日志导出（adb 通道）。

手机 EGOViewer 自己会把日志打包成 `ego-logs-app-<时间戳>_<ms>.zip` 放在应用外部目录：

    /sdcard/Android/data/com.egoviewer.app/files/
    ├── app-logs-zips/ego-logs-app-*.zip     ← App 日志 + 诊断 + SDK + 设备固件（连过设备时）
    ├── sdk-logs/OrbbecSDK.log.txt
    └── firmware_logs/                        ← 手机侧拉取落盘的固件日志缓存

本模块只做「把手机上的东西原样、完整地搬到电脑」，不解包重排、不做帧对齐：
App 自己的导出包结构（app/ sdk/ diagnostics/ firmware/<SN>-<时间戳>/）已经能直接
交给桌面端导入，重排反而会丢掉 firmware/<SN> 这层设备分组。

按设备分组体现在 `firmware/<SN>-<时间戳>-<hash>/`：手机一次导出会把当时连接的
**所有设备**各放一个子目录，所以「多设备分组」由 App 侧保证，这里只需保持原样。
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

# 导出目录里的两大类：开发拿到导出包先看这一层就能分清哪侧出的日志
PC_DIR = "电脑端"
PHONE_DIR = "手机端"

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


def _run(adb: str, args: List[str], timeout: float = 20.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [adb] + args, capture_output=True, timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


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
    """把一台手机的日志拉到 `<dest_root>/手机端/<机型>/`，只取最新一个导出包。

    目录结构（面向开发，一眼分清电脑端/手机端）：

        手机端/Pixel 7/
        ├── 手机导出包.zip            ← 最新那个 ego-logs-app-*.zip，原样不拆
        ├── sdk日志/                  ← OrbbecSDK.log.txt
        └── 固件日志/<SN>-<时间戳>-<hash>/   ← 手机侧落盘的完整副本

    只拉最新 zip：手机 app-logs-zips 会累积历次导出，全拉回来会让目录里
    堆满互相重复的历史包，开发反而分不清哪份是这次问题的。需要历史包时
    在手机上另行导出即可。
    """
    emit = note or (lambda _t: None)
    prog = progress or (lambda _p: None)
    model = ""
    try:
        r = _run(adb, ["-s", serial, "shell", "getprop", "ro.product.model"], timeout=10)
        model = r.stdout.decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001
        pass
    # 目录名不能带下划线（adb pull 会当成路径分隔符），所以 Pixel_7 → Pixel 7；
    # 日志里则用同样可读的显示名。
    label = _folder_name(model, serial)
    dev_root = os.path.join(dest_root, PHONE_DIR, label)
    result: Dict[str, list] = {}

    # 1) 最新导出包：按文件名里的时间戳挑最新，避免依赖 mtime（拷来拷去会变）
    if cancel_event is not None and cancel_event.is_set():
        raise MobileExportError("操作已取消")
    latest = latest_zip(adb, serial)
    if latest:
        os.makedirs(dev_root, exist_ok=True)
        target = os.path.join(dev_root, "手机导出包.zip")
        _adb_pull_to(adb, serial, latest, target, "导出包", label)
        result["zip"] = [target]
        emit(f"手机 {label}：拉取最新导出包 {os.path.basename(latest)}")
    else:
        result["zip"] = []
        emit(f"手机 {label}：没有找到导出包，跳过")
    prog(34)

    # 2) sdk 日志
    if cancel_event is not None and cancel_event.is_set():
        raise MobileExportError("操作已取消")
    result["sdk"] = _pull_one_dir(adb, serial, REMOTE_DIRS["sdk-logs"],
                                  os.path.join(dev_root, "sdk日志"), label,
                                  "sdk日志", emit)
    prog(67)

    # 3) 固件日志（手机侧落盘的完整副本；zip 落进 App 私有 cache 时只能靠它）
    if cancel_event is not None and cancel_event.is_set():
        raise MobileExportError("操作已取消")
    result["firmware"] = _pull_one_dir(adb, serial, REMOTE_DIRS["firmware_logs"],
                                       os.path.join(dev_root, "固件日志"), label,
                                       "固件日志", emit)
    prog(100)

    if not any(result.values()):
        raise MobileExportError(
            f"手机 {serial} 上没有任何可导出日志。请先在手机 EGOViewer 里点「导出日志」"
            "生成 zip 后再试。")
    return result


def latest_zip(adb: str, serial: str) -> str:
    """返回手机上最新那个 ego-logs-app-*.zip 的完整路径；没有则空串。"""
    r = _run(adb, ["-s", serial, "shell", "ls", REMOTE_DIRS["app-logs-zips"]], timeout=30)
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
                  label: str, what: str, emit) -> List[str]:
    """拉一个手机目录到 local；不存在视为空，权限问题给出可操作提示。

    _adb_pull_to 已经保证 local 就是源目录的内容本身（不再多套一层），
    所以这里直接用，不能再做压平——那会把 firmware_logs 下的设备分组目录
    （AZLV961004Y-.../）也抹掉。
    """
    try:
        _adb_pull_to(adb, serial, remote, local, what, label)
    except MobileExportError as e:
        if "不存在" in str(e):
            emit(f"手机 {label}：{what} 不存在，跳过")
            return []
        raise
    files = [os.path.join(dp, f) for dp, _d, fs in os.walk(local) for f in fs]
    emit(f"手机 {label}：{what} 拉取 {len(files)} 个文件")
    return files


def _adb_pull_to(adb: str, serial: str, remote: str, target: str,
                 what: str, label: str) -> None:
    """把手机上的文件/目录拉到 target（可含中文），返回后 target 已就位。

    Windows 版 adb 用 GBK 转码目标路径，直接给中文目录会报
    "Illegal byte sequence" / "Not a directory"。所以统一先拉到纯 ASCII
    临时目录，再用 Python 挪到最终位置；Python 处理中文路径没有这个问题。
    """
    tmp_root = tempfile.mkdtemp(prefix="adbpull_")
    try:
        tmp_target = os.path.join(tmp_root, "payload")
        r = _run(adb, ["-s", serial, "pull", remote, tmp_target], timeout=900)
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


def _flatten(local_dir: str, source_name: str = "") -> List[str]:
    """把 `adb pull <源目录> <local>` 多套出来的那一层剥掉。

    adb 的行为：目标不存在时，会把内容放进 `<local>/`；目标已存在时
    会再套一层以源目录名命名的子目录。剥壳只在**那一层名字等于源目录名**时才做，
    否则会把本该保留的设备分组目录（如 firmware_logs/AZLV.../）也压平。
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
    """盘点 `<dest_root>/手机端/<机型>/` 下的产物，解析出设备分组。

    结构（见 pull_all）：

        手机端/Pixel 7/手机导出包.zip                 手机导出包（原样，不拆）
        手机端/Pixel 7/固件日志/<SN>-<时间戳>-<hash>/   手机侧落盘的固件日志

    两条来源都可能有设备分组，且互相独立：zip 里的分组来自手机导出的那一刻，
    `固件日志/` 是手机 App 自己的持久化目录。某些机型上 zip 会落在 App 私有
    cache（adb 读不到），此时 `固件日志/` 是唯一完整的副本，必须单独统计。

    返回 {"phones": [{"phone", "bundles": [{"source", "zip"|"dir",
    "devices":[{group,files}], "app_files"}]}]}；只读，不改动任何文件。
    """
    root = os.path.join(dest_root, PHONE_DIR)
    phones = []
    if not os.path.isdir(root):
        return {"phones": phones}
    for phone in sorted(os.listdir(root)):
        phone_dir = os.path.join(root, phone)
        if not os.path.isdir(phone_dir):
            continue
        bundles = []

        # 1) 手机导出包（本次只拉最新那一个）
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
                if not g:
                    continue
                groups[g] = groups.get(g, 0) + 1
            entry["devices"] = [{"group": g, "files": c} for g, c in sorted(groups.items())]
            bundles.append(entry)

        # 2) 固件日志持久化目录
        fw_root = os.path.join(phone_dir, "固件日志")
        if os.path.isdir(fw_root):
            for grp in sorted(os.listdir(fw_root)):
                grp_dir = os.path.join(fw_root, grp)
                if not os.path.isdir(grp_dir):
                    continue
                files = sum(len(fs) for _dp, _ds, fs in os.walk(grp_dir))
                bundles.append({
                    "source": "firmware_logs",
                    "dir": grp_dir,
                    "devices": [{"group": grp, "files": files}],
                    "app_files": 0,
                })
        phones.append({"phone": phone, "bundles": bundles})
    return {"phones": phones}
