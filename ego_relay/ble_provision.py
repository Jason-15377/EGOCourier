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
#
# 前缀按「连字符 / 下划线都算分隔、后缀随意」匹配（见 _matches_ego_name）：
#   EGO_AK8M…          （下划线）
#   EGO-4B_AZL9861004V （连字符 + 型号，实际产品就是这样广播的）
#   UMI_Finger-R_…     （带 -R/-L 方位后缀，早先写死的 UMIFinger_ 永远匹配不到）
#   WristCam-R-R_…     （同上，左右叠戴）
# 早先只比对裸前缀，导致 EGO-4B_* / UMI_Finger-R_* 这类真实设备扫不到，
# 现场表现为「设备在，但列表里没名字，重启设备才出现」。
EGO_BLE_NAME_PREFIXES = ("EGO", "UMIFinger", "UMI_Finger", "WristCam", "EGOPro",
                         "UMIGripper", "EGODepth")
EGO_BLE_OUI = ("54:14:FD",)


def _matches_ego_name(name: str) -> bool:
    """名称是否像 EGO 设备：前缀命中，且其后紧跟分隔符或名称就此结束。

    只看前缀会误收 EGOXXX 之类无关广播；要求「前面一致 + 后面是 _ - 或结束」，
    既能认 EGO-4B_AZL…、UMI_Finger-R_BF8…，又不会把 EGOBike 收进来。
    """
    up = name.upper()
    for prefix in EGO_BLE_NAME_PREFIXES:
        p = prefix.upper()
        if not up.startswith(p):
            continue
        rest = up[len(p):]
        if rest == "" or rest[0] in "-_ ":
            return True
    return False


def dll_available() -> bool:
    """EgoLowBle.dll 是否可定位（配网功能是否可用）。"""
    return bool(find_dll())


def is_ego_ble_device(dev: dict) -> bool:
    """是否是 EGO 设备的 BLE 广播（按名称前缀或 OUI 判断）。

    **只排地址为空的条目**：地址是连接的唯一凭据，没有地址连不上。
    名称允许为空——BLE 广播包里 GAP 名称是可选的，很多 EGO 设备刚上电、
    或上一次连接断开后，头几个广播包只带 MAC 不带名称。早先这里把无名称
    的条目也丢掉，于是「设备在线但扫不到名字，重启设备才出现」：重启后设备
    重新广播完整包，名字才被填上。现在无名称的条目保留，交给上层显示为
    「(未命名)」，地址仍然可连接、可后续按名称刷新。
    """
    addr = (dev.get("address") or "").strip().upper()
    if not addr:
        return False
    name = (dev.get("name") or "").strip()
    if not name:
        # 无名称：只能靠 OUI 判定，属于 EGO 器件的保留下来
        return addr.startswith(EGO_BLE_OUI)
    if _matches_ego_name(name):
        return True
    return addr.startswith(EGO_BLE_OUI)


def scan_devices(only_ego: bool = True, rssi_floor: int | None = None) -> list:
    """扫描附近 BLE 设备，返回 [{name,address,rssi,connectable}]。

    过滤链：EGO 设备白名单 → 信号强度下限（距离过远）→ 按地址去重 → 按信号降序。
    only_ego=False 时不做白名单（排查"设备扫不到"时用得上）。
    rssi_floor：覆盖默认下限，供「扫不到设备」时放宽再试（见 scan_devices_robust）。
    """
    with EgoLowBle() as ble:
        devices = ble.scan_devices()
    if only_ego:
        devices = [d for d in devices if is_ego_ble_device(d)]
    rssi_min = getattr(config, "BLE_SCAN_RSSI_MIN", -70) if rssi_floor is None else rssi_floor
    devices = [d for d in devices if d.get("rssi", -100) >= rssi_min]
    # 同一台设备可能被扫到多次：按地址去重，保留信号最强的那条
    best = {}
    for d in devices:
        key = (d.get("address") or "").upper()
        if not key:
            continue
        cur = best.get(key)
        # 同地址的后续广播可能带上了名称（首包无名称）：合并时优先保留非空名称
        if cur is None:
            best[key] = d
            continue
        merged = dict(cur)
        if not (merged.get("name") or "").strip() and (d.get("name") or "").strip():
            merged["name"] = d["name"]
        if d.get("rssi", -100) > cur.get("rssi", -100):
            merged["rssi"] = d["rssi"]
            merged["connectable"] = d.get("connectable", merged.get("connectable"))
        best[key] = merged
    devices = list(best.values())
    devices.sort(key=lambda d: d.get("rssi", -100), reverse=True)
    return devices


