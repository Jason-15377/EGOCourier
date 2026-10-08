"""BLE 配网编排：扫描 → 连接 → 配置 Wi-Fi → 等待联网并拿 IP。

供 GUI（export_tab）、Web API（api.py）、CLI 共用。底层用 EgoLowBle.dll。
"""

from __future__ import annotations

import time

from . import config
from .egolowble import EgoLowBle, BleError, find_dll


def dll_available() -> bool:
    """EgoLowBle.dll 是否可定位（配网功能是否可用）。"""
    return bool(find_dll())


def scan_devices() -> list:
    """扫描附近 BLE 设备，返回 [{name,address}]。

    按信号强度过滤：丢弃信号过弱（距离过远）的设备，并按信号从强到弱排序，
    只保留附近、易连接的设备，缩短选择列表、加快体验。
    """
    with EgoLowBle() as ble:
        devices = ble.scan_devices()
    rssi_min = getattr(config, "BLE_SCAN_RSSI_MIN", -70)
    devices = [d for d in devices if d.get("rssi", -100) >= rssi_min]
    devices.sort(key=lambda d: d.get("rssi", -100), reverse=True)
    return devices


def connect_and_provision(device_address: str, ssid: str, password: str,
                          join_timeout: float = None,
                          poll_step: float = 2.0) -> dict:
    """连接设备并下发 Wi-Fi 配置，等待设备联网，返回 {ip, connected}。

    device_address: 蓝牙 MAC 地址（来自 scan_devices）。
    ssid/password: 目标 Wi-Fi（password 开放网络可传空串）。
    join_timeout: 等待设备加入局域网并拿到 IP 的超时秒数。
    """
    join_timeout = join_timeout or config.BLE_NETWORK_JOIN_TIMEOUT_S
    with EgoLowBle() as ble:
        ble.connect(device_address)
        if not ble.is_connected():
            raise BleError("设备已连接但 is_connected 返回假，配网前置条件不满足")
        ble.configure_wifi(ssid, password)

        # 配置后轮询设备 IP，直到设备加入目标局域网
        deadline = time.time() + join_timeout
        ip = ""
        while time.time() < deadline:
            try:
                ip = ble.get_ip()
            except BleError:
                ip = ""
            if ip and ip not in ("0.0.0.0", "255.255.255.255"):
                return {"ip": ip, "connected": True}
            time.sleep(poll_step)

        return {"ip": ip, "connected": False}


def provision(device_address: str, ssid: str, password: str,
              join_timeout: float = None) -> dict:
    """一键配网：扫描到设备地址后连接、配置并等待联网。返回 {ip, connected}。

    未提供 device_address 时不会自动挑选（避免误连），由调用方先 scan_devices。
    """
    if not ssid:
        raise BleError("Wi-Fi 名称不能为空")
    return connect_and_provision(device_address, ssid, password, join_timeout)
