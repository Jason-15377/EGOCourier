"""media.video 的实现。"""
import bisect
import csv
import os
import re
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from ego_checker.data.mcap import (
    _mcap_has_start_code,
    _mcap_topic_role,
    extract_foxglove_timestamp,
    extract_sei_mcap,
    parse_compressed_video,
    SEI_TOLERANCE_US,
    _IMU_TS_RE,
    extract_imu_ts,
    extract_depth_timestamps,
    ImuTimeline,
    _is_grip_topic,
    extract_mcap_sensor_timestamps,
    extract_mcap_timestamps,
    iter_mcap_messages_file_order,
)
from ego_checker.runtime.paths import application_directory


MIN_VALID_TS = 1_000_000_000_000


_FFMPEG_VFR: Optional[List[str]] = None


def ffmpeg_command() -> str:
    """优先用随包捆绑的 ffmpeg.exe, 其次 EGO Courier 识别的 ffmpeg, 最后 PATH."""
    if os.name == "nt":
        bundled = application_directory() / "ffmpeg.exe"
        if bundled.is_file():
            return str(bundled)
    try:
        from ego_relay import deps
        found = deps.ffmpeg_path()
        if found:
            return found
    except Exception:  # noqa: BLE001 - 独立运行时没有 ego_relay
        pass
    return "ffmpeg"


def _ffmpeg_process_options() -> dict:
    """启动 ffmpeg 的通用选项: 无控制台弹窗, stdin 关闭."""
    opts: dict = {"stdin": subprocess.DEVNULL}
    if os.name == "nt":
        opts["creationflags"] = subprocess.CREATE_NO_WINDOW
    return opts


