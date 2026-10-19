"""Orbbec SDK firmware-log export adapter.

The SDK exports device firmware logs only. Device application logs remain an
SSH/SFTP concern and are deliberately not mixed into this module.
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional

from . import config


class OrbbecSdkError(RuntimeError):
    """An SDK loading, device, callback, or export error."""


# OBFwLogPacketState values from ObTypes.h.
PACKET_BEGIN = 0
PACKET_DATA = 1
PACKET_END = 2


def _candidate_dlls(sdk_root: str = ""):
    roots = []
    if sdk_root:
        roots.append(Path(sdk_root))
    if config.ORBBEC_SDK_ROOT:
        roots.append(Path(config.ORBBEC_SDK_ROOT))
    env = os.environ.get("ORBBEC_SDK_ROOT", "")
    if env:
        roots.append(Path(env))
    for root in roots:
        if root.is_file() and root.name.lower() == "orbbecsdk.dll":
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
        yield from matches


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
            raise OrbbecSdkError(f"加载 OrbbecSDK.dll 失败: {exc}") from exc
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
            raise OrbbecSdkError(f"OrbbecSDK.dll 缺少导出符号: {name}") from exc
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
        raise OrbbecSdkError("未找到 OrbbecSDK.dll，请在设置中指定 SDK 目录")
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
                             ) -> dict:
    """从**本机直连设备**各拉一份固件日志，按设备名分目录。

    USB 直连优先：GVCP 网络枚举会把整个局域网里的设备都扫出来（实测本机只有 3 台，
    却枚举到 5 台），因此默认跳过纯网络发现的设备，并在日志里说明跳过了谁。
    include_network=True 时连纯网络设备一起导（只有网线连接、没插 USB 的场景）。

    输出结构：<root_dir>/<设备名>/(其本次拉取产物)。同一设备因 USB + 网络重复枚举时
    只拉 USB 那条；同名设备（如两台 EGO-4B）在名称后追加序列号，避免互相覆盖。

    返回 {"ok": {设备目录: {"name","serial","connection","paths"}},
          "errors": [{"name","serial","error"}],
          "skipped": [{"name","serial","ip"}]}
    """
    all_devices = list_devices(sdk_root)
    if not all_devices:
        raise OrbbecSdkError("Orbbec SDK 未发现任何设备，请确认 USB / 网络链路已连接")
    devices = select_devices(all_devices, include_network=include_network)
    skipped = [d for d in all_devices if d not in devices]
    if skipped and note_cb:
        note_cb("跳过局域网设备（非 USB 直连）：" + "；".join(
            " · ".join(p for p in (d.get("name"), d.get("serial"),
                                   d.get("ip") and f"IP={d['ip']}") if p)
            for d in skipped))

    # 同名设备加序列号后缀区分
    name_counts = {}
    for d in devices:
        name_counts[d.get("name") or "device"] = name_counts.get(d.get("name") or "device", 0) + 1
    ok, errors = {}, []
    total = len(devices)
    for n, dev in enumerate(devices):
        if cancel_event is not None and cancel_event.is_set():
            raise OrbbecSdkError("固件日志导出已取消")
        base = dev.get("name") or "device"
        if name_counts.get(dev.get("name") or "device", 0) > 1 and dev.get("serial"):
            base = f"{base}_{dev['serial']}"
        folder = _safe_folder(base)
        label = " · ".join(p for p in (dev.get("name"), dev.get("serial"),
                                       dev.get("connection")) if p)
        if note_cb:
            note_cb(f"[{n + 1}/{total}] 拉取设备固件日志：{label} → {folder}")
        try:
            paths = export_firmware_logs(
                os.path.join(root_dir, folder), sdk_root=sdk_root,
                device_index=int(dev.get("index", 0)),
                timeout_s=timeout_s, cancel_event=cancel_event,
                firmware_idle_s=firmware_idle_s, note_cb=note_cb,
                progress_cb=(None if progress_cb is None else
                             (lambda pct, fname, _n=n: progress_cb(
                                 int((_n * 100 + pct) / max(total, 1)), fname))))
        except OrbbecSdkError as e:
            # 单台失败不影响其它设备，最后统一汇报
            errors.append({"name": dev.get("name"), "serial": dev.get("serial"),
                           "error": str(e)})
            if note_cb:
                note_cb(f"设备 {label} 拉取失败：{e}")
            continue
        ok[folder] = dict(dev, paths=paths)
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
                         firmware_idle_s: float = 30.0,
                         note_cb: Optional[Callable[[str], None]] = None) -> list[str]:
    """Export firmware logs from a network device (enumeration-first, official-style).

    firmware_idle_s：静默收尾阈值。设备在该时间内没有回任何新包即认为已传完。
    默认 30 秒：EGO-4B（固件 1.1.1）传 5 MB 级大文件时，包间隔实测可超过 20 秒，
    15 秒会在传到一半时误判为结束（现场表现为 .log.1 正好停在 5242880 字节）。
    note_cb：收尾/异常时的附加说明回调（如“设备未上报完成”“文件可能不完整”）。
    """
    dll = find_sdk_dll(sdk_root)
    if not dll:
        raise OrbbecSdkError("未找到 OrbbecSDK.dll，请在设置中指定 SDK 目录")
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
        if device_ip:
            device_list = b.call(b.ob_query_device_list, ctx)
            count = int(b.call(b.ob_device_list_get_count, device_list))
            target = device_ip.strip().lower()
            match = -1
            for i in range(count):
                try:
                    ip_raw = b.call(b.ob_device_list_get_device_ip_address, device_list, i)
                    ip = (ip_raw or b"").decode("utf-8", "replace").strip().lower()
                except OrbbecSdkError:
                    ip = ""
                if ip and ip == target:
                    match = i
                    break
            if match >= 0:
                device = b.call(b.ob_device_list_get_device, device_list, match)
                picked_index = match
        if device is None:
            # 未匹配到指定 IP 或未指定 IP：回退到手动建链（仍支持旧式调用方）
            if device_ip and not device_list:
                device = b.call(b.ob_create_net_device, ctx, device_ip.encode("utf-8"), int(device_port))
            else:
                if device_list is None:
                    device_list = b.call(b.ob_query_device_list, ctx)
                count = int(b.call(b.ob_device_list_get_count, device_list))
                if count <= device_index:
                    raise OrbbecSdkError("Orbbec SDK 未发现可导出的设备，请确认 USB/SDK 网络链路已连接")
                device = b.call(b.ob_device_list_get_device, device_list, int(device_index))
                picked_index = int(device_index)
        if not device:
            raise OrbbecSdkError("SDK 获取设备句柄失败")
        if note_cb and device_list is not None:
            note_cb(f"已选中设备 {_describe_device(b, device_list, picked_index)}")

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
                raise OrbbecSdkError("固件日志导出已取消")
            if writer.error:
                raise OrbbecSdkError(str(writer.error))
            if writer.paths and time.monotonic() - last_packet[0] > idle_s:
                silent_finish = True
                break
            if timeout_s is not None and time.monotonic() - start > timeout_s:
                raise OrbbecSdkError("等待固件日志导出完成超时")
        if writer.error:
            raise OrbbecSdkError(str(writer.error))
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
