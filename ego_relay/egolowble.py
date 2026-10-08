"""EgoLowBle.dll 的 ctypes 包装 —— EGO 设备 BLE 配网（按官方 egolowble.h 对齐）。

EGOViewer 通过 EgoLowBle.dll 完成 BLE 配网：扫描蓝牙 → 连接设备 → 配置 Wi-Fi
（SSID/密码各 ≤32 个 UTF-8 字节）→ 设备加入局域网 → 拿到 IP。本模块用 ctypes
直接加载该 DLL 调用其导出函数。

签名来源：厂商 SDK `EgoLowBle-windows-x64-*/include/EgoLowBle/egolowble.h`。
所有函数返回 `EgoLowBleStatus` 枚举（0=OK）；句柄由 `EgoLowBle_Create` 出参返回。

DLL 定位顺序（find_dll）：
  1. 显式参数 / config.EGOLOWBLE_DLL
  2. 环境变量 EGOLOWBLE_DLL
  3. 本工具安装目录下 lib/（lib/EgoLowBle.dll）
  4. 扫描 EGOViewer / EgoLowBle SDK 安装目录
"""

from __future__ import annotations

import ctypes
import os
from ctypes import POINTER, c_char_p, c_int, c_void_p, c_size_t, c_uint8, c_uint16, c_int16

from . import config


class BleError(RuntimeError):
    """BLE 配网相关错误（DLL 缺失、加载失败、调用失败等）。"""


# ---------------------------------------------------------------------------
# 状态码（egolowble.h: EgoLowBleStatus）
# ---------------------------------------------------------------------------
class BleStatus:
    OK = 0
    INVALID_ARGUMENT = 1
    ADAPTER_NOT_FOUND = 2
    DEVICE_NOT_FOUND = 3
    NOT_CONNECTED = 4
    BUFFER_TOO_SMALL = 5
    BLUETOOTH_UNAVAILABLE = 6
    OPERATION_FAILED = 7
    TIMEOUT = 8
    PROTOCOL_ERROR = 9
    REMOTE_ERROR = 10
    NOT_CONFIGURED = 11


_STATUS_TEXT = {
    0: "OK", 1: "INVALID_ARGUMENT", 2: "ADAPTER_NOT_FOUND", 3: "DEVICE_NOT_FOUND",
    4: "NOT_CONNECTED", 5: "BUFFER_TOO_SMALL", 6: "BLUETOOTH_UNAVAILABLE",
    7: "OPERATION_FAILED", 8: "TIMEOUT", 9: "PROTOCOL_ERROR", 10: "REMOTE_ERROR",
    11: "NOT_CONFIGURED",
}


def _status_text(rc: int) -> str:
    return _STATUS_TEXT.get(rc, f"UNKNOWN({rc})")


# ---------------------------------------------------------------------------
# 结构体（egolowble.h）
# ---------------------------------------------------------------------------
class EgoLowBleDeviceInfo(ctypes.Structure):
    """ScanDevices 返回的单台设备信息。"""
    _fields_ = [
        ("device_name", ctypes.c_char * 256),
        ("device_address", ctypes.c_char * 64),
        ("rssi", c_int16),
        ("is_connectable", c_uint8),
    ]


class EgoLowBleCharacteristicInfo(ctypes.Structure):
    _fields_ = [
        ("service_uuid", ctypes.c_char * 40),
        ("characteristic_uuid", ctypes.c_char * 40),
        ("can_read", c_uint8),
        ("can_write_request", c_uint8),
        ("can_write_command", c_uint8),
        ("can_notify", c_uint8),
        ("can_indicate", c_uint8),
    ]


class EgoLowBleProtocolResponseInfo(ctypes.Structure):
    _fields_ = [
        ("opcode", c_uint16),
        ("request_id", c_uint16),
        ("device_error_code", c_uint16),
    ]