def scan_devices_robust(attempts: int = 2, status_cb=None) -> list:
    """扫描设备；**快出结果**，只有一台都没有时才补扫 / 放宽。

    速度是第一诉求：EgoLowBle 的扫描会阻塞到超时才返回，所以
      · 默认只扫 1 轮、每轮 2 秒（config.BLE_SCAN_TIMEOUT_S），实测已能覆盖附近全部设备；
      · 有结果就立刻返回，不再为了「补名称」多扫——名称缺失的设备也会列出来，
        用户点选时按地址连接，不受影响；
      · 真的一台都没扫到时，才补扫一轮（蓝牙刚开 / 设备刚广播的瞬态）。
    兜底顺序（仅在一片空白时升级）：
      1. 再扫 1 轮（默认档）；
      2. 放宽信号下限到 -100（设备抖动到下限以下）；
      3. 关闭名称白名单、只按 OUI 认（名称还没解析出来的 EGO 广播）。
    status_cb: 可选，用于 GUI 追加日志（说明当前在做哪一步）。
    """
    emit = status_cb or (lambda _t: None)
    merged: dict = {}

    def _collect(devs):
        for d in devs:
            key = (d.get("address") or "").upper()
            if not key:
                continue
            cur = merged.get(key)
            if cur is None:
                merged[key] = d
                continue
            if not (cur.get("name") or "").strip() and (d.get("name") or "").strip():
                cur["name"] = d["name"]
            if d.get("rssi", -100) > cur.get("rssi", -100):
                cur["rssi"] = d["rssi"]
                cur["connectable"] = d.get("connectable", cur.get("connectable"))

    # 1) 默认档：先扫一轮，有结果立即返回（快）。
    _collect(scan_devices(only_ego=True))
    if merged:
        return _sorted(merged)

    # 2) 一片空白：补扫一轮（蓝牙刚开 / 设备刚上电广播的瞬态）
    emit("未发现设备，再次扫描…")
    _collect(scan_devices(only_ego=True))
    if merged:
        return _sorted(merged)

    # 3) 放宽信号下限（设备在附近但 RSSI 抖动到下限以下）
    emit("仍未发现，放宽信号强度下限重试…")
    _collect(scan_devices(only_ego=True, rssi_floor=-100))
    if merged:
        return _sorted(merged)

    # 4) 关闭名称白名单（只靠 OUI 认设备），兜住名称未解析的 EGO 广播
    emit("仍未发现，关闭名称过滤、只按厂商识别重试…")
    _collect(scan_devices(only_ego=False, rssi_floor=-100))
    return _sorted(merged)


def _sorted(best: dict) -> list:
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
        # 先做一次短扫描「预热」再连接：Windows 的 BLE 栈要靠扫描把设备放进缓存，
        # 直接按地址 connect 会走较慢的服务发现路径。本机实测同一台设备（EGO-4B）
        #   直接连接：17.4 秒
        #   先扫 2 秒再连接：2.2 秒
        # 差 8 倍——这就是「配置 Wi-Fi 并添加设备巨慢」的主因（不是等 IP 慢，
        # get_ip 实测只要 0.1~0.2 秒）。扫描失败不阻断流程，连接本身仍会尝试。
        emit("预热蓝牙扫描…")
        try:
            ble.scan_devices(timeout_ms=int(config.BLE_SCAN_TIMEOUT_S * 1000))
        except BleError:
            pass
        emit(f"连接设备 {device_address} …")
        ble.connect(device_address)
        if not ble.is_connected():
            raise BleError("设备已连接但 is_connected 返回假，配网前置条件不满足")
        emit("下发 Wi-Fi 配置…")
        ble.configure_wifi(ssid, password)
        emit("设备已接受配置，等待加入局域网并上报 IP…")

        # 配置后轮询设备 IP，直到设备加入目标局域网。
        # 速度要点：get_ip 默认超时 10 秒，若直接用它轮询，即使设备 3 秒就联网，
        # 也要等满一次 10 秒的阻塞才返回——这就是「配置 Wi-Fi 并添加设备巨慢」。
        # 改为短超时（1.2 秒）+ 短间隔轮询：设备一上报 IP 就立刻拿到，
        # 典型场景从「十几秒」降到「几秒」。
        deadline = time.time() + join_timeout
        ip = ""
        started = time.time()
        poll_ms = 1200
        while time.time() < deadline:
            try:
                ip = ble.get_ip(timeout_ms=poll_ms)
            except BleError:
                ip = ""
            if ip and ip not in ("0.0.0.0", "255.255.255.255"):
                emit(f"设备已联网，IP = {ip}")
                return {"ip": ip, "connected": True}
            elapsed = time.time() - started
            # 前 15 秒每 0.35 秒问一次（设备通常 3~6 秒拿到 IP）；之后退避
            time.sleep(0.35 if elapsed < 15 else poll_step)
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
