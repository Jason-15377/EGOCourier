"""录制文件名、分段和设备身份的纯转换。"""
import collections
import re
from pathlib import Path
from typing import Dict, List

def _imu_device_name(path: Path) -> str:
    """从 IMU 文件名推导设备名 (如 EGO / WristCam-L / WristCam-R)."""
    stem = Path(path).stem
    stem = re.sub(r'_part\d+$', '', stem)
    stem = stem.replace('_imu', '')
    return stem.split('_')[0]


def _device_serial_from_path(path: Path) -> str:
    """从录制文件名前缀提取序列号，用于区分同型号多设备。"""
    stem = re.sub(r'_part\d+$', '', Path(path).stem, flags=re.IGNORECASE)
    tokens = stem.split('_')
    return tokens[1] if len(tokens) > 1 else ""


def _session_device_keys(part: Dict) -> Dict[Path, str]:
    """为会话内的数据源生成稳定设备键；同名设备自动附加序列号。"""
    sources: List[Path] = [Path(p) for p in part.get("mcaps", [])]
    sources.extend(Path(p) for p in part.get("imu", []))
    for files in (part.get("extra") or {}).values():
        sources.extend(Path(p) for p in files)
    for _role, files in part.get("camera_entries", []):
        sources.extend(Path(p) for p in files)
    explicit = {
        Path(path): str(device)
        for path, device in part.get("device_name_by_source", {}).items()
    }
    identities = {
        path: ((explicit[path], "") if path in explicit
               else (_imu_device_name(path), _device_serial_from_path(path)))
        for path in sources
    }
    serials: Dict[str, set] = collections.defaultdict(set)
    for name, serial in identities.values():
        if serial:
            serials[name].add(serial)
    return {
        path: (f"{name}[{serial}]" if serial and len(serials[name]) > 1 else name)
        for path, (name, serial) in identities.items()
    }


def _session_role_key(role: str, device: str) -> str:
    suffix = re.sub(r"[^A-Za-z0-9]+", "_", device).strip("_") or "device"
    return f"{role}__{suffix}"


def _part_token(name: str) -> str:
    """从文件名去掉 part 编号 token, 用于同 part 内识别相机/IMU 文件.

    支持 camera_left_part0000.csv (尾部 _partN) 与 part0000_camera_left.csv (头部 partN_).
    """
    stem = Path(name).stem
    stem = re.sub(r'_part\d+', '', stem, flags=re.IGNORECASE)
    stem = re.sub(r'^(part|rec|clip)\d+_', '', stem, flags=re.IGNORECASE)
    stem = re.sub(r'^\d{3,}_', '', stem)
    return stem


def _group_token(name: str) -> str:
    """返回该文件所属 part 的组标识 (同 part 同目录文件此值相同)."""
    stem = Path(name).stem
    m = re.search(r'_part(\d+)', stem, re.IGNORECASE)
    if m:
        return "part" + m.group(1)
    m = re.match(r'^(part|rec|clip)(\d+)', stem, re.IGNORECASE)
    if m:
        return m.group(1).lower() + m.group(2)
    m = re.match(r'^(\d{3,})_', stem)
    if m:
        return m.group(1)
    return ""


def _part_token_of(p: Dict) -> str:
    """优先使用文件分段编号；iOS 无文件编号时回溯 part_001 等目录。"""
    sources = ([Path(p["mcap"])] if p.get("mcap") else [])
    sources += [Path(f) for files in p.get("cameras", {}).values() for f in files]
    sources += [Path(f) for f in p.get("imu", [])]
    for source in sources:
        token = _group_token(source.name)
        if token:
            return token
    for name in [Path(p["path"]).name] + [source.parent.name for source in sources]:
        match = re.fullmatch(r"(part|rec|clip)_?(\d+)", name, re.IGNORECASE)
        if match:
            return match[1].lower() + match[2]
    return ""