class EgoLowBleWifiConfigResponse(ctypes.Structure):
    _fields_ = [
        ("result", c_uint8),                 # 0=成功, 1=失败
        ("reason", ctypes.c_char * 65),
    ]


class EgoLowBleIpResponse(ctypes.Structure):
    _fields_ = [
        ("result", c_uint8),                 # EgoLowBleIpResult
        ("ip", ctypes.c_char * 33),
        ("reason", ctypes.c_char * 33),
    ]


class EgoLowBleWifiScanResult(ctypes.Structure):
    _fields_ = [
        ("ssid", ctypes.c_char * 33),
    ]


# ---------------------------------------------------------------------------
# 函数签名（restype, argtypes）—— 按 egolowble.h 逐条核对
# ---------------------------------------------------------------------------
_SIGS = {
    "EgoLowBle_Create":                  (c_int, [POINTER(c_void_p)]),
    "EgoLowBle_Destroy":                 (None, [c_void_p]),
    "EgoLowBle_ConnectByName":           (c_int, [c_void_p, c_char_p, c_int]),
    "EgoLowBle_ConnectByAddress":        (c_int, [c_void_p, c_char_p, c_int]),
    "EgoLowBle_Disconnect":              (c_int, [c_void_p]),
    "EgoLowBle_IsConnected":             (c_int, [c_void_p, POINTER(c_int)]),
    "EgoLowBle_GetMtu":                  (c_int, [c_void_p, POINTER(c_uint16)]),
    "EgoLowBle_ScanDevices":             (c_int, [c_void_p, c_int,
                                                   POINTER(EgoLowBleDeviceInfo),
                                                   POINTER(c_size_t)]),
    "EgoLowBle_ListCharacteristics":     (c_int, [c_void_p,
                                                   POINTER(EgoLowBleCharacteristicInfo),
                                                   POINTER(c_size_t)]),
    "EgoLowBle_Read":                    (c_int, [c_void_p, c_char_p, c_char_p,
                                                   POINTER(POINTER(c_uint8)),
                                                   POINTER(c_size_t)]),
    "EgoLowBle_WriteRequest":            (c_int, [c_void_p, c_char_p, c_char_p,
                                                   POINTER(c_uint8), c_size_t]),
    "EgoLowBle_WriteCommand":            (c_int, [c_void_p, c_char_p, c_char_p,
                                                   POINTER(c_uint8), c_size_t]),
    "EgoLowBle_ConfigureWifi":           (c_int, [c_void_p, c_char_p, c_char_p,
                                                   c_int, POINTER(EgoLowBleWifiConfigResponse)]),
    "EgoLowBle_RequestIp":               (c_int, [c_void_p, c_int, POINTER(EgoLowBleIpResponse)]),
    "EgoLowBle_ScanWifi":                (c_int, [c_void_p, c_char_p, c_uint16, c_uint16,
                                                   c_int, POINTER(POINTER(EgoLowBleWifiScanResult)),
                                                   POINTER(c_size_t), POINTER(c_uint8)]),
    "EgoLowBle_SetWifiConfig":           (c_int, [c_void_p, c_char_p, c_char_p,
                                                   POINTER(EgoLowBleProtocolResponseInfo)]),
    "EgoLowBle_GetIpAddress":            (c_int, [c_void_p, c_char_p, c_size_t,
                                                   POINTER(c_size_t),
                                                   POINTER(EgoLowBleProtocolResponseInfo)]),
    "EgoLowBle_SyncWristBluetoothNames": (c_int, [c_void_p, POINTER(c_char_p), c_size_t]),
    "EgoLowBle_FreeMemory":              (None, [c_void_p]),
    "EgoLowBle_GetLastError":            (c_int, [c_void_p, c_char_p, c_size_t, POINTER(c_size_t)]),
}


