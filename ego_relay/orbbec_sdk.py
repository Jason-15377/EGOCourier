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
    """Assemble callback packets into files below one output directory."""

    def __init__(self, output_dir: str, progress_cb: Optional[Callable[[int, str], None]] = None):
        self.output_dir = Path(output_dir).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.progress_cb = progress_cb
        self.files: Dict[str, object] = {}
        self.total = 0
        self.completed = 0
        self.error: Optional[BaseException] = None
        self.paths = []

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
            self.total = int(total_file_count or self.total)
            self.completed = int(completed_file_count)
            if state == PACKET_BEGIN:
                old = self.files.pop(name, None)
                if old:
                    old.close()
                path = self.output_dir / name
                fh = path.open("wb")
                self.files[name] = fh
                self.paths.append(str(path))
            fh = self.files.get(name)
            if data and data_size and fh:
                fh.write(ctypes.string_at(data, data_size))
                fh.flush()
            if state == PACKET_END:
                if fh:
                    fh.close()
                    self.files.pop(name, None)
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


def export_firmware_logs(output_dir: str, sdk_root: str = "", device_index: int = 0,
                         timeout_s: float = 180.0,
                         progress_cb: Optional[Callable[[int, str], None]] = None,
                         cancel_event: Optional[threading.Event] = None,
                         device_ip: str = "", device_port: int = 8090) -> list[str]:
    """Export firmware logs from a network device (enumeration-first, official-style)."""
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
        if not device:
            raise OrbbecSdkError("SDK 获取设备句柄失败")

        CALLBACK = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_int,
                                    ctypes.c_void_p, ctypes.c_uint32,
                                    ctypes.c_uint32, ctypes.c_uint32,
                                    ctypes.c_void_p)

        def on_packet(name, state, data, data_size, total, completed, _user):
            writer.packet(name or b"", int(state), data, int(data_size), int(total), int(completed))
            if writer.error or (total and completed >= total) or state == PACKET_END and total == 0:
                finished.set()

        callback_ref = CALLBACK(on_packet)
        b.call(b.ob_device_start_firmware_log_export, device, callback_ref, None)
        while not finished.wait(0.2):
            if cancel_event and cancel_event.is_set():
                raise OrbbecSdkError("固件日志导出已取消")
            if writer.error:
                raise OrbbecSdkError(str(writer.error))
            if timeout_s is not None and time.monotonic() - start > timeout_s:
                raise OrbbecSdkError("等待固件日志导出完成超时")
        if writer.error:
            raise OrbbecSdkError(str(writer.error))
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
