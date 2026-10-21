"""BLE 配网编排：扫描 → 连接 → 配置 Wi-Fi → 等待联网并拿 IP。

供 GUI（device_tab / export_tab）、Web API（api.py）、CLI 共用。底层用 EgoLowBle.dll。
"""

from __future__ import annotations

import time

from . import config
from .egolowble import EgoLowBle, BleError, find_dll


# 只为"认识的设备"展示：EGOViewer 手册 4.1 节说明扫描列表只显示受支持的设备
# （名称以上述前缀开头，或地址属于主要 EGO OUI）。本机实测扫到 50+ 台蓝牙设备，
# 不过滤的话目标设备会被鼠标/耳机/手机淹掉。
EGO_BLE_NAME_PREFIXES = ("EGO_", "UMIFinger_", "WristCam_", "EGOPro_",
                         "UMIGripper_", "EGODepth_")
EGO_BLE_OUI = ("54:14:FD",)


def dll_available() -> bool:
    """EgoLowBle.dll 是否可定位（配网功能是否可用）。"""
    return bool(find_dll())


def is_ego_ble_device(dev: dict) -> bool:
    """是否是 EGO 设备的 BLE 广播（按名称前缀或 OUI 判断）。

    名称/地址为空的条目直接排除：连不上也没有意义，且手册要求"名称、蓝牙地址
    均非空"才展示。
    """
    name = (dev.get("name") or "").strip()
    addr = (dev.get("address") or "").strip().upper()
    if not name or not addr:
        return False
    if name.startswith(EGO_BLE_NAME_PREFIXES):
        return True
    return addr.startswith(EGO_BLE_OUI)


def scan_devices(only_ego: bool = True) -> list:
    """扫描附近 BLE 设备，返回 [{name,address,rssi,connectable}]。

    过滤链：EGO 设备白名单 → 信号强度下限（距离过远）→ 按地址去重 → 按信号降序。
    only_ego=False 时不做白名单（排查"设备扫不到"时用得上）。
    """
    with EgoLowBle() as ble:
        devices = ble.scan_devices()
    if only_ego:
        devices = [d for d in devices if is_ego_ble_device(d)]
    rssi_min = getattr(config, "BLE_SCAN_RSSI_MIN", -70)
    devices = [d for d in devices if d.get("rssi", -100) >= rssi_min]
    # 同一台设备可能被扫到多次：按地址去重，保留信号最强的那条
    best = {}
    for d in devices:
        key = (d.get("address") or "").upper()
        if not key:
            continue
        if key not in best or d.get("rssi", -100) > best[key].get("rssi", -100):
            best[key] = d
    devices = list(best.values())
    devices.sort(key=lambda d: d.get("rssi", -100), reverse=True)
    return devices


def connect_and_provision(device_address: str, ssid: str, password: str,
                          join_timeout: float = None,
                          poll_step: float = 2.0,
                          status_cb=None) -> dict:
    """连接设备并下发 Wi-Fi 配置，等待设备联网，返回 {ip, connected}。

    device_address: 蓝牙 MAC 地址（来自 scan_devices）。
    ssid/password: 目标 Wi-Fi（password 开放网络可传空串）。
    join_timeout: 等待设备加入局域网并拿到 IP 的超时秒数。
    status_cb: 可选进度回调（GUI 用来实时显示"已等 N/40 秒"）。

    设备端拒绝配置（例如密码错误）时 ble.configure_wifi 会抛 BleError，这里不再
    吞掉；等待超时则返回 connected=False，由调用方决定如何呈现。
    """
    join_timeout = join_timeout or config.BLE_NETWORK_JOIN_TIMEOUT_S
    emit = status_cb or (lambda _t: None)
    with EgoLowBle() as ble:
        emit(f"连接设备 {device_address} …")
        ble.connect(device_address)
        if not ble.is_connected():
            raise BleError("设备已连接但 is_connected 返回假，配网前置条件不满足")
        emit("下发 Wi-Fi 配置…")
        ble.configure_wifi(ssid, password)
        emit("设备已接受配置，等待加入局域网并上报 IP…")

        # 配置后轮询设备 IP，直到设备加入目标局域网
        deadline = time.time() + join_timeout
        ip = ""
        elapsed = 0.0
        while time.time() < deadline:
            try:
                ip = ble.get_ip()
            except BleError:
                ip = ""
            if ip and ip not in ("0.0.0.0", "255.255.255.255"):
                emit(f"设备已联网，IP = {ip}")
                return {"ip": ip, "connected": True}
            # 前段快速轮询（设备通常 2~5 秒拿到 IP），之后退避降低 BLE 通道压力
            step = 1.0 if elapsed < 12 else poll_step
            time.sleep(step)
            elapsed += step
            emit(f"等待设备联网…已等 {int(elapsed)}/{int(join_timeout)} 秒")

        return {"ip": ip, "connected": False}


def provision(device_address: str, ssid: str, password: str,
              join_timeout: float = None, status_cb=None) -> dict:
    """一键配网：扫描到设备地址后连接、配置并等待联网。返回 {ip, connected}。

    未提供 device_address 时不会自动挑选（避免误连），由调用方先 scan_devices。
    connected=False 表示"配置已下发但超时未拿到 IP"——不是成功，调用方应提示失败。
    """
    if not ssid:
        raise BleError("Wi-Fi 名称不能为空")
    return connect_and_provision(device_address, ssid, password, join_timeout,
                                 status_cb=status_cb)