def check_wifi_bytes(ssid: str, password: str) -> None:
    """校验 Wi-Fi 名称/密码长度（各 ≤32 个 UTF-8 字节，见 EGOViewer 手册）。"""
    for label, val in (("SSID", ssid), ("密码", password)):
        n = len(val.encode("utf-8"))
        if n > config.BLE_WIFI_MAX_BYTES:
            raise BleError(
                f"{label} 超过 {config.BLE_WIFI_MAX_BYTES} 个 UTF-8 字节，当前 {n} 字节"
                f"（中文每字 3 字节，实际字符数 {len(val)}）")


# ---------------------------------------------------------------------------
# DLL 定位
# ---------------------------------------------------------------------------
def _find_in_dir(root: str) -> str:
    if not root or not os.path.isdir(root):
        return ""
    try:
        entries = list(os.scandir(root))
    except OSError:
        return ""
    for entry in entries:
        try:
            if entry.is_dir() and (entry.name.startswith("EGOViewer_")
                                   or entry.name.startswith("EgoLowBle-")):
                # EGOViewer: <dir>/EgoLowBle.dll；SDK: <dir>/bin/EgoLowBle.dll
                for rel in ("EgoLowBle.dll", "bin/EgoLowBle.dll"):
                    p = os.path.join(entry.path, rel)
                    if os.path.isfile(p):
                        return p
            elif entry.is_file() and entry.name.lower() == "egolowble.dll":
                return entry.path
        except OSError:
            continue
    return ""


def find_dll(explicit: str = "") -> str:
    """按优先级定位 EgoLowBle.dll，找不到返回空串。"""
    candidates = []
    if explicit:
        candidates.append(explicit)
    if config.EGOLOWBLE_DLL:
        candidates.append(config.EGOLOWBLE_DLL)
    candidates.append(os.environ.get("EGOLOWBLE_DLL", ""))
    here = os.path.dirname(os.path.abspath(__file__))          # ego_relay/
    project = os.path.dirname(here)                            # 项目根
    candidates.append(os.path.join(project, "lib", "EgoLowBle.dll"))
    candidates.append(os.path.join(here, "EgoLowBle.dll"))
    for probe in (project, os.path.join(project, ".."),
                  r"D:\Soft&tools\软件工具", r"D:\Soft&tools", r"D:\file"):
        found = _find_in_dir(probe)
        if found:
            candidates.append(found)

    for c in candidates:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    return ""