def ffmpeg_available() -> bool:
    try:
        r = subprocess.run([ffmpeg_command(), "-version"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15,
                           **_ffmpeg_process_options())
        return r.returncode == 0
    except Exception:
        return False


def _mp4_boxes(stream, start: int, end: int):
    """Yield ISO BMFF box metadata without reading media payloads into memory."""
    position = start
    while position + 8 <= end:
        stream.seek(position)
        header = stream.read(8)
        if len(header) != 8:
            break
        size, kind = struct.unpack(">I4s", header)
        header_size = 8
        if size == 1:
            extended = stream.read(8)
            if len(extended) != 8:
                break
            size = struct.unpack(">Q", extended)[0]
            header_size = 16
        elif size == 0:
            size = end - position
        if size < header_size or position + size > end:
            break
        yield kind, position + header_size, position + size
        position += size


def _ffmpeg_mp4_duration(path: Path, native_error: str) -> Tuple[Optional[float], str]:
    """Probe fragmented MP4 metadata when mvhd intentionally carries duration 0."""
    try:
        result = subprocess.run(
            [ffmpeg_command(), "-hide_banner", "-i", str(path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", timeout=30,
            **_ffmpeg_process_options())
        match = re.search(
            r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr or "")
        if match:
            hours, minutes, seconds = match.groups()
            duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
            if duration > 0:
                return duration, ""
        return None, native_error + "；FFmpeg 未读取到有效视频时长"
    except Exception as exc:  # noqa: BLE001
        return None, native_error + f"；FFmpeg 时长探测失败:{exc}"


def mp4_duration_seconds(path: Path) -> Tuple[Optional[float], str]:
    """Read MP4 movie-header duration without decoding the video.

    Standard MP4/MOV files store the presentation duration in ``moov/mvhd``.
    Only box headers and the small mvhd payload are read, so multi-GB media files
    remain cheap to inspect. Fragmented files with an unset movie duration fall
    back to bundled FFmpeg metadata probing; filesystem timestamps are never used.
    """
    path = Path(path)
    try:
        with path.open("rb") as stream:
            extent = stream.seek(0, 2)
            for kind, payload_start, box_end in _mp4_boxes(stream, 0, extent):
                if kind != b"moov":
                    continue
                for child_kind, child_start, child_end in _mp4_boxes(
                        stream, payload_start, box_end):
                    if child_kind != b"mvhd":
                        continue
                    stream.seek(child_start)
                    full_header = stream.read(4)
                    if len(full_header) != 4:
                        return _ffmpeg_mp4_duration(path, "MP4 mvhd 截断")
                    version = full_header[0]
                    if version == 0:
                        if child_end - child_start < 20:
                            return _ffmpeg_mp4_duration(path, "MP4 mvhd 截断")
                        fields = stream.read(16)
                        if len(fields) != 16:
                            return _ffmpeg_mp4_duration(path, "MP4 mvhd 截断")
                        _created, _modified, timescale, duration = struct.unpack(
                            ">IIII", fields)
                        invalid_duration = 0xFFFFFFFF
                    elif version == 1:
                        if child_end - child_start < 32:
                            return _ffmpeg_mp4_duration(path, "MP4 mvhd 截断")
                        fields = stream.read(28)
                        if len(fields) != 28:
                            return _ffmpeg_mp4_duration(path, "MP4 mvhd 截断")
                        _created, _modified, timescale, duration = struct.unpack(
                            ">QQIQ", fields)
                        invalid_duration = 0xFFFFFFFFFFFFFFFF
                    else:
                        return _ffmpeg_mp4_duration(
                            path, f"不支持的 MP4 mvhd 版本:{version}")
                    if not timescale or duration in (0, invalid_duration):
                        return _ffmpeg_mp4_duration(path, "MP4 未提供有效视频时长")
                    return duration / timescale, ""
            return _ffmpeg_mp4_duration(path, "MP4 缺少 moov/mvhd 时长信息")
    except OSError as exc:
        return None, f"MP4 读取失败:{exc}"


def extract_sei_timestamps(mp4_path: Path, chunk_size: int = 16 * 1024 * 1024,
                           overlap: int = 64) -> List[int]:
    """直接扫描 mp4 文件字节中的 'timestamp_us=<数字>' 提取 SEI 时间戳 (µs).

    与 four_cam_sync_check 的 Tier2 一致, 是跨 H.264/H.265 最鲁棒的兜底方案.
    跨块字段由非数字结束符所在的新块记录一次，保留原始顺序、重复和倒退。
    忽略前缀字段、超长数字和文件尾缺少结束符的数字。
    同步自 ego_sdk/tools 提交 2cb07c9beb4ad6634d4f5c8d9295a62572da725e。
    """
    file_size = os.path.getsize(mp4_path)
    pattern = b"timestamp_us="
    timestamps: List[int] = []
    pos = 0
    carry = b""
    with open(mp4_path, "rb") as f:
        while pos < file_size:
            remaining = min(chunk_size, file_size - pos)
            if remaining <= 0:
                break
            chunk = f.read(remaining)
            if not chunk:
                break
            data = carry + chunk
            search_pos = 0
            while True:
                idx = data.find(pattern, search_pos)
                if idx == -1:
                    break
                num_start = idx + len(pattern)
                num_end = num_start
                # 多看一位，避免将超长数字的前缀误当作有效时间戳。
                while (num_end < len(data) and num_end - num_start < 19
                       and 0x30 <= data[num_end] <= 0x39):
                    num_end += 1
                previous = data[idx - 1] if idx > 0 else 0
                standalone = not (0x41 <= previous <= 0x5A or 0x61 <= previous <= 0x7A
                                  or previous == 0x5F)
                # 结束符属于新块才记录；数字恰好在块尾结束时留到下一块。
                # num_end == len(carry) 必须接收，旧重叠字段不可重复记录。
                # EOF 无结束符时不猜测数字是否完整，不按时间戳值去重。
                if (standalone and 0 < num_end - num_start <= 18
                        and len(carry) <= num_end < len(data)):
                    ts_val = int(data[num_start:num_end])
                    if MIN_VALID_TS <= ts_val < 10 ** 18:
                        timestamps.append(ts_val)
                search_pos = idx + 1
            pos += len(chunk)
            carry = data[-overlap:] if len(data) > overlap else data
    return timestamps


def save_sei_csv(sei_ts: List[int], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["frame_index", "timestamp_us"])
        for i, ts in enumerate(sei_ts):
            w.writerow([i + 1, ts])


def _role_from_name(name: str) -> Optional[str]:
    from ego_checker.data.jd import camera_file, CAMERA_ROLES
    jd_camera = camera_file(name)
    if jd_camera is not None:
        return CAMERA_ROLES[jd_camera[1]]
    n = name.lower()
    if "wristcam-l" in n or "wristcam_l" in n:
        return "wrist_left"
    if "wristcam-r" in n or "wristcam_r" in n:
        return "wrist_right"
    if "camera" not in n or "audio" in n:
        return None
    if "side" in n:
        if "left" in n and "right" not in n:
            return "side_left"
        if "right" in n and "left" not in n:
            return "side_right"
    if "camera_depth" in n:
        return "depth_rgb"
    if "camera_up" in n:
        return "up_rgb"
    if "left" in n and "right" not in n:
        return "left_rgb"
    if "right" in n and "left" not in n:
        return "right_rgb"
    if "color" in n:            # 夹爪彩色 RGB (如 /camera/color, camera_color)
        return "color_rgb"
    if "front" in n:
        return "front_rgb"
    if "back" in n or "rear" in n:
        return "back_rgb"
    if "top" in n:
        return "top_rgb"
    return None


def find_camera_mp4s(folder: Path) -> Dict[str, List[Path]]:
    """在文件夹中按角色查找相机 mp4 (支持 _partN 分片)."""
    found: Dict[str, List[Path]] = {}
    # Windows 下 glob 大小写不敏感, *.mp4 与 *.MP4 会重复命中, 用 set 去重
    seen = set()
    for f in sorted(folder.glob("*.mp4")) + sorted(folder.glob("*.MP4")):
        if f in seen:
            continue
        seen.add(f)
        role = _role_from_name(f.name)
        if role is None:
            continue
        found.setdefault(role, []).append(f)
    return found


def resolve_camera_mp4s(camera_sources: Dict[str, Path]) -> Dict[str, List[Path]]:
    """根据分析结果的 CSV 源文件精确配对同目录 MP4。

    适用单设备、多设备会话和腕带无 ``camera`` 关键字的命名。
    不递归搜索不相关 part，也不猜测多个候选。
    """
    resolved: Dict[str, List[Path]] = {}
    directory_cache: Dict[Path, Dict[str, List[Path]]] = {}
    for role, source in camera_sources.items():
        source = Path(source)
        if source.suffix.lower() == ".mp4" and source.is_file():
            resolved[role] = [source]
            continue
        if source.suffix.lower() != ".csv":
            continue
        stem = source.stem
        peers = []
        for suffix in ("_pts", "_frames"):
            if stem.lower().endswith(suffix):
                peers.extend(source.with_name(stem[:-len(suffix)] + ext)
                             for ext in (".mp4", ".MP4"))
        # Windows 大小写不敏感，同一文件会同时满足 .mp4 与 .MP4；先按平台路径语义
        # 去重，否则腕带角色无法进入后续 left_rgb 兜底，会被误报为“未找到 MP4”。
        exact_by_path = {}
        for path in peers:
            if not path.is_file():
                continue
            identity = os.path.normcase(os.path.abspath(path))
            exact_by_path.setdefault(identity, path)
        exact = list(exact_by_path.values())
        if len(exact) == 1:
            resolved[role] = exact
            continue
        base_role = role.split("__", 1)[0]
        by_role = directory_cache.setdefault(source.parent,
                                             find_camera_mp4s(source.parent))
        candidates = by_role.get(base_role, [])
        if len(candidates) == 1:
            resolved[role] = candidates
    return resolved


def run_sei_alignment(part_dir: Path, cameras_csv: Dict[str, List[int]],
                      sei_out_root: Path, log=None, *, video_by_role=None) -> Dict[str, dict]:
    """对 part 目录下的相机视频提取 SEI 并与相机 CSV 时间戳按容差对齐.

    返回 {role: {"pass", "count", "sei_count", "csv_count", "mismatch",
                 "only_sei", "only_csv", "sei_ts", "csv_saved"}}
    """
    def _log(msg):
        if log:
            log(msg)

    mp4_by_role = video_by_role if video_by_role is not None else find_camera_mp4s(part_dir)
    results: Dict[str, dict] = {}
    if not mp4_by_role:
        _log(f"  [SEI] {part_dir.name}: 未找到相机 mp4, 跳过 SEI 对齐")
        return results
    # 并行提取所有视频 SEI (I/O 密集, 线程池加速)
    all_files = [m for files in mp4_by_role.values() for m in files]
    from concurrent.futures import ThreadPoolExecutor
    n_workers = min(8, max(1, os.cpu_count() or 1))
    with ThreadPoolExecutor(max_workers=n_workers) as ex:
        sei_by_file = dict(zip(all_files, ex.map(extract_sei_timestamps, all_files)))
    for role, files in mp4_by_role.items():
        from ego_checker.data.jd import camera_file
        jd = any(camera_file(m.name) is not None for m in files)
        sei_all: List[int] = []
        for m in files:
            sei_all.extend(sei_by_file.get(m, []))
        _log(f"  [SEI] {role}: {len(sei_all)} 条 (视频 {len(files)} 个)")
        # 保存 SEI CSV (合并所有分片)
        csv_saved = ""
        if sei_all:
            out_csv = sei_out_root / f"{role}_sei.csv"
            save_sei_csv(sei_all, out_csv)
            csv_saved = str(out_csv)
            _log(f"  [SEI] {role}: 提取 {len(sei_all)} 条, 已保存 {out_csv}")
        # 与相机 CSV 完全匹配
        csv_ts = list(cameras_csv.get(role, []))
        if not sei_all:
            # 视频中未提取到任何 SEI, 无法做对齐判定 -> 视为不适用 (不拉低 part 状态)
            results[role] = {
                "pass": not jd, "count": 0, "sei_count": 0,
                "csv_count": len(csv_ts), "mismatch": -1,
                "only_sei": [], "only_csv": [], "sei_ts": [], "csv_saved": csv_saved,
            }
            _log(f"  [SEI] {role}: 未从视频提取到 SEI 时间戳 (SEI=0, CSV={len(csv_ts)}), "
                 + ("JD缺少必需SEI，FAIL" if jd else "跳过对齐判定"))
        elif csv_ts:
            from itertools import zip_longest
            # 时间差在 SEI_TOLERANCE_US 内视为一致；缺失或超容差才计为错配。
            pairs = [(left, right) for left, right in zip_longest(sei_all, csv_ts)
                     if left is None or right is None
                     or abs(left - right) > SEI_TOLERANCE_US]
            only_sei = [left for left, _ in pairs if left is not None]
            only_csv = [right for _, right in pairs if right is not None]
            mismatch = len(pairs)
            passed = not pairs
            results[role] = {
                "pass": passed, "count": len(sei_all), "sei_count": len(sei_all),
                "csv_count": len(csv_ts), "mismatch": mismatch,
                "only_sei": only_sei[:20], "only_csv": only_csv[:20],
                "sei_ts": sei_all, "csv_saved": csv_saved,
            }
            _log(f"  [SEI] {role}: SEI {len(sei_all)} vs CSV {len(csv_ts)} "
                 f"{'完全匹配 PASS' if passed else f'错配 {mismatch} 组 FAIL'}")
        else:
            results[role] = {
                "pass": False, "count": len(sei_all), "sei_count": len(sei_all),
                "csv_count": 0, "mismatch": -1,
                "only_sei": [], "only_csv": [], "sei_ts": [], "csv_saved": csv_saved,
            }
            _log(f"  [SEI] {role}: 相机 CSV 无数据, 判 FAIL")
    return results


def _ffmpeg_vfr_args() -> List[str]:
    global _FFMPEG_VFR
    if _FFMPEG_VFR is None:
        try:
            probe = subprocess.run(
                [ffmpeg_command(), "-hide_banner", "-fps_mode", "vfr", "-i", "nonexistent"],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                **_ffmpeg_process_options())
            _FFMPEG_VFR = (["-vsync", "0"]
                           if b"Unrecognized option" in probe.stderr
                           else ["-fps_mode", "vfr"])
        except Exception:
            _FFMPEG_VFR = ["-vsync", "0"]
    return _FFMPEG_VFR


def _nearest_idx(ts: List[int], t: int) -> Optional[int]:
    """在已排序时间戳列表 ts 中返回最接近 t 的下标."""
    if not ts:
        return None
    i = bisect.bisect_left(ts, t)
    best = None
    if i < len(ts):
        best = i
    if i > 0 and (best is None or abs(ts[i - 1] - t) < abs(ts[best] - t)):
        best = i - 1
    return best


def make_synced_groups(ts_by_role: Dict[str, List[int]], n_groups: int,
                       seed: Optional[int] = None) -> List[Dict[str, int]]:
    """随机生成 n_groups 组同步帧, 每组 {role: 帧号} (各目取最接近的帧).

    ts_by_role: {role: [ts,...]} 已排序时间戳. 帧号即时间戳下标 (对应视频帧号).
    返回 [{role: 帧号}, ...]; 组数可能少于 n_groups (参考相机帧数不足时).
    """
    import random
    roles = [role for role in ts_by_role if ts_by_role[role]]
    if not roles:
        return []
    rng = random.Random(seed)
    ref_role = max(roles, key=lambda r: len(ts_by_role[r]))
    ref_ts = ts_by_role[ref_role]
    groups: List[Dict[str, int]] = []
    used = set()
    trials = 0
    while len(groups) < n_groups and trials < n_groups * 5:
        trials += 1
        idx = rng.randrange(len(ref_ts))
        if idx in used:
            continue
        used.add(idx)
        ref_t = ref_ts[idx]
        g: Dict[str, int] = {ref_role: idx}
        for role in roles:
            if role == ref_role:
                continue
            gi = _nearest_idx(ts_by_role[role], ref_t)
            if gi is not None:
                g[role] = gi
        groups.append(g)
    return groups


def make_oos_groups(cam_sync: Dict[str, object],
                    ts_by_role: Dict[str, List[int]],
                    max_groups: Optional[int] = None) -> List[dict]:
    """把每个不同步基准帧扩展为多设备同时刻帧组。

    输出项格式::

        {"name": "OOS_base_left_rgb_001", "base_role": "left_rgb",
         "timestamp_us": 123, "frames": {"left_rgb": 4, "right_rgb": 5}}

    同一基准角色单独连续编号。基准帧优先使用异常记录原始下标，
    其他设备/角色按基准时间戳取最近帧。
    """
    if max_groups is not None and max_groups <= 0:
        return []
    groups: List[dict] = []
    counters: Dict[str, int] = {}
    active = {role: timestamps for role, timestamps in ts_by_role.items() if timestamps}
    for base_role, sync in cam_sync.items():
        base_ts = active.get(base_role, [])
        if not base_ts:
            continue
        for abnormal in getattr(sync, "abnormal", []):
            base_index = getattr(abnormal, "row_index", -1)
            if not 0 <= base_index < len(base_ts):
                reference = getattr(abnormal, "ts_a", None)
                base_index = _nearest_idx(base_ts, reference) if reference is not None else None
            if base_index is None or not 0 <= base_index < len(base_ts):
                continue
            reference_ts = base_ts[base_index]
            frames: Dict[str, int] = {}
            for role, timestamps in active.items():
                if role == base_role:
                    index = base_index
                elif (role == getattr(abnormal, "b_role", "")
                      and getattr(abnormal, "b_row_index", None) is not None
                      and 0 <= abnormal.b_row_index < len(timestamps)):
                    index = abnormal.b_row_index
                else:
                    index = _nearest_idx(timestamps, reference_ts)
                if index is not None:
                    frames[role] = index
            if not frames:
                continue
            counters[base_role] = counters.get(base_role, 0) + 1
            token = re.sub(r"[^A-Za-z0-9._-]+", "_", base_role).strip("_") or "camera"
            groups.append({
                "name": f"OOS_base_{token}_{counters[base_role]:03d}",
                "base_role": base_role,
                "timestamp_us": reference_ts,
                "frames": frames,
            })
            if max_groups is not None and len(groups) >= max_groups:
                return groups
    return groups


def _role_index_groups(groups: Optional[List[dict]], role: str
                       ) -> Dict[int, Optional[List[str]]]:
    """把命名帧组转换为单角色的「帧号 -> 组目录」映射。"""
    result: Dict[int, Optional[List[str]]] = {}
    for group in groups or []:
        frames = group.get("frames", {})
        index = frames.get(role) if isinstance(frames, dict) else None
        name = group.get("name")
        if not isinstance(index, int) or index < 0 or not isinstance(name, str) or not name:
            continue
        result.setdefault(index, []).append(name)
    return result


def _capture_output_dir(out_root: Path, kind: str,
                        part_name: Optional[str] = None) -> Path:
    """Return ``<root>/<kind>/<part>`` without allowing path traversal."""
    directory = Path(out_root) / kind
    if part_name is None:
        return directory
    component = Path(str(part_name).strip()).name
    component = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", component).rstrip(". ")
    return directory / (component or "part")


def mcap_capture_role_aliases(result_roles: List[str]) -> Dict[str, str]:
    """Build raw MCAP camera-role -> analyzed session-role aliases for one source file."""
    aliases: Dict[str, str] = {}
    for result_role in result_roles:
        base_role = result_role.split("__", 1)[0]
        if base_role in ("wrist_left", "wrist_right"):
            # Wrist firmware commonly publishes its only camera as /camera/left.
            # Limit the broad aliases to a single wrist camera source so a mixed
            # multi-camera MCAP cannot collapse several streams into one role.
            if len(result_roles) == 1:
                for raw_role in ("left_rgb", "right_rgb", "color_rgb", "front_rgb",
                                 "back_rgb", "top_rgb", "up_rgb"):
                    aliases[raw_role] = result_role
        else:
            aliases[base_role] = result_role
    return aliases


def _frame_select_expression(indexes) -> str:
    """构造等价的平衡加法树，避免 FFmpeg 8 的 100 层表达式深度限制。"""
    terms = [f"eq(n\\,{index})" for index in indexes]
    if not terms:
        return "0"
    while len(terms) > 1:
        terms = [f"({terms[i]}+{terms[i + 1]})" if i + 1 < len(terms) else terms[i]
                 for i in range(0, len(terms), 2)]
    return terms[0]


def _capture_role_frames(mp4_path: Path, index_groups: Dict[int, Optional[List[str]]],
                         out_dir: Path, prefix: str, log=None, timeout: int = 600) -> int:
    """对一路 mp4 截取指定帧号图片, 并按组分发到子文件夹.

    index_groups: {帧号: [组名,...]} — 每个帧号拷到 out_dir/<组名>/; 组名为 None/[] 时直接放 out_dir.
    用一次 ffmpeg select 批量解码，筛选条件通过文件传递，避免 Windows 命令行长度上限。
    返回成功帧数 (按组计).
    """
    idxs = sorted(index_groups)
    if not idxs:
        return 0
    out_dir.mkdir(parents=True, exist_ok=True)
    expr = _frame_select_expression(idxs)
    tmp_dir = Path(tempfile.mkdtemp(prefix="fsyncfrm_"))
    ok = 0
    try:
        # Windows CreateProcessW 的命令行最多 32767 个字符（包含结尾 NUL）。
        # 异常帧较多时 select 表达式会远超该上限；完整条件写入每次调用独占的
        # 临时文件，不截断帧号、不拆成多次全视频解码。关闭文件后再启动子进程，
        # 兼容 Windows 文件共享规则；文件随 tmp_dir 在所有退出路径清理。
        filter_path = tmp_dir / "select.filter"
        filter_path.write_text(f"select={expr}\n", encoding="utf-8")
        proc = subprocess.Popen(
            [ffmpeg_command(), "-y", "-nostdin", "-i", str(mp4_path),
             "-filter_script:v", str(filter_path), *_ffmpeg_vfr_args(), "-q:v", "2",
             "-start_number", "0", "-loglevel", "error",
             str(tmp_dir / "f_%05d.jpg")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **_ffmpeg_process_options())
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            if log:
                log(f"  [帧截取] 超时: {mp4_path.name}")
            return 0
        if proc.returncode != 0:
            if log:
                log(f"  [帧截取] ffmpeg 失败: {mp4_path.name}")
            return 0
        for i, fi in enumerate(idxs):
            cand = tmp_dir / f"f_{i:05d}.jpg"
            if not (cand.exists() and cand.stat().st_size > 100):
                continue
            grps = index_groups[fi]
            targets = [out_dir / g for g in grps] if grps else [out_dir]
            for d in targets:
                d.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copyfile(cand, d / f"{prefix}_f{fi:05d}.jpg")
                    ok += 1
                except OSError:
                    pass
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return ok


def capture_part_frames(part_dir: Path, out_root: Path,
                        oos_map: Dict[str, List[int]],
                        groups: Optional[List[Dict[str, int]]],
                        log=None, oos_groups: Optional[List[dict]] = None,
                        video_by_role: Optional[Dict[str, List[Path]]] = None,
                        part_name: Optional[str] = None) -> Dict[str, dict]:
    """截取 part 的不同步帧与同步帧组图片.

    oos_map: 旧接口 {role: [帧号,...]}，未提供 oos_groups 时使用。
    oos_groups: 多设备不同步帧组，输出到
                out_root/out_of_sync/<part>/OOS_base_<基准>_<序号>/。
    groups: [{role: 帧号}, ...] 同步帧组
            (输出到 out_root/synced/<part>/GroupK/)。
    part_name: Part 目录名；None 保留直接调用者原有的无 Part 中间层布局。
    返回 {kind: {role: 成功数}}
    """
    mp4_by_role = video_by_role if video_by_role is not None else find_camera_mp4s(part_dir)
    out: Dict[str, dict] = {"oos": {}, "synced": {}}
    from concurrent.futures import ThreadPoolExecutor
    nw = min(4, max(1, os.cpu_count() or 1))
    with ThreadPoolExecutor(max_workers=nw) as ex:
        # 不同步帧 (各角色 ffmpeg 独立, 并发执行)
        oos_futs = []
        for role, files in mp4_by_role.items():
            if oos_groups:
                index_groups = _role_index_groups(oos_groups, role)
                d = _capture_output_dir(out_root, "out_of_sync", part_name)
            else:
                idxs = oos_map.get(role) or []
                index_groups = {i: None for i in idxs}
                d = _capture_output_dir(out_root, "out_of_sync", part_name) / role
            if not index_groups or not files:
                continue
            oos_futs.append((role, ex.submit(_capture_role_frames, files[0],
                                             index_groups, d,
                                             prefix=role, log=log)))
        for role, fu in oos_futs:
            try:
                out["oos"][role] = fu.result()
            except Exception as e:  # noqa: BLE001
                if log:
                    log(f"  [帧截取] {role} 不同步帧截取失败: {e}")
        # 同步帧组
        if groups:
            syn_futs = []
            for role, files in mp4_by_role.items():
                index_groups: Dict[int, Optional[List[str]]] = {}
                for gi, g in enumerate(groups, 1):
                    idx = g.get(role)
                    if idx is None:
                        continue
                    index_groups.setdefault(idx, []).append(f"Group{gi}")
                if not index_groups or not files:
                    continue
                d = _capture_output_dir(out_root, "synced", part_name)
                syn_futs.append((role, ex.submit(_capture_role_frames, files[0],
                                                 index_groups, d, prefix=role,
                                                 log=log)))
            for role, fu in syn_futs:
                try:
                    out["synced"][role] = fu.result()
                except Exception as e:  # noqa: BLE001
                    if log:
                        log(f"  [帧截取] {role} 同步帧截取失败: {e}")
    return out


def capture_mcap_frames(mcap_path: Path, ts_by_role: Dict[str, List[int]],
                        oos_map: Dict[str, List[int]],
                        groups: Optional[List[Dict[str, int]]],
                        out_root: Path, log=None,
                        oos_groups: Optional[List[dict]] = None,
                        role_aliases: Optional[Dict[str, str]] = None,
                        part_name: Optional[str] = None) -> Dict[str, dict]:
    """从 .mcap 截取指定帧号图片, 输出结构与 mp4 版 (capture_part_frames) 一致.

    ts_by_role: {role: [ts,...]} 分析用原始顺序时间戳列表，保留重复，帧号即其下标。
    oos_map: 旧接口 {role: [帧号,...]}。
    oos_groups: 多设备不同步帧组，输出到
                out_root/out_of_sync/<part>/OOS_base_<基准>_<序号>/。
    groups: [{role: 帧号},...] (输出到 out_root/synced/<part>/GroupK/)。
    part_name: Part 目录名；None 保留直接调用者原有的无 Part 中间层布局。
    """
    out: Dict[str, dict] = {"oos": {}, "synced": {}}
    tmp = Path(tempfile.mkdtemp(prefix="fsyncmcap_"))
    try:
        streams: Dict[str, dict] = {}
        raw_image_notices = set()
        # 打开/读取 mcap 失败 (路径失效/被占用/损坏) 不应中断整体分析
        if not Path(mcap_path).is_file():
            if log:
                log(f"  [帧截取] 跳过: mcap 不存在或无法访问 {mcap_path}")
            return out
        try:
            with open(mcap_path, "rb") as f:
                # 合法 MCAP 可以没有 summary；在数据流中直接识别通道，
                # 与主分析路径保持一致。
                for _schema, channel, msg in iter_mcap_messages_file_order(f):
                    raw_role = _mcap_topic_role(channel.topic)
                    if raw_role is None:
                        continue
                    role = (role_aliases or {}).get(raw_role, raw_role)
                    parsed = parse_compressed_video(msg.data, getattr(_schema, "name", ""), channel.topic)
                    if getattr(_schema, "name", "") == "foxglove.RawImage":
                        if role not in raw_image_notices:
                            raw_image_notices.add(role)
                            if log:
                                log(f"  [帧截取] {role}: RawImage {parsed['format']}为原始像素，暂不支持帧截图")
                        continue
                    h264 = parsed.get("h264_data", b"")
                    if not h264:
                        continue
                    st = streams.get(role)
                    if st is None:
                        p = tmp / f"{role}.h264"
                        st = {"fh": open(p, "wb"), "timestamps": [], "positions": [],
                              "path": p, "n": 0}
                        streams[role] = st
                    pos = st["n"]
                    st["n"] += 1
                    if not _mcap_has_start_code(h264):
                        st["fh"].write(b"\x00\x00\x00\x01")
                    st["fh"].write(h264)
                    schema_name = getattr(_schema, "name", "")
                    if schema_name == "jd.CompressedVideo":
                        ts = parsed["timestamp_us"]
                        if extract_sei_mcap(h264, parsed.get("format", "")) != ts:
                            raise ValueError("JD视频SEI与消息时间不一致或SEI缺失")
                    else:
                        # 严格解析器已校验字段1；H.26x SEI 时间差仅作审计，
                        # 截帧始终使用字段1时间戳。
                        ts = parsed["timestamp_us"]
                    if ts is not None:
                        st["timestamps"].append(ts)
                        st["positions"].append(pos)
                for st in streams.values():
                    st["fh"].close()
                # 时间戳已由严格字段、身份和SEI契约验证。只在帧数
                # 完全相等时做顺序映射，避免猜测错帧。
                for role, st in streams.items():
                    timeline = ts_by_role.get(role, [])
                    if st["timestamps"] == timeline:
                        st["pos_by_row"] = dict(enumerate(st["positions"]))
                    elif not st["timestamps"] and len(timeline) == st["n"]:
                        st["pos_by_row"] = {index: index for index in range(st["n"])}
                    else:
                        st["pos_by_row"] = {}
                        if log:
                            log(f"  [帧截取] {role}: 源视频时间序列与分析记录不一致，跳过截取")
        except OSError as e:
            for st in streams.values():
                if not st["fh"].closed:
                    st["fh"].close()
            if log:
                log(f"  [帧截取] 跳过 (无法打开/读取 mcap): {e}")
            return out
        except Exception as e:  # noqa: BLE001
            for st in streams.values():
                if not st["fh"].closed:
                    st["fh"].close()
            if log:
                log(f"  [帧截取] 跳过 (mcap 解析失败): {e}")
            return out

        # 不同步帧 (各角色 ffmpeg 独立, 并发执行)
        from concurrent.futures import ThreadPoolExecutor
        nw = min(4, max(1, os.cpu_count() or 1))
        with ThreadPoolExecutor(max_workers=nw) as ex:
            oos_futs = []
            roles = streams if oos_groups else oos_map
            for role in roles:
                st = streams.get(role)
                ts_list = ts_by_role.get(role)
                if oos_groups:
                    frame_groups = _role_index_groups(oos_groups, role)
                else:
                    frame_groups = {index: None for index in (oos_map.get(role) or [])}
                if not st or not ts_list or not frame_groups:
                    continue
                position_groups: Dict[int, Optional[List[str]]] = {}
                for idx, names in frame_groups.items():
                    if 0 <= idx < len(ts_list):
                        p = st["pos_by_row"].get(idx)
                        if p is not None:
                            if names:
                                position_groups.setdefault(p, []).extend(names)
                            else:
                                position_groups.setdefault(p, None)
                if not position_groups:
                    continue
                d = _capture_output_dir(out_root, "out_of_sync", part_name)
                if not oos_groups:
                    d /= role
                oos_futs.append((role, ex.submit(_decode_mcap_positions,
                                                 st["path"], position_groups,
                                                 d, role, log)))
            for role, fu in oos_futs:
                try:
                    out["oos"][role] = fu.result()
                except Exception as e:  # noqa: BLE001
                    if log:
                        log(f"  [帧截取] {role} 不同步帧截取失败: {e}")
            # 同步帧组
            if groups:
                syn_futs = []
                for role, st in streams.items():
                    ts_list = ts_by_role.get(role)
                    if not ts_list:
                        continue
                    pos_groups: Dict[int, Optional[List[str]]] = {}
                    for gi, g in enumerate(groups, 1):
                        idx = g.get(role)
                        if idx is None or not (0 <= idx < len(ts_list)):
                            continue
                        p = st["pos_by_row"].get(idx)
                        if p is not None:
                            pos_groups.setdefault(p, []).append(f"Group{gi}")
                    if not pos_groups:
                        continue
                    d = _capture_output_dir(out_root, "synced", part_name)
                    syn_futs.append((role, ex.submit(_decode_mcap_positions,
                                                     st["path"], pos_groups, d, role, log)))
                for role, fu in syn_futs:
                    try:
                        out["synced"][role] = fu.result()
                    except Exception as e:  # noqa: BLE001
                        if log:
                            log(f"  [帧截取] {role} 同步帧截取失败: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def _decode_mcap_positions(stream_path: Path,
                           pos_groups: Dict[int, Optional[List[str]]],
                           out_dir: Path, prefix: str, log=None,
                           timeout: int = 600) -> int:
    """对重建的 H.264 字节流做一次 ffmpeg 解码, 按位置把帧分发到组文件夹."""
    positions = sorted(pos_groups)
    if not positions:
        return 0
    img_tmp = Path(tempfile.mkdtemp(prefix="fsyncmcapimg_"))
    ok = 0
    try:
        proc = subprocess.Popen(
            [ffmpeg_command(), "-y", "-nostdin", "-i", str(stream_path),
             "-q:v", "2", "-start_number", "0", "-loglevel", "error",
             str(img_tmp / "f_%05d.jpg")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **_ffmpeg_process_options())
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            if log:
                log(f"  [帧截取] mcap 解码超时: {stream_path.name}")
            return 0
        if proc.returncode != 0:
            if log:
                log(f"  [帧截取] mcap ffmpeg 解码失败: {stream_path.name}")
            return 0
        for p in positions:
            src = img_tmp / f"f_{p:05d}.jpg"
            if not (src.exists() and src.stat().st_size > 100):
                continue
            grps = pos_groups[p]
            targets = [out_dir / g for g in grps] if grps else [out_dir]
            for d in targets:
                d.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copyfile(src, d / f"{prefix}_f{p:05d}.jpg")
                    ok += 1
                except OSError:
                    pass
    finally:
        shutil.rmtree(img_tmp, ignore_errors=True)
    return ok
