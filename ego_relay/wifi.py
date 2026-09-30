"""WiFi 扫描与连接状态（Windows netsh，兼容中英文系统输出）。

- scan():  扫描可见 WiFi，返回 [{ssid, signal, security, connected}]
- status():当前网卡连接状态 {connected, ssid, signal}
作为「设备 WiFi 导出」通道的基础信息层。
"""

from __future__ import annotations

import locale
import re
import socket
import subprocess
from typing import Dict, List, Optional


def _run(*args) -> str:
    try:
        r = subprocess.run(["netsh", "wlan", *args], capture_output=True, timeout=30)
        # netsh 输出编码与 locale 不一定一致（中文 Windows 常为 GBK/cp936），
        # 按 gbk -> utf-8 顺序尝试解码。
        for enc in ("gbk", "cp936", "utf-8"):
            try:
                return r.stdout.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return r.stdout.decode("utf-8", errors="replace")
    except Exception:
        return ""


# SSID 起始行（含中文全角冒号）
_SSID_RE = re.compile(r"SSID\s*\d*\s*[:：]\s*(.+)")
_AUTH_RE = re.compile(r"(?:身份验证|Authentication)\s*[:：]\s*(.+)")
_ENC_RE = re.compile(r"(?:加密|Encryption)\s*[:：]\s*(.+)")
_SIG_RE = re.compile(r"(?:信号|Signal)\s*[:：]\s*(\d{1,3})%")
# 接口状态（行首锚定，避免误匹配「承载网络状态」等含“状态”的其他行）
_STATE_RE = re.compile(r"^(?:状态|State)\s*[:：]\s*(.+)")
_IF_SSID_RE = re.compile(r"^\s*SSID\s*[:：]\s*(.+)")
_IF_SIG_RE = re.compile(r"(?:信号|Signal)\s*[:：]\s*(\d{1,3})%")


def scan() -> Dict[str, object]:
    """扫描并解析可见 WiFi 列表。"""
    out = _run("show", "networks", "mode=bssid")
    networks: List[dict] = []
    cur: Optional[dict] = None
    for raw in out.splitlines():
        line = raw.strip()
        m = _SSID_RE.match(line)
        if m:
            cur = {"ssid": m.group(1).strip(), "signal": 0, "security": "",
                   "auth": "", "enc": "", "connected": False}
            networks.append(cur)
            continue
        if cur is None:
            continue
        a = _AUTH_RE.search(line)
        if a:
            cur["auth"] = a.group(1).strip()
        e = _ENC_RE.search(line)
        if e:
            cur["enc"] = e.group(1).strip()
        s = _SIG_RE.search(line)
        if s:
            sig = int(s.group(1))
            if sig > cur["signal"]:
                cur["signal"] = sig
    for n in networks:
        parts = [p for p in (n["auth"], n["enc"]) if p]
        n["security"] = " + ".join(parts)
        n.pop("auth", None)
        n.pop("enc", None)
    return {"networks": networks, "raw_len": len(out)}


def status() -> Dict[str, object]:
    """当前 WiFi 连接状态。"""
    out = _run("show", "interfaces")
    connected = False
    ssid = ""
    signal = 0
    for raw in out.splitlines():
        line = raw.strip()
        st = _STATE_RE.search(line)
        if st:
            v = st.group(1).strip().lower()
            connected = ("connected" in v or "已连接" in v or "已連線" in v)
        s = _IF_SSID_RE.match(line)
        if s:
            ssid = s.group(1).strip()
        sig = _IF_SIG_RE.search(line)
        if sig:
            signal = int(sig.group(1))
    return {"connected": connected, "ssid": ssid, "signal": signal}


def scan_with_status() -> Dict[str, object]:
    st = status()
    data = scan()
    cur_ssid = st.get("ssid", "")
    for n in data.get("networks", []):
        n["connected"] = bool(cur_ssid) and n["ssid"].lower() == cur_ssid.lower()
    data["interface"] = st
    return data


def local_ip() -> str:
    """电脑当前出口 IP（本机在局域网内的地址）；失败返回空串。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # 不需要真正发包，仅用于让系统选路出本机 IP
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


def same_subnet(ip_a: str, ip_b: str, prefix: int = 24) -> bool:
    """两个 IPv4 是否在同一网段（默认 /24）。非法地址返回 False。"""
    def _num(s):
        try:
            parts = [int(x) for x in s.split(".")]
            if len(parts) != 4 or any(not 0 <= p <= 255 for p in parts):
                return None
            return sum(p << (8 * (3 - i)) for i, p in enumerate(parts))
        except ValueError:
            return None
    na, nb = _num(ip_a), _num(ip_b)
    if na is None or nb is None:
        return False
    mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
    return (na & mask) == (nb & mask)


def ping(host: str, timeout_ms: int = 1000) -> bool:
    """对 host 发一个 ICMP ping，返回是否可达（Windows ping）。"""
    try:
        r = subprocess.run(
            ["ping", "-n", "1", "-w", str(timeout_ms), host],
            capture_output=True, timeout=timeout_ms + 2000)
        return r.returncode == 0
    except Exception:
        return False


def probe_device(host: str, timeout_ms: int = 1000) -> Dict[str, object]:
    """导出前连通性预检：返回 {reachable, same_net, local_ip}。"""
    return {
        "reachable": ping(host, timeout_ms),
        "same_net": same_subnet(host, local_ip()),
        "local_ip": local_ip(),
    }
