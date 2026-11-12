"""Orbbec SDK firmware-log export adapter.

The SDK exports device firmware logs only. Device application logs remain an
SSH/SFTP concern and are deliberately not mixed into this module.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional

from . import config


class OrbbecSdkError(RuntimeError):
    """An SDK loading, device, callback, or export error.

    kind 用于让上层给用户精确的提示：把"缺 DLL"说成"设备未连接"会让用户排查方向
    完全错误。取值见下方注释。
    """

    #: dll         —— 找不到 / 加载不了 OrbbecSDK.dll
    #: no_device   —— 枚举不到任何设备
    #: busy        —— 设备被其它进程占用（典型：EGOViewer 正在用）
    #: fw_disabled —— 设备端固件日志功能未开启
    #: unresponsive —— 设备能枚举但读不动（回包 0 字节 / IO 失败，常见于已离线或固件卡死）
    #: timeout     —— 导出超时 / 静默收尾
    #: canceled    —— 用户取消
    #: other       —— 其它

    def __init__(self, message: str, kind: str = "other"):
        super().__init__(message)
        self.kind = kind


# 「设备暂时没响应」重试前的等待秒数。太短设备还没恢复，太长会让整轮导出白等；
# 2 秒足够让一次丢包/设备刚处理完上个请求的抖动过去。
FIRMWARE_RETRY_DELAY_S = 2.0


# SDK 原始错误串里用于识别失败类型的关键字（小写比较）。
_BUSY_HINTS = ("occup", "in use", "already open", "占用", "device busy", "resource")
_FWDIS_HINTS = ("firmware log", "already disabled", "disabled")
# 「枚举得到、却读不动」：设备回了 0 字节（连 10 字节响应头都没有），SDK 归类为 IO。
# 实测这类错误与具体设备强相关——同一台电脑、同一个 SDK，有的设备能导、有的导不了，
# EGOViewer 自己的日志里也用同样的串报过 device_enumeration_entry_unavailable，
# 因此它是设备侧链路/固件问题，不是调用方参数错误。
_UNRESPONSIVE_HINTS = (
    "device response size",      # Device response size(0) < response header size(10)
    "response header size",
    "unrecoverable_exception",
    "end_of_stream",
)


def _classify_error(err) -> str:
    """从 SDK 错误串推断失败类型。默认 other，不臆测。"""
    if isinstance(err, OrbbecSdkError) and getattr(err, "kind", "other") != "other":
        return err.kind
    low = str(err).lower()
    if any(h in low for h in _BUSY_HINTS):
        return "busy"
    if any(h in low for h in _FWDIS_HINTS):
        return "fw_disabled"
    if any(h in low for h in _UNRESPONSIVE_HINTS):
        return "unresponsive"
    return "other"


def is_transient_error(err) -> bool:
    """是否值得重试一次。只对「设备暂时没响应」这类抖动返回 True。

    判定刻意保守：设备被占用（busy）、固件日志关闭（fw_disabled）、缺 DLL
    （dll）都是稳定状态，重试只会白白多等一轮；只有 IO 无响应 / 超时这种
    一次性的链路抖动才重试。
    """
    return _classify_error(err) in ("unresponsive", "timeout")


# OBFwLogPacketState values from ObTypes.h.
PACKET_BEGIN = 0
PACKET_DATA = 1
PACKET_END = 2


def _candidate_dlls(sdk_root: str = ""):
    """按优先级产出候选 OrbbecSDK.dll 路径。

    优先级：显式参数 → 配置 → 环境变量 → 本工具自带 lib → EGOViewer 安装目录
            → 动态推导出的 EGOViewer 搜索根。
    后两项是"找不到也不报错"的兜底：EGOViewer 安装包本身带一份 OrbbecSDK.dll，
    装了 EGOViewer 的机器就应该能拉到固件日志，不该要求用户再手动配一次路径。
    """
    roots = []
    if sdk_root:
        roots.append(Path(sdk_root))
    if config.ORBBEC_SDK_ROOT:
        roots.append(Path(config.ORBBEC_SDK_ROOT))
    env = os.environ.get("ORBBEC_SDK_ROOT", "")
    if env:
        roots.append(Path(env))

    found_any = False
    for root in roots:
        if root.is_file() and root.name.lower() == "orbbecsdk.dll":
            found_any = True
            yield root
            continue
        if not root.is_dir():
            continue
        try:
            matches = list(root.rglob("OrbbecSDK.dll"))
        except OSError:
            continue
        # Prefer the SDK runtime beside its import library over the Viewer copy.
        matches.sort(key=lambda p: ("\\bin\\" not in str(p).lower(), len(str(p))))
        if matches:
            found_any = True
        yield from matches

    if found_any:
        return

    # ---- 兜底：上面没给路径，或给了路径但里面没有 DLL ----
    # 1) 本工具自带 runtime/lib（便携版可随包携带一份）
    for base in _tool_roots():
        for rel in (Path("lib") / "OrbbecSDK.dll",
                    Path("runtime") / "lib" / "OrbbecSDK.dll"):
            d = base / rel
            if d.is_file():
                yield d
    # 2) 已检测到的 EGOViewer 安装目录（最常见、最可靠）
    try:
        from . import evlog_pack
        for ev in evlog_pack.find_ego_viewer_roots():
            for rel in (Path("OrbbecSDK.dll"), Path("bin") / "OrbbecSDK.dll"):
                p = Path(ev) / rel
                if p.is_file():
                    yield p
        # 3) EGOViewer 搜索根本身（用户把 DLL 直接放在某层目录下的情况）
        for sr in evlog_pack._candidate_search_roots():
            p = Path(sr) / "OrbbecSDK.dll"
            if p.is_file():
                yield p
    except Exception:  # noqa: BLE001
        pass


def _tool_roots() -> list:
    """本工具可能的根目录（源码模式：runtime/app 的上一级；打包模式：exe 同级）。"""
    out = []
    base = Path(__file__).resolve().parent          # .../app/ego_relay
    app_dir = base.parent                           # .../app
    out.extend([app_dir, app_dir.parent])           # .../app, .../runtime
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        out.insert(0, exe_dir)
        out.insert(1, exe_dir / "runtime")
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        out.insert(0, Path(meipass))
    return [p for p in out if p.is_dir()]


def find_sdk_dll(sdk_root: str = "") -> str:
    """Return the first existing OrbbecSDK.dll, or an empty string."""
    seen = set()
    for candidate in _candidate_dlls(sdk_root):
        key = str(candidate.resolve()).lower()
        if key not in seen and candidate.is_file():
            seen.add(key)
            return str(candidate.resolve())
    return ""


class FirmwareLogWriter:
    """Assemble callback packets into files below one output directory.

    设备按「文件」逐个回传：每个文件以 PACKET_BEGIN 开一个包、若干 PACKET_DATA
    续传、PACKET_END 收尾；total/completed 是**整批**的文件计数。因此写入器必须
    同时支持多个文件句柄并存（SDK 可能交错回传不同文件），不能按名字 pop/覆盖。
    """

    def __init__(self, output_dir: str, progress_cb: Optional[Callable[[int, str], None]] = None):
        self.output_dir = Path(output_dir).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.progress_cb = progress_cb
        self.files: Dict[str, object] = {}
        self.total = 0
        self.completed = 0
        self.error: Optional[BaseException] = None
        self.paths = []
        self._seen: set = set()   # 已开过包的文件名（用于去重 paths）
        self.unfinished: set = set()  # 开了包但没收到 PACKET_END 的文件（收尾时报警）

    def _safe_name(self, file_name: str) -> str:
        if not file_name or file_name in (".", "..") or "\x00" in file_name:
            raise OrbbecSdkError("SDK 返回了非法固件日志文件名")
        if "/" in file_name or "\\" in file_name:
            raise OrbbecSdkError(f"拒绝带路径的固件日志文件名: {file_name!r}")
        name = Path(file_name).name
        if name != file_name:
            raise OrbbecSdkError(f"拒绝不安全的固件日志文件名: {file_name!r}")
        return name

    def packet(self, file_name: bytes | str, state: int, data, data_size: int,
               total_file_count: int, completed_file_count: int) -> None:
        try:
            if isinstance(file_name, bytes):
                file_name = file_name.decode("utf-8", "replace")
            name = self._safe_name(file_name)
            if total_file_count:
                self.total = max(self.total, int(total_file_count))
            self.completed = max(self.completed, int(completed_file_count))
            fh = self.files.get(name)
            if state == PACKET_BEGIN:
                # 重新开始同一个文件时先关掉旧句柄（同一文件多次回传取最后一次）
                if fh:
                    fh.close()
                path = self.output_dir / name
                fh = path.open("wb")
                self.files[name] = fh
                self.unfinished.add(name)
                if name not in self._seen:
                    self._seen.add(name)
                    self.paths.append(str(path))
            elif fh is None:
                # 未收到 BEGIN 直接来 DATA/END：按新文件容错，避免丢内容
                path = self.output_dir / name
                fh = path.open("wb")
                self.files[name] = fh
                self.unfinished.add(name)
                if name not in self._seen:
                    self._seen.add(name)
                    self.paths.append(str(path))
            if data and data_size and fh:
                fh.write(ctypes.string_at(data, data_size))
                fh.flush()
            if state == PACKET_END:
                if fh:
                    fh.close()
                    self.files.pop(name, None)
                self.unfinished.discard(name)
            if self.progress_cb:
                pct = int(self.completed * 100 / self.total) if self.total else 0
                self.progress_cb(pct, name)
        except BaseException as exc:  # callback must not escape into native code
            self.error = exc
            self.close()

    def close(self) -> None:
        for fh in list(self.files.values()):
            try:
                fh.close()
            except Exception:
                pass
        self.files.clear()


class _SdkBindings:
    def __init__(self, dll_path: str):
        self.dll_path = dll_path
        dll_dir = str(Path(dll_path).parent)
        self._dll_dir = None
        if hasattr(os, "add_dll_directory"):
            try:
                self._dll_dir = os.add_dll_directory(dll_dir)
            except OSError:
                pass
        try:
            self.lib = ctypes.CDLL(dll_path)
        except OSError as exc:
            if self._dll_dir:
                self._dll_dir.close()
            raise OrbbecSdkError(f"加载 OrbbecSDK.dll 失败: {exc}", kind="dll") from exc
        P = ctypes.c_void_p
        E = ctypes.POINTER(P)
        self._bind("ob_create_context", P, [E])
        self._bind("ob_delete_context", None, [P, E])
        self._bind("ob_query_device_list", P, [P, E])
        self._bind("ob_create_net_device", P, [P, ctypes.c_char_p, ctypes.c_uint16, E])
        self._bind("ob_delete_device_list", None, [P, E])
        self._bind("ob_device_list_get_count", ctypes.c_uint32, [P, E])
        self._bind("ob_device_list_get_device", P, [P, ctypes.c_uint32, E])
        self._bind("ob_device_list_get_device_ip_address", ctypes.c_char_p, [P, ctypes.c_uint32, E])
        self._bind("ob_device_list_get_device_connection_type", ctypes.c_char_p, [P, ctypes.c_uint32, E])
        # 仅用于日志里显示选中了哪台设备；老版本 DLL 可能没有这两个符号，
        # 缺失时跳过而不影响导出本身。
        for _opt in ("ob_device_list_get_device_name",
                     "ob_device_list_get_device_serial_number"):
            try:
                self._bind(_opt, ctypes.c_char_p, [P, ctypes.c_uint32, E])
            except OrbbecSdkError:
                pass
        self._bind("ob_enable_net_device_enumeration", None, [P, ctypes.c_bool, E])
        self._bind("ob_delete_device", None, [P, E])
        self._bind("ob_device_start_firmware_log_export", None,
                   [P, ctypes.c_void_p, P, E])
        self._bind("ob_device_stop_firmware_log_export", None, [P, E])
        self._bind("ob_error_get_message", ctypes.c_char_p, [P])
        self._bind("ob_delete_error", None, [P])

    def _bind(self, name, restype, argtypes):
        try:
            fn = getattr(self.lib, name)
        except AttributeError as exc:
            raise OrbbecSdkError(f"OrbbecSDK.dll 缺少导出符号: {name}", kind="dll") from exc
        fn.restype = restype
        fn.argtypes = argtypes
        setattr(self, name, fn)

    def close(self):
        if self._dll_dir:
            self._dll_dir.close()
            self._dll_dir = None

    def error_text(self, err) -> str:
        if not err:
            return "未知 SDK 错误"
        try:
            raw = self.ob_error_get_message(err)
            text = raw.decode("utf-8", "replace") if raw else "未知 SDK 错误"
        finally:
            self.ob_delete_error(err)
        return text

    def call(self, fn, *args):
        err = ctypes.c_void_p()
        result = fn(*args, ctypes.byref(err))
        if err.value:
            raise OrbbecSdkError(self.error_text(err))
        return result


def _describe_device(b, device_list, index: int) -> str:
    """把列表中第 index 个设备描述成 "EGO-4B 序列号=... 连接=... IP=..."，便于核对选错设备。"""
    try:
        name = b.call(b.ob_device_list_get_device_name, device_list, int(index))
        name = (name or b"").decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001
        name = ""
    try:
        sn = b.call(b.ob_device_list_get_device_serial_number, device_list, int(index))
        sn = (sn or b"").decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001
        sn = ""
    try:
        conn = b.call(b.ob_device_list_get_device_connection_type, device_list, int(index))
        conn = (conn or b"").decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001
        conn = ""
    parts = [p for p in (name, f"序列号={sn}" if sn else "",
                         f"连接={conn}" if conn else "") if p]
    return " · ".join(parts) if parts else f"索引 {index}"


def is_local_connection(dev: dict) -> bool:
    """是否本机直连（USB）。纯网络枚举到的设备可能属于局域网里别人/别处的机器。"""
    conn = (dev.get("connection") or "").lower()
    ip = (dev.get("ip") or "").strip()
    if "usb" in conn:
        return True
    # 无连接类型字段时，用 IP 兜底：USB 设备不报 IP（0.0.0.0）
    return not conn and (not ip or ip == "0.0.0.0")


def _same_device(a: dict, b: dict) -> bool:
    """两个设备条目是否指同一台设备：优先比序列号，其次比 IP，最后比 dict 相等。"""
    sa = str(a.get("serial") or "").strip().upper()
    sb = str(b.get("serial") or "").strip().upper()
    if sa and sb:
        return sa == sb
    ia = str(a.get("ip") or "").strip()
    ib = str(b.get("ip") or "").strip()
    if ia and ib and ia != "0.0.0.0":
        return ia == ib
    return a == b


def _dedupe(devices: list[dict]) -> list[dict]:
    """按 _same_device 去重，保留首次出现的顺序（dict 不可哈希，不能直接用 set）。"""
    out: list[dict] = []
    for d in devices:
        if not any(_same_device(d, x) for x in out):
            out.append(d)
    return out


def load_device_memory() -> dict:
    """读取「导出过的设备」记忆；文件缺失或损坏时返回空字典。

    结构 {SN大写: {"count": 次数, "last": 最后导出时间, "name":..., "ip":...}}。
    """
    path = getattr(config, "FIRMWARE_DEVICE_MEMORY", None)
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 —— 记忆文件坏了不能影响导出
        return {}


def remember_devices(devices) -> None:
    """把本次成功导出的设备写入记忆（累加次数、更新最后时间）。

    只记录成功拉到的设备，避免「失败一次就把这台算进历史优先」。
    """
    path = getattr(config, "FIRMWARE_DEVICE_MEMORY", None)
    if not path or not devices:
        return
    mem = load_device_memory()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    for d in devices:
        sn = str(d.get("serial") or "").strip().upper()
        if not sn:
            continue
        rec = mem.get(sn) or {"count": 0}
        rec["count"] = int(rec.get("count") or 0) + 1
        rec["last"] = stamp
        if d.get("name"):
            rec["name"] = d["name"]
        if d.get("ip"):
            rec["ip"] = d["ip"]
        mem[sn] = rec
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(mem, fh, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass


def _memory_rank(dev: dict, mem: dict) -> tuple:
    """设备在「历史优先」里的排序键：次数多的在前，其次时间新的在前。"""
    sn = str(dev.get("serial") or "").strip().upper()
    rec = mem.get(sn) or {}
    return (-int(rec.get("count") or 0), str(rec.get("last") or ""))


def _closest_devices(devices: list[dict], limit: int, note_cb=None) -> list[dict]:
    """按 ping 延迟从快到慢取前 limit 台网络设备（近似「离得近」）。

    固件日志拉取单台往往十几秒到一分钟，一网段七八台会把整次导出拖到几分钟。
    用户要求「以太网优先按最近的 3 台导出」。以太网没有蓝牙 RSSI，只能用往返
    延迟近似距离；不可达/超时的设备排在最后，只在名额还有剩余时才会被选中。
    """
    from . import wifi as wifi_mod

    scored = []
    for d in devices:
        ip = (d.get("ip") or "").strip()
        ms = wifi_mod.ping_ms(ip, timeout_ms=300) if ip else 1e9
        scored.append((ms, d))
    scored.sort(key=lambda x: x[0])
    picked = [d for _ms, d in scored[:limit]]
    if note_cb:
        def _label(ms, d):
            sn = d.get("serial") or d.get("name") or "?"
            return f"{sn}({'超时' if ms >= 1e6 else f'{ms:.0f}ms'})"

        note_cb(f"以太网设备共 {len(devices)} 台，按延迟取最近 {len(picked)} 台："
                + "、".join(_label(ms, d) for ms, d in scored[:limit]))
        if len(devices) > limit:
            note_cb(f"其余 {len(devices) - limit} 台以太网设备未拉取（已限制为最近 "
                    f"{limit} 台，避免导出过慢）")
    return picked


def _subnet_of(ip: str) -> str:
    """返回 IP 的 /24 网段前缀（如 '192.168.1'）；非法或空 IP 返回空串。

    用于「配网 IP 与设备是否同一套网络」的判断：同一 /24 内的网络设备才属于
    当前这套系统（主设备 + 左右指环等从设备），跨网段的多是别人的机器。
    """
    ip = (ip or "").strip()
    if not ip or ip == "0.0.0.0":
        return ""
    parts = ip.split(".")
    if len(parts) != 4:
        return ""
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return ""
    if any(n < 0 or n > 255 for n in nums):
        return ""
    return ".".join(str(n) for n in nums[:3])


def select_devices(devices: list[dict], include_network: bool = False) -> list[dict]:
    """挑选本次要导出的设备：**USB 直连优先**。

    GVCP 网络枚举会把整个局域网里所有 Orbbec 设备都扫出来（实测本机插 3 台，
    却枚举到 5 台，多出的两台在 192.168.1.x 上），因此默认只导 USB 直连的设备。
    include_network=True 时才连纯网络设备一起导（用于只有网线连接的场景）。
    若一台 USB 都没有，则退回全部设备，避免整批为空。
    """
    usb = [d for d in devices if is_local_connection(d)]
    if usb:
        return usb + ([d for d in devices if d not in usb] if include_network else [])
    return list(devices)


def list_devices(sdk_root: str = "") -> list[dict]:
    """枚举当前所有可导出固件日志的设备，返回 [{name, serial, connection, ip, index}]。

    同一台设备常同时以 USB 和 192.168.x.x:8090 两种方式被枚举到（EGOViewer 的日志里
    就是这样），按**序列号**去重并优先保留 USB 链路——它不依赖网络，更稳定。
    """
    dll = find_sdk_dll(sdk_root)
    if not dll:
        raise OrbbecSdkError("未找到 OrbbecSDK.dll。该 DLL 由 EGOViewer 自带，请确认已安装 EGOViewer", kind="dll")
    b = _SdkBindings(dll)
    ctx = device_list = None
    try:
        ctx = b.call(b.ob_create_context)
        try:
            b.call(b.ob_enable_net_device_enumeration, ctx, True)
        except OrbbecSdkError:
            pass
        device_list = b.call(b.ob_query_device_list, ctx)
        count = int(b.call(b.ob_device_list_get_count, device_list))
        found = []
        for i in range(count):
            info = _device_info(b, device_list, i)
            info["index"] = i
            found.append(info)
        return _dedupe_by_serial(found)
    finally:
        for fn, arg in ((getattr(b, "ob_delete_device_list", None), device_list),
                        (getattr(b, "ob_delete_context", None), ctx)):
            if arg:
                try:
                    b.call(fn, arg)
                except Exception:  # noqa: BLE001
                    pass
        b.close()


def _device_info(b, device_list, index: int) -> dict:
    def _str(fn_name):
        try:
            raw = b.call(getattr(b, fn_name), device_list, int(index))
            return (raw or b"").decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001
            return ""
    return {
        "name": _str("ob_device_list_get_device_name"),
        "serial": _str("ob_device_list_get_device_serial_number"),
        "connection": _str("ob_device_list_get_device_connection_type"),
        "ip": _str("ob_device_list_get_device_ip_address"),
    }


def _dedupe_by_serial(devices: list[dict]) -> list[dict]:
    """按序列号去重；同一台设备的 USB 与网络两条枚举记录只留 USB。"""
    def _is_usb(d: dict) -> bool:
        return "usb" in (d.get("connection") or "").lower()

    chosen = {}
    for d in devices:
        key = d.get("serial") or f"{d.get('name')}#{d.get('index')}"
        old = chosen.get(key)
        if old is None or (_is_usb(d) and not _is_usb(old)):
            chosen[key] = d
    return sorted(chosen.values(), key=lambda d: d.get("index", 0))


def export_all_firmware_logs(root_dir: str, sdk_root: str = "",
                             timeout_s: float = 180.0,
                             cancel_event: Optional[threading.Event] = None,
                             note_cb: Optional[Callable[[str], None]] = None,
                             firmware_idle_s: float = 15.0,
                             progress_cb: Optional[Callable[[int, str], None]] = None,
                             include_network: bool = False,
                             prefer_ip: str = "",
                             prefer_port: int = 0,
                             skip_serials=(),
                             max_network: Optional[int] = None,
                             only_serials=(),
                             ) -> dict:
    """从**本机 USB 直连的设备**各拉一份固件日志，按序列号分目录。

    只导 USB：GVCP 网络枚举会把整个局域网里所有 Orbbec 设备都扫出来（实测本机插
    3 台，却枚举到 5 台以上），而以太网/Wi-Fi 上的设备属于谁无法从 SDK 信息判断，
    靠网段、延迟、历史次数都只能猜，结果时多时少。USB 是物理连接，插了就是本机的。
    因此自动模式下**非 USB 直连的设备一律不导**，并在日志里说明跳过了谁。
    一台 USB 都没有（纯网线现场）时退回读取全部设备，避免整批为空。

    include_network=True 仅用于调用方**明确**要求"连网络设备一起导"；
    手动勾选（only_serials）同样是明确指定，此时不受 USB 限制。

    prefer_ip：配网 IP。它只影响拉取顺序（命中设备排最前），不再决定拉几台。

    输出结构：<root_dir>/<设备名>/(其本次拉取产物)。同一设备因 USB + 网络重复枚举时
    只拉 USB 那条；同名设备（如两台 EGO-4B）在名称后追加序列号，避免互相覆盖。

    返回 {"ok": {设备目录: {"name","serial","connection","paths"}},
          "errors": [{"name","serial","error","kind"}],
          "skipped": [{"name","serial","ip"}]}
    """
    all_devices = list_devices(sdk_root)
    if not all_devices:
        raise OrbbecSdkError("Orbbec SDK 未发现任何设备，请确认 USB / 网络链路已连接",
                             kind="no_device")

    # 用户在界面上**手动勾选**了具体设备：只导这些，其余全部跳过。
    # 这是最高优先级——自动策略（历史优先 / 按延迟）再怎么做也可能猜错，
    # 让用户直接指定是唯一可靠的兜底。勾了但已离线的设备会提示出来。
    want = {str(s).strip().upper() for s in (only_serials or ()) if str(s).strip()}
    if want:
        manual = [d for d in all_devices
                  if str(d.get("serial") or "").strip().upper() in want]
        missing = want - {str(d.get("serial") or "").strip().upper() for d in manual}
        if note_cb:
            note_cb("按手动勾选导出设备：" + "、".join(
                str(d.get("serial") or d.get("name")) for d in manual)
                if manual else "手动勾选的设备当前都不在线")
            if missing:
                note_cb("以下勾选设备未找到（可能已离线）：" + "、".join(sorted(missing)))
        if not manual:
            raise OrbbecSdkError(
                "手动勾选的设备都不在线，请刷新设备列表后重新勾选", kind="no_device")
        all_devices = manual
        # 手动模式不走网段筛选，但保留以太网限额与「配网设备优先」：
        # prefer_ip 只影响排序，不会把没勾的设备带进来（all_devices 已裁好）。
    elif not any(is_local_connection(d) for d in all_devices):
        # 一台 USB 都没有：自动模式本来只留 USB，会挑出空集合。这种现场（只用
        # 网线/Wi-Fi 连）退回老语义——全部设备都导，否则导出结果为空。
        if note_cb:
            note_cb("没有检测到 USB 直连设备，改为导出全部可访问设备"
                    f"（{len(all_devices)} 台）")
        # 与纯网线现场换位：no_usb 时下面的「只导 USB」分支不能再把设备全剔掉
        net_only = True
    else:
        net_only = False

    # 配网 IP 命中的设备优先拉（排最前）。注意：自动模式只导 USB，网络设备会被
    # 下面「只导 USB」那步剔除，所以这句话只在它确实入选（USB/手动）时才提示。
    by_ip: list[dict] = []
    if prefer_ip:
        target_ip = prefer_ip.strip().lower()
        hit = [d for d in all_devices
               if (d.get("ip") or "").strip().lower() == target_ip]
        if hit and (want or is_local_connection(hit[0])):
            by_ip = hit
            if note_cb:
                note_cb(f"配网设备 {prefer_ip} 优先拉取")
        elif note_cb and not hit:
            note_cb(f"枚举列表中未找到 IP {prefer_ip}，改为按连接方式挑选")

    # 其余设备的选择策略：**只导本机 USB 直连的设备**。
    # 现场要求（用户原话）：「电脑 USB 识别到的设备导出，非 USB 识别的设备不导出」。
    # 理由：以太网/Wi-Fi 上的设备是谁的说不清——GVCP 广播会把整个局域网扫一遍，
    # 同网段既有自己的从设备，也有别人机器上的设备，靠网段/延迟/历史都猜不准，
    # 只能是「有时多导了几台，有时漏了」，结果不稳定。USB 是物理连接，插了就是
    # 本机的，不会有歧义。
    # 因此：
    #   · 配网 IP / 网段 / 延迟 / 历史记忆这些网络侧启发式一律不再参与自动挑选；
    #   · 用户**手动勾选**（only_serials）仍然有效，那是明确指定，不受此限；
    #   · 一台 USB 都没有时，退回按 include_network 的老语义（默认取全部设备），
    #     避免纯网线现场导出为空。
    rest = [d for d in all_devices if d not in by_ip]
    usb_rest = [d for d in rest if is_local_connection(d)]
    net_rest = [d for d in rest if not is_local_connection(d)]
    net_pick: list[dict] = []
    if want:
        # 手动勾选模式：all_devices 已被裁成勾选的那几台，全部纳入，不再筛选
        net_pick = net_rest
    elif net_only:
        # 纯网线现场（一台 USB 都没有）：所有网络设备都导，否则结果为空
        net_pick = list(by_ip) + net_rest
        by_ip = []
    else:
        # 自动模式：网络设备一律不进（含配网 IP 命中的那台，除非它是 USB）
        net_pick = []
        dropped_net = list(by_ip) + net_rest
        by_ip = [d for d in by_ip if is_local_connection(d)]
        if dropped_net and note_cb:
            note_cb("只导出电脑 USB 直连的设备，以下 "
                    f"{len(dropped_net)} 台以太网/Wi-Fi 设备不导："
                    + "；".join(
                        " · ".join(p for p in (d.get("name"),
                                               d.get("serial") or d.get("name") or "?",
                                               d.get("ip") and f"IP={d['ip']}") if p)
                        for d in dropped_net))

    # 已经被其它来源（手机导出包）覆盖的设备：先剔除，避免同一台设备被拉两份
    # （目录里出现带时间戳后缀的重名文件）。
    skip = {str(s).strip().upper() for s in (skip_serials or ()) if str(s).strip()}
    if skip:
        def _covered(d):
            return (d.get("serial") or "").strip().upper() in skip
        covered = [d for d in (by_ip + usb_rest + net_pick) if _covered(d)]
        if covered:
            by_ip = [d for d in by_ip if not _covered(d)]
            usb_rest = [d for d in usb_rest if not _covered(d)]
            net_pick = [d for d in net_pick if not _covered(d)]
            if note_cb:
                note_cb("手机上已有这些设备的日志，不再重复拉取："
                        + "、".join((d.get("serial") or "?") for d in covered))

    # 以太网设备限速：固件日志单台往往十几秒到一分钟，一网段七八台会把整次导出
    # 拖到几分钟，所以最多拉 max_network 台。挑选顺序（重要）：
    #   1) 历史记忆里**导出次数多 / 最近导出过**的设备优先——用户真正想要的是他
    #      反复在用的那几台。按延迟猜「远近」并不可靠：同一交换机内几毫秒的差异
    #      纯属抖动，同一批设备每次排序都可能不同（现场反馈「选的三台不是我想要的」）。
    #   2) 没有历史的设备才按 ping 延迟排序补位。
    #   3) 配网设备（prefer_ip 命中）保底，它一定是用户当下在操作的那台。
    # 注意：自动模式只导 USB 设备，by_ip/net_pick 此时都为空，这段不会触发；
    # 只有手动勾选、或一台 USB 都没有的纯网线现场才会用到。
    if max_network is not None:
        limit = int(max_network)
        net_all = by_ip + net_pick
        if len(net_all) > limit:
            mem = load_device_memory()
            # 有历史的排前面（按次数/时间），无历史的按延迟排后面
            known = [d for d in net_all
                     if str(d.get("serial") or "").strip().upper() in mem]
            unknown = [d for d in net_all
                       if str(d.get("serial") or "").strip().upper() not in mem]
            known.sort(key=lambda d: _memory_rank(d, mem))
            picked = list(known[:limit])
            if len(picked) < limit:
                picked += _closest_devices(unknown, limit - len(picked))
            # 配网设备保底：它一定是用户当下在操作的那台，不能被挤掉。
            # 名额已满时挤掉最不优先的那台（先进先出：picked 末尾是优先级最低的）。
            for d in by_ip:
                if any(_same_device(d, x) for x in picked):
                    continue
                if len(picked) >= limit:
                    picked.pop()
                picked.insert(0, d)
            kept = _dedupe(picked)
            by_ip = [d for d in by_ip if any(_same_device(d, k) for k in kept)]
            net_pick = [d for d in kept
                        if not any(_same_device(d, b) for b in by_ip)]
            if note_cb:
                names = "、".join(
                    f"{(d.get('serial') or d.get('name') or '?')}"
                    f"（历史 {int((mem.get(str(d.get('serial') or '').strip().upper()) or {}).get('count') or 0)} 次）"
                    if str(d.get("serial") or "").strip().upper() in mem
                    else f"{(d.get('serial') or d.get('name') or '?')}（按延迟）"
                    for d in kept)
                note_cb(f"以太网设备共 {len(net_all)} 台，按历史优先取 {len(kept)} 台：{names}")
                if len(net_all) > limit:
                    note_cb(f"其余 {len(net_all) - limit} 台未拉取"
                            f"（已限制为 {limit} 台，避免导出过慢）")

    devices = by_ip + usb_rest + net_pick
    # 手动模式已裁成勾选集合；自动模式下若真的空（USB 全没选上），再兜一次
    if not devices:
        devices = select_devices(all_devices, include_network=(include_network or not want))

    skipped = [d for d in all_devices if d not in devices]
    if skipped and note_cb and want:
        note_cb(f"跳过 {len(skipped)} 台设备（未勾选）")
    port = int(prefer_port or config.ORBBEC_SDK_PORT or 8090)
    # 设备目录名必须全局唯一：EGOViewer 的"设备名"经常重复（两台都叫 UMI Finger，
    # 固件上报的 EGO-4B 甚至没有型号名）。只用设备名会让后一台把前一台的产物
    # 覆盖/混在一起，用户看到"三份日志一模一样"。序列号才唯一，故一律带上它。
    ok, errors = {}, []
    total = len(devices)
    for n, dev in enumerate(devices):
        if cancel_event is not None and cancel_event.is_set():
            raise OrbbecSdkError("固件日志导出已取消", kind="canceled")
        name = (dev.get("name") or "").strip() or "device"
        serial = (dev.get("serial") or "").strip()
        folder = _safe_folder(f"{name}_{serial}" if serial else name)
        taken = {_safe_folder(k) for k in ok} | {
            _safe_folder(f"{e.get('name') or 'device'}_{e.get('serial') or ''}".strip("_"))
            for e in errors}
        if folder in taken:
            folder = _safe_folder(f"{folder}_{dev.get('index', n)}")
        label = " · ".join(p for p in (dev.get("name"), serial or "(无序列号)",
                                       dev.get("connection")) if p)
        if note_cb:
            note_cb(f"[{n + 1}/{total}] 拉取设备固件日志：{label} → {folder}")

        def _attempt(attempt_no: int):
            return export_firmware_logs(
                os.path.join(root_dir, folder), sdk_root=sdk_root,
                device_index=int(dev.get("index", 0)),
                # 序列号一路传下去精确选设备（枚举顺序会漂移，index 不可靠）；
                # 网络枚举到的设备再带上 IP 兜底。USB 设备 IP 为空，行为不变。
                device_serial=serial,
                device_ip=(dev.get("ip") or ""), device_port=port,
                timeout_s=timeout_s, cancel_event=cancel_event,
                firmware_idle_s=firmware_idle_s, note_cb=note_cb,
                progress_cb=(None if progress_cb is None else
                             (lambda pct, fname, _n=n: progress_cb(
                                 int((_n * 100 + pct) / max(total, 1)), fname))))

        try:
            try:
                paths = _attempt(1)
            except OrbbecSdkError as e:
                # 设备"回了 0 字节 / IO 超时"这类抖动重试一次往往就好了（实测同一批
                # 设备里能导的与导不了的随机分布）。只重试瞬态错误：被占用、固件日志
                # 关闭、缺 DLL 都是稳定状态，重试只会让用户多等一轮。
                if (cancel_event is not None and cancel_event.is_set()) \
                        or not is_transient_error(e):
                    raise
                if note_cb:
                    note_cb(f"设备 {label} 首次拉取失败（{e}），"
                            f"{FIRMWARE_RETRY_DELAY_S:g} 秒后重试一次…")
                if cancel_event is not None:
                    if cancel_event.wait(FIRMWARE_RETRY_DELAY_S):
                        raise OrbbecSdkError("固件日志导出已取消", kind="canceled")
                else:
                    time.sleep(FIRMWARE_RETRY_DELAY_S)
                paths = _attempt(2)
                if note_cb:
                    note_cb(f"设备 {label} 重试成功：{len(paths)} 个文件")
        except OrbbecSdkError as e:
            # 单台失败不影响其它设备，最后统一汇报（带 kind 供上层给精确提示）。
            # kind 走 _classify_error 再定一次：底层抛出的原始串（如 statusCode: 8）
            # 没带 kind 时，到这里才能被识别成 unresponsive，而不是笼统的 other。
            errors.append({"name": dev.get("name"), "serial": dev.get("serial"),
                           "error": str(e), "kind": _classify_error(e)})
            if note_cb:
                note_cb(f"设备 {label} 拉取失败：{e}")
            continue
        ok[folder] = dict(dev, paths=paths)
    # 成功拉到日志的设备记进「历史记忆」，下次导出优先选它们
    if ok:
        remember_devices(list(ok.values()))
    return {"ok": ok, "errors": errors, "skipped": skipped}


def _report_incomplete_files(writer: "FirmwareLogWriter", note_cb: Callable[[str], None]) -> None:
    """收尾时检查有没有文件没收到 PACKET_END，提示哪些文件可能不完整。

    设备端行为不一致（EGO-4B 固件 1.1.1 会在 5 MB 边界处断包），只提示不删除：
    用户至少能拿到截断前的全部内容，但要知道它不完整，避免当成完整日志去分析。
    """
    if not writer.unfinished:
        return
    names = sorted(writer.unfinished)
    sizes = []
    for n in names:
        p = writer.output_dir / n
        try:
            sizes.append(f"{n}（{p.stat().st_size} 字节）")
        except OSError:
            sizes.append(n)
    note_cb(
        f"警告：{len(names)} 个文件未收到结束包，可能不完整：" + "、".join(sizes))


def _safe_folder(name: str) -> str:
    """把设备名转成安全的目录名（去掉路径分隔符与 Windows 非法字符）。"""
    cleaned = "".join("_" if c in '<>:"/\\|?*' or ord(c) < 32 else c for c in name)
    return cleaned.strip(" .") or "device"


def export_firmware_logs(output_dir: str, sdk_root: str = "", device_index: int = 0,
                         timeout_s: float = 180.0,
                         progress_cb: Optional[Callable[[int, str], None]] = None,
                         cancel_event: Optional[threading.Event] = None,
                         device_ip: str = "", device_port: int = 8090,
                         device_serial: str = "",
                         firmware_idle_s: float = 30.0,
                         note_cb: Optional[Callable[[str], None]] = None) -> list[str]:
    """Export firmware logs from a network device (enumeration-first, official-style).

    firmware_idle_s：静默收尾阈值。设备在该时间内没有回任何新包即认为已传完。
    默认 30 秒：EGO-4B（固件 1.1.1）传 5 MB 级大文件时，包间隔实测可超过 20 秒，
    15 秒会在传到一半时误判为结束（现场表现为 .log.1 正好停在 5242880 字节）。
    note_cb：收尾/异常时的附加说明回调（如“设备未上报完成”“文件可能不完整”）。

    device_serial / device_index 的优先级：
      1) device_serial 与枚举到的设备序列号**完全相等** → 用那一条；
      2) device_ip 与枚举到的 IP 相等 → 用那一条；
      3) 都不命中 → 才按 device_index 取列表第 N 条（旧式调用）。
    多设备场景必须把序列号一路传下来：枚举顺序在多次 query 之间并不稳定，只按
    index 选会让三台设备都拉到同一台（现场表现为"三份固件日志内容完全一样"）。
    命中设备后还会用描述的序列号复核，不符直接报错，绝不把 A 设备的日志写进
    B 设备的目录。
    """
    dll = find_sdk_dll(sdk_root)
    if not dll:
        raise OrbbecSdkError("未找到 OrbbecSDK.dll。该 DLL 由 EGOViewer 自带，请确认已安装 EGOViewer", kind="dll")
    b = _SdkBindings(dll)
    ctx = device_list = device = None
    callback_ref = None
    writer = FirmwareLogWriter(output_dir, progress_cb)
    finished = threading.Event()
    start = time.monotonic()
    try:
        ctx = b.call(b.ob_create_context)
        # 贴近官方实现：先启用网络设备枚举（GVCP），通过 queryDeviceList 自动发现
        # 局域网内的设备，再按 IP 匹配。EGOViewer 即走此路径，能导出固件日志。
        # 仅当枚举失败时才回退到手动 create_net_device(ip, port) 建链。
        try:
            b.call(b.ob_enable_net_device_enumeration, ctx, True)
        except OrbbecSdkError:
            pass  # 老固件可能不支持，忽略后继续枚举
        device = None
        device_list = None
        picked_index = -1          # 最终选中的设备在列表中的下标（用于日志说明）
        want_serial = (device_serial or "").strip().lower()
        if want_serial or device_ip:
            device_list = b.call(b.ob_query_device_list, ctx)
            count = int(b.call(b.ob_device_list_get_count, device_list))
            target_ip = (device_ip or "").strip().lower()
            match = -1
            for i in range(count):
                info = _device_info(b, device_list, i)
                # 序列号优先：它才是设备的唯一身份，index / IP 都会随枚举漂移
                if want_serial and (info.get("serial") or "").strip().lower() == want_serial:
                    match = i
                    break
                if (match < 0 and target_ip
                        and (info.get("ip") or "").strip().lower() == target_ip):
                    match = i          # IP 命中先记下，继续找有没有序列号更准的
            if match >= 0:
                device = b.call(b.ob_device_list_get_device, device_list, match)
                picked_index = match
        if device is None:
            # 未匹配到指定设备或未指定：回退到手动建链（仍支持旧式调用方）
            if device_ip and not device_list:
                device = b.call(b.ob_create_net_device, ctx, device_ip.encode("utf-8"), int(device_port))
            else:
                if device_list is None:
                    device_list = b.call(b.ob_query_device_list, ctx)
                count = int(b.call(b.ob_device_list_get_count, device_list))
                if count <= device_index:
                    raise OrbbecSdkError("Orbbec SDK 未发现可导出的设备，请确认 USB/SDK 网络链路已连接", kind="no_device")
                device = b.call(b.ob_device_list_get_device, device_list, int(device_index))
                picked_index = int(device_index)
        if not device:
            raise OrbbecSdkError("SDK 获取设备句柄失败", kind="no_device")
        described = (_describe_device(b, device_list, picked_index)
                     if device_list is not None else "")
        # 复核：指定了序列号就必须真的是那台。宁可这一台报错，也不能把别的设备的
        # 日志写进它的目录（否则用户会拿错日志去分析，且完全看不出来）。
        if want_serial and picked_index >= 0:
            got = (_device_info(b, device_list, picked_index).get("serial") or "").strip().lower()
            if got != want_serial:
                raise OrbbecSdkError(
                    f"选中的设备序列号不符：期望 {device_serial}，实际 {got or '(空)'}",
                    kind="other")
        if note_cb and described:
            note_cb(f"已选中设备 {described}")

        CALLBACK = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_int,
                                    ctypes.c_void_p, ctypes.c_uint32,
                                    ctypes.c_uint32, ctypes.c_uint32,
                                    ctypes.c_void_p)

        last_packet = [time.monotonic()]   # 最近一次收到回调包的时刻（静默兜底用）

        def on_packet(name, state, data, data_size, total, completed, _user):
            last_packet[0] = time.monotonic()
            writer.packet(name or b"", int(state), data, int(data_size), int(total), int(completed))
            # 整批结束的条件：SDK 报出的总文件数都已收完。绝不能只凭单个文件的
            # PACKET_END 或 completed>=total 提前触发，否则会在第一个文件收完后
            # 就停掉导出，后续文件（messages.log*/observice.console.log*）全部丢失。
            if writer.error:
                finished.set()
            elif writer.total and writer.completed >= writer.total:
                finished.set()

        callback_ref = CALLBACK(on_packet)
        b.call(b.ob_device_start_firmware_log_export, device, callback_ref, None)
        # 收尾判定（两条任一满足即结束）：
        #   1) SDK 报完总文件数且已全部收到 —— 正常完成；
        #   2) 静默兜底 —— 已有文件落盘、且连续 idle_s 秒没有任何新包。
        # 第 2 条对 total 是否有值都要生效：EGO-4B（固件 1.1.1）会在传完大部分文件后
        # 不再回任何包，且 total 与实际文件数对不上，只靠总数判定会一直等到超时。
        idle_s = firmware_idle_s
        silent_finish = False
        while not finished.wait(0.2):
            if cancel_event and cancel_event.is_set():
                raise OrbbecSdkError("固件日志导出已取消", kind="canceled")
            if writer.error:
                raise OrbbecSdkError(str(writer.error), kind=_classify_error(writer.error))
            if writer.paths and time.monotonic() - last_packet[0] > idle_s:
                silent_finish = True
                break
            if timeout_s is not None and time.monotonic() - start > timeout_s:
                raise OrbbecSdkError("等待固件日志导出完成超时", kind="timeout")
        if writer.error:
            raise OrbbecSdkError(str(writer.error), kind=_classify_error(writer.error))
        if silent_finish and note_cb:
            note_cb(
                f"设备未上报完成（已收 {len(writer.paths)} 个文件，"
                f"{idle_s:.0f} 秒无新数据），按静默超时收尾")
        if note_cb:
            _report_incomplete_files(writer, note_cb)
        return list(writer.paths)
    finally:
        writer.close()
        if device:
            try:
                b.call(b.ob_device_stop_firmware_log_export, device)
            except Exception:
                pass
            try:
                b.call(b.ob_delete_device, device)
            except Exception:
                pass
        if device_list:
            try:
                b.call(b.ob_delete_device_list, device_list)
            except Exception:
                pass
        if ctx:
            try:
                b.call(b.ob_delete_context, ctx)
            except Exception:
                pass
        b.close()