# ---------------------------------------------------------------------------
# 高层包装
# ---------------------------------------------------------------------------
class EgoLowBle:
    """EgoLowBle.dll 面向配网的高层句柄包装（方法名保持既有约定）。"""

    def __init__(self, dll_path: str = ""):
        self._dll_path = find_dll(dll_path)
        if not self._dll_path:
            raise BleError(
                "未找到 EgoLowBle.dll。请把厂商 SDK 的 bin/EgoLowBle.dll 放到本工具 lib/ 下，"
                "或在 config.EGOLOWBLE_DLL / 环境变量 EGOLOWBLE_DLL 指定路径。")
        try:
            self._lib = ctypes.CDLL(self._dll_path)
        except OSError as e:
            raise BleError(f"加载 EgoLowBle.dll 失败: {e}") from e
        self._fn = {}
        self._bind()
        self._handle = None
        self._open = False

    def _bind(self) -> None:
        """绑定导出函数；缺少必要符号视为损坏的 DLL。"""
        for name, (restype, argtypes) in _SIGS.items():
            try:
                fn = getattr(self._lib, name)
            except AttributeError:
                continue
            if restype is not None:
                fn.restype = restype
            if argtypes:
                fn.argtypes = argtypes
            self._fn[name] = fn
        need = ("EgoLowBle_Create", "EgoLowBle_Destroy", "EgoLowBle_ScanDevices",
                "EgoLowBle_ConnectByAddress", "EgoLowBle_ConfigureWifi",
                "EgoLowBle_RequestIp", "EgoLowBle_IsConnected")
        missing = [n for n in need if n not in self._fn]
        if missing:
            raise BleError(f"EgoLowBle.dll 缺少必要导出符号: {', '.join(missing)}")

    # ------------------------------------------------------------- 句柄管理
    @property
    def dll_path(self) -> str:
        return self._dll_path

    def _ensure(self) -> None:
        if not self._open or not self._handle:
            raise BleError("BLE 尚未初始化，请先调用 open()")

    def _check(self, rc: int, what: str) -> int:
        """非 0 状态码统一抛 BleError（带状态文本与 last_error）。"""
        if rc != BleStatus.OK:
            detail = self.last_error() or ""
            msg = f"{what}失败(rc={rc} {_status_text(rc)})"
            if detail:
                msg += f"：{detail}"
            raise BleError(msg)
        return rc

    def open(self) -> None:
        if self._open:
            return
        out = c_void_p()
        rc = self._fn["EgoLowBle_Create"](ctypes.byref(out))
        if rc != BleStatus.OK or not out.value:
            detail = self.last_error() or ""
            raise BleError(
                f"EgoLowBle_Create 失败(rc={rc} {_status_text(rc)}){('：' + detail) if detail else ''}。"
                "请确认电脑蓝牙驱动正常。")
        self._handle = out
        self._open = True

    def close(self) -> None:
        if self._open and self._handle:
            try:
                self._fn["EgoLowBle_Destroy"](self._handle)
            except Exception:  # noqa: BLE001
                pass
        self._handle = None
        self._open = False

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def last_error(self) -> str:
        fn = self._fn.get("EgoLowBle_GetLastError")
        if not fn:
            return ""
        try:
            required = c_size_t(0)
            fn(self._handle, None, 0, ctypes.byref(required))
            n = int(required.value)
            if n <= 1:
                return ""
            buf = ctypes.create_string_buffer(n)
            rc = fn(self._handle, buf, n, ctypes.byref(required))
            if rc == BleStatus.OK:
                return buf.value.decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001
            pass
        return ""

    # ------------------------------------------------------------- 扫描/连接
    def scan_devices(self, timeout_ms: int = 0) -> list:
        """扫描 BLE 设备（两遍调用），返回 [{name,address,rssi,connectable}]。"""
        self._ensure()
        if timeout_ms <= 0:
            timeout_ms = int(config.BLE_SCAN_TIMEOUT_S * 1000)
        count = c_size_t(0)
        rc = self._fn["EgoLowBle_ScanDevices"](
            self._handle, timeout_ms, None, ctypes.byref(count))
        if rc == BleStatus.BUFFER_TOO_SMALL:
            rc = BleStatus.OK
        self._check(rc, "扫描 BLE 设备")
        n = int(count.value)
        if n <= 0:
            return []
        arr = (EgoLowBleDeviceInfo * n)()
        cnt = c_size_t(n)
        rc = self._fn["EgoLowBle_ScanDevices"](
            self._handle, timeout_ms, arr, ctypes.byref(cnt))
        if rc == BleStatus.BUFFER_TOO_SMALL:
            n = int(cnt.value)
            arr = (EgoLowBleDeviceInfo * n)()
            rc = self._fn["EgoLowBle_ScanDevices"](
                self._handle, timeout_ms, arr, ctypes.byref(cnt))
        self._check(rc, "扫描 BLE 设备")
        devices = []
        for i in range(int(cnt.value)):
            d = arr[i]
            devices.append({
                "name": d.device_name.decode("utf-8", "replace").strip("\x00"),
                "address": d.device_address.decode("utf-8", "replace").strip("\x00"),
                "rssi": int(d.rssi),
                "connectable": bool(d.is_connectable),
            })
        return devices

    def connect(self, address: str, timeout_ms: int = 0) -> None:
        """按 MAC 地址连接设备。"""
        self._ensure()
        if timeout_ms <= 0:
            timeout_ms = int(config.BLE_CONNECT_TIMEOUT_S * 1000)
        rc = self._fn["EgoLowBle_ConnectByAddress"](
            self._handle, address.encode("utf-8"), timeout_ms)
        self._check(rc, f"连接设备 {address}")

    def connect_by_name(self, name: str, timeout_ms: int = 0) -> None:
        self._ensure()
        if timeout_ms <= 0:
            timeout_ms = int(config.BLE_CONNECT_TIMEOUT_S * 1000)
        rc = self._fn["EgoLowBle_ConnectByName"](
            self._handle, name.encode("utf-8"), timeout_ms)
        self._check(rc, f"按名称连接 {name}")

    def is_connected(self) -> bool:
        self._ensure()
        out = c_int(0)
        rc = self._fn["EgoLowBle_IsConnected"](self._handle, ctypes.byref(out))
        if rc != BleStatus.OK:
            return False
        return bool(out.value)

    def disconnect(self) -> None:
        if self._open and self._handle:
            try:
                self._fn["EgoLowBle_Disconnect"](self._handle)
            except Exception:  # noqa: BLE001
                pass

    def get_mtu(self) -> int:
        self._ensure()
        out = c_uint16(0)
        self._check(self._fn["EgoLowBle_GetMtu"](self._handle, ctypes.byref(out)), "获取 MTU")
        return int(out.value)

    # ------------------------------------------------------------- 配网
    def configure_wifi(self, ssid: str, password: str,
                       timeout_ms: int = 0) -> dict:
        """下发 Wi-Fi 配置（A001 协议）。返回 {result, reason}（result=0 成功）。"""
        self._ensure()
        check_wifi_bytes(ssid, password)
        if timeout_ms <= 0:
            timeout_ms = 10000
        resp = EgoLowBleWifiConfigResponse()
        rc = self._fn["EgoLowBle_ConfigureWifi"](
            self._handle, ssid.encode("utf-8"), password.encode("utf-8"),
            timeout_ms, ctypes.byref(resp))
        self._check(rc, "配置 Wi-Fi")
        return {
            "result": int(resp.result),
            "reason": resp.reason.decode("utf-8", "replace").strip("\x00"),
        }

    def request_ip(self, timeout_ms: int = 0) -> dict:
        """请求设备 IP（A002 协议）。返回 {result, ip, reason}。"""
        self._ensure()
        if timeout_ms <= 0:
            timeout_ms = 10000
        resp = EgoLowBleIpResponse()
        rc = self._fn["EgoLowBle_RequestIp"](
            self._handle, timeout_ms, ctypes.byref(resp))
        self._check(rc, "请求设备 IP")
        return {
            "result": int(resp.result),
            "ip": resp.ip.decode("utf-8", "replace").strip("\x00"),
            "reason": resp.reason.decode("utf-8", "replace").strip("\x00"),
        }

    def get_ip(self, timeout_ms: int = 0) -> str:
        """读取设备 IP；尚未拿到时返回空串（用于轮询等待联网）。"""
        r = self.request_ip(timeout_ms)
        return r.get("ip", "")

    # 协议层旧接口（保留，便于调试/扩展）
    def set_wifi_config(self, ssid: str, password: str) -> None:
        self._ensure()
        info = EgoLowBleProtocolResponseInfo()
        rc = self._fn["EgoLowBle_SetWifiConfig"](
            self._handle, ssid.encode("utf-8"), password.encode("utf-8"),
            ctypes.byref(info))
        self._check(rc, "设置 Wi-Fi 配置")


def _parse_device(raw: bytes) -> dict:
    """保留：把旧的自由格式扫描字节解析为 {name,address}（现改为结构体解析，此函数备用）。"""
    text = raw.decode("utf-8", "replace").strip()
    for sep in ("|", ",", ";", "\t"):
        if sep in text:
            a, b = text.split(sep, 1)
            a, b = a.strip(), b.strip()
            if b.lower().startswith(("mac:", "addr:", "address:")):
                b = b.split(":", 1)[1].strip()
            return {"name": a, "address": b}
    return {"name": text, "address": ""}
