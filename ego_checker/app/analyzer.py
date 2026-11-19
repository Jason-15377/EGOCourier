"""app.analyzer 的实现。"""
import collections
import ego_checker.app.audio as audio_validation
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from ego_checker.analysis.context import apply_cam_segment_context, apply_imu_segment_context
from ego_checker.analysis.timing import _adaptive_cam_sync_ms_single, _adaptive_imu_sync_ms, _adaptive_rate_threshold_ms, check_cam_sync, check_frame_count, check_grip_sync, check_imu_drop_sources, check_imu_groups_diff, check_imu_sync, check_interval, check_rgb_drop, check_row_align, expected_interval_us
from ego_checker.data.csv_readers import _read_frames_csv, normalize_ts, read_csv_timestamps, read_imu_timestamps, imu_source_sequences, SourceRowIndex
from ego_checker.data.discovery import ROLE_LABELS, _firmware_from_meta, _imu_device_name, _session_device_keys, _session_firmware_summary, _session_role_key, discover_parts
from ego_checker.data.camera_frequency import resolve_camera_frequency
from ego_checker.data.imu_frequency import resolve_imu_frequency
from ego_checker.analysis.imu_frequency import audit_magnetic_frequency
from ego_checker.data.grip_frequency import resolve_grip_frequency
from ego_checker.data.meta_integrity import check_device_metas
from ego_checker.domain.constants import CAM_SYNC_THRESHOLD_US, FRAMES_SUFFIX, IMU_DROP_MULTIPLIER, IMU_SYNC_THRESHOLD_US, INTERVAL_WAVE_MS, PRESENTATION_COL, RGB_DROP_MULTIPLIER, ROW_ALIGN_THRESHOLD_US, US_PER_MS
from ego_checker.domain.models import AbnormalFrame, CameraData, DropResult, PartResult
from ego_checker.data.mcap import save_mcap_stream_timestamp_csv
from ego_checker.analysis.timing import _cam_sync_pair_groups, _cam_sync_by_pairs, _depth_sync_threshold_us
from ego_checker.runtime.control import AnalysisStopped
from ego_checker.runtime.errors import describe_exception
from ego_checker.app.analysis_log import event, input_plan, part_progress, stage
from ego_checker.app.parallel import AnalysisWorkerError, PartAnalyzer


def _source_row_map(timestamps, rows) -> SourceRowIndex:
    return SourceRowIndex(timestamps, rows)


_META_INTEGRITY_CACHE: Dict[str, List] = {}


def _attach_meta_integrity(res: PartResult, part: Dict, opt: Dict) -> None:
    """meta 完整性/准确性检测（独立检查，默认开启）；按设备目录缓存，异常不中断分析。"""
    if not opt.get("meta_check", True):
        return
    dirs = list(part.get("device_dirs") or []) or [part["path"], Path(part["path"]).parent]
    seen = set()
    try:
        for d in dirs:
            key = str(Path(d))
            if key in seen:
                continue
            seen.add(key)
            if key not in _META_INTEGRITY_CACHE:
                _META_INTEGRITY_CACHE[key] = check_device_metas(Path(d))
            res.meta_integrity.extend(_META_INTEGRITY_CACHE[key])
    except (AnalysisStopped, MemoryError):
        raise
    except Exception:  # noqa: BLE001 - 检测自身异常降级，不中断分析
        pass


def _record_imu_frequency(res: PartResult, source: Path, timestamps,
                          device: str = "") -> None:
    resolved = resolve_imu_frequency(source, timestamps)
    if resolved.magnetic_reference:
        profile = dict(device=device, reference_hz=resolved.rate_hz,
                       sensor_rates_hz=resolved.sensor_rates_hz,
                       configuration_source=resolved.source or str(source),
                       configuration_errors=resolved.configuration_errors)
        # Metadata identities may repeat across files; source records are never deduplicated.
        if profile not in res.imu_frequency_profiles:
            res.imu_frequency_profiles.append(profile)
    if device:
        if resolved.error:
            res.imu_frequency_error_by_device[device] = resolved.error
            return
        previous = res.imu_frequency_hz_by_device.get(device)
        if previous is not None and previous != resolved.rate_hz:
            res.imu_frequency_error_by_device[device] = (
                f"同设备IMU频率来源冲突: {previous:g}Hz 与 {resolved.rate_hz:g}Hz")
            return
        res.imu_frequency_hz_by_device[device] = resolved.rate_hz
        res.imu_frequency_source_by_device[device] = resolved.source
        return
    if resolved.error:
        res.imu_frequency_error = resolved.error
        return
    if res.imu_frequency_hz is not None and res.imu_frequency_hz != resolved.rate_hz:
        res.imu_frequency_error = (
            f"IMU频率来源冲突: {res.imu_frequency_hz:g}Hz 与 {resolved.rate_hz:g}Hz")
        return
    res.imu_frequency_hz = resolved.rate_hz
    res.imu_frequency_source = resolved.source


def _record_grip_frequency(res: PartResult, source: Path, device: str = '',
                           timestamps=None) -> None:
    resolved = resolve_grip_frequency(source, timestamps)
    if device:
        if resolved.error:
            res.grip_frequency_error_by_device[device] = resolved.error
            return
        previous = res.grip_frequency_hz_by_device.get(device)
        if previous is not None and previous != resolved.rate_hz:
            res.grip_frequency_error_by_device[device] = (
                f'同设备夹爪JSON频率冲突：{res.grip_frequency_source_by_device[device]}={previous:g}Hz；'
                f'{resolved.source}={resolved.rate_hz:g}Hz')
            return
        res.grip_frequency_hz_by_device[device] = resolved.rate_hz
        res.grip_frequency_source_by_device[device] = resolved.source
    else:
        if resolved.error:
            res.grip_frequency_error = resolved.error
            return
        if res.grip_frequency_hz is not None and res.grip_frequency_hz != resolved.rate_hz:
            res.grip_frequency_error = (
                f'夹爪JSON频率冲突：{res.grip_frequency_source}={res.grip_frequency_hz:g}Hz；'
                f'{resolved.source}={resolved.rate_hz:g}Hz')
            return
        res.grip_frequency_hz = resolved.rate_hz
        res.grip_frequency_source = resolved.source


def _save_mcap_timeline(res: PartResult, mcap_path: Path,
                        stream_name: str, timestamps: List[int],
                        allow_empty: bool = False) -> None:
    """Save extracted timestamps beside the recording; surface export errors."""
    if not timestamps and not allow_empty:
        return
    try:
        save_mcap_stream_timestamp_csv(mcap_path, stream_name, timestamps)
    except OSError as exc:
        res.error = (res.error
                     + f" 时间戳导出失败({Path(mcap_path).name}/{stream_name}):{exc}").strip()


def _save_mcap_sensor_timelines(res: PartResult, mcap_path: Path,
                                cameras: Dict[str, List[int]],
                                imu: List[int], grip: List[int]) -> None:
    for role, timestamps in cameras.items():
        _save_mcap_timeline(res, mcap_path, role, timestamps)
    if imu or getattr(imu, 'channel_present', False):
        _save_mcap_timeline(res, mcap_path, 'imu', imu, allow_empty=True)
    _save_mcap_timeline(res, mcap_path, 'grip', grip)


def _extend_notices(res: PartResult, source) -> None:
    for notice in getattr(source, "notices", []):
        if notice not in res.notices:
            res.notices.append(notice)


def _merge_sei_audit(res: PartResult, role: str, info: Dict) -> None:
    """把MCAP提取的字段1/SEI核对结果并入报告角色；只累加计数与样本."""
    target = res.sei_align_audit.setdefault(role, dict(
        source=info.get('source', ''), total=0, missing=0,
        out_of_tolerance=0, max_abs_delta_us=0, samples=[]))
    target['total'] += info.get('total', 0)
    target['missing'] += info.get('missing', 0)
    target['out_of_tolerance'] += info.get('out_of_tolerance', 0)
    target['max_abs_delta_us'] = max(target['max_abs_delta_us'],
                                     info.get('max_abs_delta_us', 0))
    target['samples'].extend(info.get('samples', []))


def _configured_fps(cameras: Dict[str, CameraData], res: PartResult) -> Optional[float]:
    """首个能解析到配置帧率的相机；全部缺失时返回 None 由调用方用实测帧率."""
    for role, camera in cameras.items():
        expected = _configured_expected_us(camera, role, res)
        if expected:
            return 1_000_000.0 / expected
    return None


def _configured_expected_us(camera: CameraData, role: str, res: PartResult) -> Optional[float]:
    """理论帧间隔(µs)：优先录制配置帧率，配置缺失或无效时返回 None 由调用方推定."""
    source = camera.file
    if not source:
        return None
    cache = res.frequency_cache
    try:
        frequency = resolve_camera_frequency(source, role, cache)
    except Exception:  # noqa: BLE001 - 配置异常不阻断分析，退回时间戳推定
        return None
    rate = frequency.rate_hz
    if not rate or rate <= 0:
        return None
    return 1_000_000.0 / rate


def _camera_device_groups(cameras: Dict[str, CameraData],
                          device_by_role: Dict[str, str]) -> Dict[str, List[str]]:
    """多设备会话的相机分组；单设备返回空字典表示沿用原「全部相机一组」口径.

    设备归属未知的相机不并入任何设备组，避免把未知来源与已知设备混算。
    """
    owners = {device_by_role.get(role, '') for role in cameras}
    known = {owner for owner in owners if owner}
    if len(known) < 2:
        return {}
    groups: Dict[str, List[str]] = {}
    for role in cameras:
        owner = device_by_role.get(role, '')
        if owner:
            groups.setdefault(owner, []).append(role)
    return {device: roles for device, roles in groups.items() if roles}


def analyze_part(part: Dict, options: Optional[Dict] = None) -> PartResult:
    """对单个 part 执行全部检查项，并附着 meta 完整性/准确性检测（GUI/CLI 共用）。"""
    res = _analyze_part_impl(part, options)
    _attach_meta_integrity(res, part, options or {})
    return res


def _analyze_part_impl(part: Dict, options: Optional[Dict] = None) -> PartResult:
    """对单个 part 执行全部检查项.

    options 可选:
      {"interval_ms": 10, "row_align_ms": 10, "cam_sync_ms": 1,
       "imu_sync_ms": 1, "rgb_drop_mult": 1.5, "imu_drop_ms": 1.5,
       "depth_sync": True, "depth_sync_ms": 5}
    mcap part: 普通/深度相机严格读取消息Protobuf字段1并校验身份及SEI，
    不回退log_time；JD读取自定义字段1并校验SEI，同时提取可用IMU。
    """
    opt = options or {}
    input_plan(opt.get('_analysis_log'), part)
    read_options = {"checkpoint": opt["_read_checkpoint"]} if opt.get("_read_checkpoint") else {}
    if part.get("session_error"):
        res = PartResult(name=part["name"], path=part["path"],
                         multi_device=bool(part.get("_multi_device")),
                         session_info=part.get("session_info", {}))
        res.error = part["session_error"]
        return res
    if part.get("_multi_device"):
        return _analyze_session(part, opt)
    if part.get("mcap"):
        return _analyze_mcap(part, opt)
    res = PartResult(name=part["name"], path=part["path"],
                     session_info=part.get("session_info", {}))
    if part.get("jd"):
        from ego_checker.data.jd import CAMERA_ROLES
        if set(part["cameras"]) != set(CAMERA_ROLES.values()):
            res.error = "JD数据组缺少四路相机文件"
    # 数据文件夹名称: 多设备会话显示会话根目录名, 否则显示 part 文件上层文件夹名
    if part.get("_multi_device"):
        res.data_folder = str(part["path"].name)
    else:
        res.data_folder = str(part["path"].parent.name)

    # 读取相机 CSV
    for role, files in part["cameras"].items():
        ts_all: List[int] = []
        rows_all: List[int] = []
        err = ""
        for f in files:
            event(opt.get('_analysis_log'), '读取开始', part=res.name, kind='相机', source=f)
            if part.get("jd") and f.suffix.lower() == ".mp4":
                from ego_checker.data.jd import read_camera_pts
                t, r_, e = read_camera_pts(f)
            elif f.suffix.lower() == ".mcap":
                # depth等MCAP相机严格读取消息字段1，不回退log_time。
                from ego_checker.media.video import extract_depth_timestamps
                t, r_, e = extract_depth_timestamps(f, **read_options)
                _extend_notices(res, t)
                for info in (getattr(t, 'sei_align_audit', {}) or {}).values():
                    _merge_sei_audit(res, role, info)
                if not e and role.split('__', 1)[0] == 'depth':
                    _save_mcap_timeline(res, f, 'depth', t)
            elif f.name.lower().endswith(FRAMES_SUFFIX):
                t, r_, e = _read_frames_csv(f, **read_options)
            else:
                t, r_, e = read_csv_timestamps(f, drop_negative_col=PRESENTATION_COL, **read_options)
            res.source_read_audits[str(Path(f))] = dict(getattr(t, "read_audit", {}),
                error=e or getattr(t, "read_audit", {}).get("error", ""))
            if e:
                err = (err + " " + e).strip()
                continue
            ts_all.extend(t)
            rows_all.extend(r_)
        if not ts_all:
            res.error = (res.error + f" 相机{role}无数据").strip()
        res.cameras[role] = CameraData(
            role=role, label=ROLE_LABELS.get(role, role),
            file=files[0], timestamps=ts_all, rows=rows_all, n_rows=len(ts_all),
            error=err)
    if not res.cameras:
        res.error = "无有效相机数据"
        return res

    # 读取 IMU (多设备会话按设备拆分)
    imu_ts: List[int] = []
    imu_by_device: Dict[str, List[int]] = {}
    imu_source_maps: List[SourceRowIndex] = []
    for f in part["imu"]:
        event(opt.get('_analysis_log'), '读取开始', part=res.name, kind='IMU', source=f)
        t, rows, e = read_imu_timestamps(f, checkpoint=opt.get("_read_checkpoint"))
        res.source_read_audits[str(Path(f))] = dict(getattr(t, "read_audit", {}),
            error=e or getattr(t, "read_audit", {}).get("error", ""))
        if e:
            res.imu_error = (res.imu_error + f" {f}: {e}").strip()
            continue
        _record_imu_frequency(res, Path(f), t)
        normalized_sources = imu_source_sequences(t)
        res.imu_order_valid = res.imu_order_valid and all(
            all(b >= a for a, b in zip(source, source[1:]))
            for source in normalized_sources)
        res.imu_drop_sources.extend(normalized_sources)
        imu_ts.extend(t)
        imu_source_maps.append(_source_row_map(t, rows))
        if part.get("_multi_device"):
            dev = _imu_device_name(f)
            imu_by_device.setdefault(dev, []).extend(t)
            res.imu_drop_sources_by_device.setdefault(dev, []).extend(normalized_sources)
    imu_ts = normalize_ts(imu_ts)
    res.imu = imu_ts if imu_ts else None
    if len(imu_source_maps) == 1:
        res.imu_row_by_timestamp = imu_source_maps[0]
    if imu_by_device:
        res.imu_by_device = {dev: normalize_ts(t) for dev, t in imu_by_device.items()}

    # 读取 grip (夹爪) 时间戳流 (额外流, 仅做丢帧/间隔检查)
    grip_ts: List[int] = []
    for f in part.get("extra", {}).get("grip", []):
        t, _, e = read_csv_timestamps(f, **read_options)
        if e:
            res.imu_error = (res.imu_error + " grip:" + e).strip()
        normalized_source = normalize_ts(t)
        if normalized_source:
            res.grip_drop_sources.append(normalized_source)
            _record_grip_frequency(res, Path(f), timestamps=t)
        grip_ts.extend(t)
    if grip_ts:
        res.grip = list(normalize_ts(grip_ts))

    _load_audio(part, res, opt)

    # 各相机时间戳归一化；归一化后立即记录帧数与首尾时间戳，供释放后报告使用。
    for c in res.cameras.values():
        c.timestamps = normalize_ts(c.timestamps)
        c.record_retained_edges()
    _load_video_durations(res, log=opt.get('_analysis_log'))

    if not res.firmware:
        res.firmware = _firmware_from_meta(part["path"])

    return _run_checks(res, opt)


def _load_audio(part: Dict, res: PartResult, options: Optional[Dict] = None) -> None:
    opt = options or {}
    if not opt.get("audio", True):
        # 用户关闭音频测试：不读取音频源，也不生成缺失/未验证证据（否则会误报音频问题）。
        res.audio_evidence = []
        res.audio = None
        res.audio_enabled = False
        return
    audio_args = {
        "target_duration_s": opt.get("audio_target_s"),
        "drift_ppm": opt.get("audio_drift_ppm"),
        "calculate_sha256": bool(opt.get("audio_sha256", False)),
    }
    if opt.get("_read_checkpoint"):
        audio_args["checkpoint"] = opt["_read_checkpoint"]
    device_keys = _session_device_keys(part)
    csv_devices = {}
    for f in part.get("extra", {}).get("audio", []):
        device = device_keys.get(Path(f), _imu_device_name(Path(f)))
        csv_devices[Path(f)] = device
        with stage(opt.get('_analysis_log'), f'音频读取及波形扫描 {f}'):
            res.audio_evidence.append(audio_validation.analyze_csv(
                f, device=device, **audio_args))
    mcaps = part.get("mcaps", []) or ([part["mcap"]] if part.get("mcap") else [])
    mcap_devices = {}
    for f in mcaps:
        device = device_keys.get(Path(f), _imu_device_name(Path(f)))
        mcap_path = Path(f)
        mcap_devices[mcap_path] = device
        preloaded = (opt.get("_mcap_audio_streams") or {}).get(mcap_path)
        with stage(opt.get('_analysis_log'), f'MCAP音频统计 {f}'):
            evidence = audio_validation.analyze_mcap(
                f, device=device, preloaded_streams=preloaded, **audio_args)
        res.audio_evidence.extend(evidence)
        active = [item for item in evidence if item.timestamps]
        for index, item in enumerate(active, 1):
            stream_name = 'audio' if len(active) == 1 else f'audio_{index}'
            _save_mcap_timeline(res, mcap_path, stream_name, item.timestamps)
    if mcaps and part.get("extra", {}).get("audio"):
        # Mixed-device inputs are valid, but two representations of the same
        # audio cannot be silently concatenated or used as each other's fallback.
        duplicates = set(csv_devices.values()) & set(mcap_devices.values())
        for device in sorted(duplicates):
            res.audio_evidence.append(audio_validation.AudioEvidence(
                str(part["path"]), reason="同设备同时存在 CSV/MCAP 音频，来源对应关系未验证",
                device=device))

    # 多设备会话必须逐设备给出音频结论。没有音频源的设备也要生成独立证据：
    # 腕带为 N/A；其他设备按本地元数据判断为禁用、缺失文件或未验证。
    expected_devices = list(dict.fromkeys(res.camera_device_by_role.values()))
    present_devices = {e.device for e in res.audio_evidence if e.device}
    for device in expected_devices:
        if device in present_devices:
            continue
        roles = [role for role, owner in res.camera_device_by_role.items()
                 if owner == device]
        local_cameras = {
            role: [res.cameras[role].file]
            for role in roles if role in res.cameras and res.cameras[role].file
        }
        evidence = audio_validation.missing_audio(
            {"path": part["path"], "cameras": local_cameras, "imu": []}, roles)
        evidence.device = device
        res.audio_evidence.append(evidence)
    if not res.audio_evidence:
        evidence = audio_validation.missing_audio(part, res.cameras)
        sources = [camera.file for camera in res.cameras.values() if camera.file]
        if sources:
            evidence.device = _imu_device_name(sources[0])
        res.audio_evidence.append(evidence)
    # This legacy display field is never used as a merged clock for validation.
    res.audio = [t for e in res.audio_evidence for t in e.timestamps] or None


def _load_video_durations(res: PartResult, *, log=None) -> None:
    """Read MP4 container durations; use per-stream timestamps for MCAP video."""
    from ego_checker.media.video import mp4_duration_seconds, resolve_camera_mp4s

    video_by_role = resolve_camera_mp4s({
        role: camera.file for role, camera in res.cameras.items() if camera.file
    })
    for role, camera in res.cameras.items():
        files = video_by_role.get(role, [])
        if files:
            total = 0.0
            errors = []
            for path in files:
                with stage(log, f'读取视频时长 {path}'):
                    duration, error = mp4_duration_seconds(path)
                if error:
                    event(log, '读取异常', part=res.name, source=path, reason=error)
                if duration is None:
                    errors.append(f"{Path(path).name}:{error}")
                else:
                    total += duration
            if total > 0 and not errors:
                res.video_duration_by_role[role] = total
            else:
                res.video_duration_error_by_role[role] = "；".join(errors) or "视频时长不可用"
            continue
        if camera.file and camera.file.suffix.lower() == ".mcap":
            if camera.error:
                reason = ('MCAP未完整收尾（缺少结束标记）'
                          if 'MCAP未完整收尾（缺少结束标记）' in camera.error else camera.error)
                res.video_duration_error_by_role[role] = f"数据读取异常，{reason}；时长未验证"
                continue
            timestamps = camera.timestamps
            if len(timestamps) >= 2:
                # MCAP 没有独立视频容器时长，以首帧到尾帧加一个实测帧周期
                # 表示完整帧覆盖范围，与 MP4 presentation duration 口径一致。
                duration_us = timestamps[-1] - timestamps[0] + expected_interval_us(timestamps)
                if duration_us > 0:
                    res.video_duration_by_role[role] = duration_us / 1_000_000
                    continue
            res.video_duration_error_by_role[role] = "MCAP 视频时间戳不足，时长未验证"
        else:
            res.video_duration_error_by_role[role] = "未找到对应 MP4 视频"


def _mcap_role_for_device(device: str, role: str) -> str:
    """结合设备身份重映射 mcap 相机角色, 避免多设备同名 topic (/camera/left) 互相覆盖.

    - EGO (双目): left→left_rgb, right→right_rgb
    - WristCam-L : 任意相机路 → wrist_left
    - WristCam-R : 任意相机路 → wrist_right
    其它设备保持原角色.
    """
    d = (device or "").upper()
    if "WRISTCAM-L" in d or d.startswith("WRISTCAM_L") or d == "WRISTCAM-L":
        return "wrist_left"
    if "WRISTCAM-R" in d or d.startswith("WRISTCAM_R") or d == "WRISTCAM-R":
        return "wrist_right"
    # EGO 双目及其它: 用 topic 推导的 role (left_rgb / right_rgb / ...)
    return role


def _analyze_session(part: Dict, opt: Dict) -> PartResult:
    """多设备会话 part: 混合源分析.

    EGO 双目时间戳来自 .mcap (extract_mcap_timestamps), 腕带相机来自 mp4/CSV,
    IMU 按设备拆分 (mcap 设备取 mcap IMU, CSV 设备取 imu 文件). 合并后统一跑测试.
    """
    from ego_checker.media.video import extract_mcap_sensor_timestamps
    read_options = {"checkpoint": opt["_read_checkpoint"]} if opt.get("_read_checkpoint") else {}
    res = PartResult(name=part["name"], path=part["path"], multi_device=True,
                     session_info=part.get("session_info", {}))
    res.data_folder = str(part["path"].name)
    imu_merged: List[int] = []
    imu_source_maps_by_device: Dict[str, List[SourceRowIndex]] = collections.defaultdict(list)
    device_keys = _session_device_keys(part)
    camera_sources = []
    mcap_audio_streams = {}

    # 同设备同相机只允许一个来源。先检查完整来源清单，避免拼接或后写覆盖。
    declared_sources = collections.defaultdict(list)
    for role, files in part.get("camera_entries") or list(part["cameras"].items()):
        for source in files:
            dev = device_keys.get(Path(source), _imu_device_name(Path(source)))
            declared_sources[(dev, _mcap_role_for_device(dev, role))].append(Path(source))
    conflicts = [(identity, paths) for identity, paths in declared_sources.items() if len(paths) > 1]
    if conflicts:
        res.error = "；".join(
            f"相机来源冲突({dev}/{role})：" + "、".join(str(p) for p in paths)
            for (dev, role), paths in conflicts)
        return res

    # 1) mcap 设备: 提取相机 + IMU (相机角色结合设备身份重映射, 避免同名覆盖)
    for mc in part.get("mcaps", []):
        event(opt.get('_analysis_log'), '读取开始', part=res.name, kind='MCAP共享扫描', source=mc)
        dev = device_keys.get(Path(mc), _imu_device_name(Path(mc)))
        try:
            cams_ts, imu_ts, grip_ts = extract_mcap_sensor_timestamps(mc, isolate_camera_errors=True, **read_options)
        except AnalysisStopped:
            raise
        except Exception as e:  # noqa: BLE001
            res.error = (res.error + f" MCAP提取失败({Path(mc).name}):{e}").strip()
            res.imu_error = (res.imu_error + f" MCAP源读取不完整: {mc}: {e}").strip()
            res.camera_source_errors.append(f'{mc}: {e}')
            continue
        _save_mcap_sensor_timelines(res, Path(mc), cams_ts, imu_ts, grip_ts)
        res.source_read_audits[str(Path(mc))] = getattr(imu_ts, 'read_audit', {})
        _extend_notices(res, imu_ts)
        for role, info in (getattr(imu_ts, 'sei_align_audit', {}) or {}).items():
            _merge_sei_audit(res, _mcap_role_for_device(dev, role), info)
        mcap_audio_streams[Path(mc)] = getattr(imu_ts, "audio_streams", None)
        if getattr(imu_ts, "jd_layout", False):
            from ego_checker.data.jd import CAMERA_ROLES
            missing = [role for role in CAMERA_ROLES.values() if not cams_ts.get(role)]
            if not imu_ts:
                missing.append("imu")
            if missing:
                res.required_streams_missing.extend(f"{dev}:{role}" for role in missing)
                res.checks["required_streams"] = False
        # 单目腕带: 若设备是腕带, 只取一路相机映射为 wrist_left/right
        for role, ts in cams_ts.items():
            base_role = _mcap_role_for_device(dev, role)
            camera_sources.append((base_role, dev, Path(mc), ts, list(range(len(ts))),
                                   getattr(imu_ts, 'camera_errors', {}).get(role, '')))
        if imu_ts:
            _record_imu_frequency(res, Path(mc), imu_ts, dev)
            res.imu_order_valid = res.imu_order_valid and getattr(
                imu_ts, "order_valid", all(b >= a for a, b in zip(imu_ts, imu_ts[1:])))
            res.imu_by_device.setdefault(dev, []).extend(imu_ts)
            imu_source_maps_by_device[dev].append(
                _source_row_map(imu_ts, range(len(imu_ts))))
            imu_merged.extend(imu_ts)
            normalized_sources = imu_source_sequences(imu_ts)
            res.imu_drop_sources.extend(normalized_sources)
            res.imu_drop_sources_by_device.setdefault(dev, []).extend(normalized_sources)
        if grip_ts:
            _record_grip_frequency(res, Path(mc), dev, grip_ts)
            res.grip_by_device.setdefault(dev, []).extend(grip_ts)
            source_sequences = getattr(grip_ts, "source_sequences", None) or [list(grip_ts)]
            normalized_sources = [normalize_ts(list(source))
                                  for source in source_sequences if source]
            res.grip_drop_sources.extend(normalized_sources)
            res.grip_drop_sources_by_device.setdefault(dev, []).extend(normalized_sources)

    # 2) CSV 相机 (腕带)
    camera_entries = part.get("camera_entries") or list(part["cameras"].items())
    for role, files in camera_entries:
        if not files:
            continue
        dev = device_keys.get(Path(files[0]), _imu_device_name(Path(files[0])))
        ts_all: List[int] = []
        rows_all: List[int] = []
        err = ""
        for f in files:
            event(opt.get('_analysis_log'), '读取开始', part=res.name, kind='相机', source=f)
            if f in part.get("jd_sources", []) and f.suffix.lower() == ".mp4":
                from ego_checker.data.jd import read_camera_pts
                t, r_, e = read_camera_pts(f)
            elif f.suffix.lower() == ".mcap":
                # depth等MCAP相机严格读取消息字段1，不回退log_time。
                from ego_checker.media.video import extract_depth_timestamps
                t, r_, e = extract_depth_timestamps(f, **read_options)
                _extend_notices(res, t)
                for info in (getattr(t, 'sei_align_audit', {}) or {}).values():
                    _merge_sei_audit(res, role, info)
                if not e and role.split('__', 1)[0] == 'depth':
                    _save_mcap_timeline(res, f, 'depth', t)
            elif f.name.lower().endswith(FRAMES_SUFFIX):
                t, r_, e = _read_frames_csv(f, **read_options)
            else:
                t, r_, e = read_csv_timestamps(f, drop_negative_col=PRESENTATION_COL, **read_options)
            res.source_read_audits[str(Path(f))] = dict(getattr(t, "read_audit", {}),
                error=e or getattr(t, "read_audit", {}).get("error", ""))
            if e:
                err = (err + " " + e).strip()
                continue
            ts_all.extend(t)
            rows_all.extend(r_)
        base_role = _mcap_role_for_device(dev, role)
        camera_sources.append((base_role, dev, Path(files[0]), ts_all, rows_all, err))

    role_counts = collections.Counter(source[0] for source in camera_sources)
    source_groups = collections.defaultdict(list)
    for base_role, dev, source, *_rest in camera_sources:
        source_groups[(dev, base_role)].append(source)
    conflicts = [(identity, paths) for identity, paths in source_groups.items() if len(paths) > 1]
    if conflicts:
        res.error = "；".join(
            f"相机来源冲突({dev}/{role})：" + "、".join(str(p) for p in paths)
            for (dev, role), paths in conflicts)
        return res
    for base_role, dev, source, timestamps, rows, err in camera_sources:
        role = (_session_role_key(base_role, dev)
                if role_counts[base_role] > 1 else base_role)
        if role in res.cameras:
            res.error = f"相机设备键冲突：{dev}/{base_role}；来源 {source} 与 {res.cameras[role].file}"
            return res
        if not timestamps:
            res.error = (res.error + f" 相机{dev}:{base_role}无数据").strip()
        label = ROLE_LABELS.get(base_role, base_role)
        if role_counts[base_role] > 1:
            label = f"{dev} {label}"
        res.cameras[role] = CameraData(
            role=role, label=label, file=source, timestamps=timestamps,
            rows=rows, n_rows=len(timestamps), error=err)
        res.camera_device_by_role[role] = dev

    # 3) CSV IMU (腕带), 按设备拆分
    for f in part["imu"]:
        dev = device_keys.get(Path(f), _imu_device_name(f))
        event(opt.get('_analysis_log'), '读取开始', part=res.name, kind='IMU', source=f)
        t, rows, e = read_imu_timestamps(f, checkpoint=opt.get("_read_checkpoint"))
        res.source_read_audits[str(Path(f))] = dict(getattr(t, "read_audit", {}),
            error=e or getattr(t, "read_audit", {}).get("error", ""))
        if e:
            res.imu_error = (res.imu_error + f" {f}: {e}").strip()
            continue
        _record_imu_frequency(res, Path(f), t, dev)
        imu_merged.extend(t)
        normalized_sources = imu_source_sequences(t)
        res.imu_order_valid = res.imu_order_valid and all(
            all(b >= a for a, b in zip(source, source[1:]))
            for source in normalized_sources)
        res.imu_by_device.setdefault(dev, []).extend(t)
        imu_source_maps_by_device[dev].append(_source_row_map(t, rows))
        res.imu_drop_sources.extend(normalized_sources)
        res.imu_drop_sources_by_device.setdefault(dev, []).extend(normalized_sources)

    imu_merged = normalize_ts(imu_merged)
    res.imu = imu_merged if imu_merged else None
    if res.imu_by_device:
        res.imu_by_device = {dev: normalize_ts(t)
                             for dev, t in res.imu_by_device.items() if t}
        res.imu_row_by_timestamp_by_device = {
            dev: maps[0] for dev, maps in imu_source_maps_by_device.items()
            if len(maps) == 1
        }

    # 4) grip (夹爪) 额外时间戳流
    grip_ts: List[int] = []
    for f in part.get("extra", {}).get("grip", []):
        t, _, e = read_csv_timestamps(f, **read_options)
        if e:
            res.imu_error = (res.imu_error + " grip:" + e).strip()
        grip_ts.extend(t)
        dev = device_keys.get(Path(f), _imu_device_name(f))
        if t:
            _record_grip_frequency(res, Path(f), dev, t)
        res.grip_by_device.setdefault(dev, []).extend(t)
        normalized_source = normalize_ts(t)
        if normalized_source:
            res.grip_drop_sources.append(normalized_source)
            res.grip_drop_sources_by_device.setdefault(dev, []).append(normalized_source)
    if res.grip_by_device:
        res.grip_by_device = {
            dev: list(normalize_ts(timestamps))
            for dev, timestamps in res.grip_by_device.items() if timestamps
        }
        if len(res.grip_by_device) == 1:
            res.grip = next(iter(res.grip_by_device.values()))
    elif grip_ts:
        res.grip = list(normalize_ts(grip_ts))

    audio_options = dict(opt)
    audio_options["_mcap_audio_streams"] = mcap_audio_streams
    _load_audio(part, res, audio_options)

    for c in res.cameras.values():
        c.timestamps = normalize_ts(c.timestamps)
        c.record_retained_edges()
    _load_video_durations(res, log=opt.get('_analysis_log'))

    if not res.cameras:
        res.error = "无有效相机数据"
        return res
    if not res.firmware:
        res.firmware = (_session_firmware_summary(part, device_keys)
                        or _firmware_from_meta(part["path"]))
    return _run_checks(res, opt)


def _analyze_mcap(part: Dict, opt: Dict) -> PartResult:
    """mcap part: 提取各相机视频时间戳及可用 IMU/音频，运行通用检查。"""
    from ego_checker.media.video import extract_mcap_sensor_timestamps
    read_options = {"checkpoint": opt["_read_checkpoint"]} if opt.get("_read_checkpoint") else {}
    mcap_path = part["mcap"]
    res = PartResult(name=part["name"], path=part["path"],
                     session_info=part.get("session_info", {}))
    res.data_folder = str(part["path"].parent.name)
    try:
        event(opt.get('_analysis_log'), '读取开始', part=res.name, kind='MCAP共享扫描', source=mcap_path)
        cams_ts, imu_ts, grip_ts = extract_mcap_sensor_timestamps(mcap_path, isolate_camera_errors=True, **read_options)
    except AnalysisStopped:
        raise
    except Exception as e:  # noqa: BLE001
        if opt.get('_analysis_log'):
            import traceback
            opt['_analysis_log']('[读取异常] MCAP\n' + traceback.format_exc())
        res.error = f"MCAP 提取失败: {e}"
        return res
    if not cams_ts:
        res.error = "MCAP 未提取到相机时间戳"
        return res
    _save_mcap_sensor_timelines(res, mcap_path, cams_ts, imu_ts, grip_ts)
    res.source_read_audits[str(Path(mcap_path))] = getattr(imu_ts, 'read_audit', {})
    _extend_notices(res, imu_ts)
    for role, info in (getattr(imu_ts, 'sei_align_audit', {}) or {}).items():
        _merge_sei_audit(res, role, info)
    for role, ts in cams_ts.items():
        res.cameras[role] = CameraData(
            role=role, label=ROLE_LABELS.get(role, role),
            file=mcap_path, timestamps=ts, rows=list(range(len(ts))),
            n_rows=len(ts), error=getattr(imu_ts, 'camera_errors', {}).get(role, ''))
    res.imu_order_valid = getattr(imu_ts, "order_valid", all(b >= a for a, b in zip(imu_ts, imu_ts[1:])))
    res.imu_drop_sources = imu_source_sequences(imu_ts)
    res.imu = list(imu_ts) if imu_ts else None
    if imu_ts:
        _record_imu_frequency(res, Path(mcap_path), imu_ts)
    res.imu_row_by_timestamp = _source_row_map(
        imu_ts, range(len(imu_ts))) if imu_ts else {}
    grip_sources = getattr(grip_ts, "source_sequences", None) or ([list(grip_ts)] if grip_ts else [])
    res.grip_drop_sources = [normalize_ts(list(source))
                             for source in grip_sources if source]
    res.grip = list(normalize_ts(grip_ts)) if grip_ts else None
    if grip_ts:
        _record_grip_frequency(res, Path(mcap_path), timestamps=grip_ts)
    audio_options = dict(opt)
    audio_options["_mcap_audio_streams"] = {
        Path(mcap_path): getattr(imu_ts, "audio_streams", None),
    }
    _load_audio(part, res, audio_options)
    _load_video_durations(res, log=opt.get('_analysis_log'))
    if not res.firmware:
        res.firmware = _firmware_from_meta(part["path"])
    result = _run_checks(res, opt)

    # MCAP 没有可用消息时，通用检查会把可选 IMU 视为跳过。若设备元数据明确
    # 声明流已启用，则缺失必须是数据失败，并保留具体缺流名称供报告解释。
    try:
        meta = audio_validation.metadata_for(mcap_path)
    except (OSError, ValueError, TypeError):
        meta = {}
    streams = meta.get("streams", {}) if isinstance(meta, dict) else {}
    role_by_stream = {
        "color_left": "left_rgb",
        "color_right": "right_rgb",
        "color_up": "up_rgb",
        "depth_rgb": "depth_rgb",
        "depth": "depth",
    }
    for stream_name, role in role_by_stream.items():
        profile = streams.get(stream_name, {}) if isinstance(streams, dict) else {}
        if isinstance(profile, dict) and profile.get("enabled") is True and role not in result.cameras:
            result.required_streams_missing.append(role)
    imu_profile = streams.get("imu", {}) if isinstance(streams, dict) else {}
    if isinstance(imu_profile, dict) and imu_profile.get("enabled") is True and not result.imu:
        result.required_streams_missing.append("imu")
    if getattr(imu_ts, "jd_layout", False):
        from ego_checker.data.jd import CAMERA_ROLES
        for role in (*CAMERA_ROLES.values(), "imu"):
            present = bool(result.imu) if role == "imu" else role in result.cameras
            if not present and role not in result.required_streams_missing:
                result.required_streams_missing.append(role)
    if result.required_streams_missing:
        result.checks["required_streams"] = False
    return result


def _run_checks(res: PartResult, opt: Dict) -> PartResult:
    """在 res.cameras / res.imu 已就绪后, 统一执行 8 项检查并汇总异常帧."""
    log = opt.get('_analysis_log')
    event(log, '检查进度', part=res.name, step='频率与配置核对')
    res.imu_frequency_audit = []
    res.imu_frequency_validation_errors = []
    for profile in res.imu_frequency_profiles:
        device = profile['device']
        sources = (res.imu_drop_sources_by_device.get(device, []) if device
                   else res.imu_drop_sources)
        rows = audit_magnetic_frequency(profile, sources)
        res.imu_frequency_audit.extend(rows)
        res.imu_frequency_validation_errors.extend(
            f'{device or "当前设备"}: {profile["configuration_source"]}: {error}'
            for error in profile['configuration_errors'])
        res.imu_frequency_validation_errors.extend(
            f'{device or "当前设备"}/{row["sensor"]}: {row["reason"]}；{row["configuration_source"]}'
            for row in rows if row['status'] == 'ERROR')
    if res.imu_frequency_profiles:
        res.checks['imu_frequency'] = not res.imu_frequency_validation_errors
    invalid = {role: f'{camera.file}: {camera.error or "相机无数据"}'
               for role, camera in res.cameras.items()
               if camera.error or not camera.timestamps}
    cameras = {role: camera for role, camera in res.cameras.items() if role not in invalid}
    camera_errors = [f'{role}: {error}' for role, error in invalid.items()]
    if camera_errors:
        res.error = (res.error + ' 相机输入未完整读取：' + '；'.join(camera_errors)).strip()
        for role, error in invalid.items():
            for kind in ('interval', 'rgb_drop', 'imu_sync', 'grip_sync'):
                res.unavailable_checks[f'{kind}_{role}'] = error
        res.unavailable_checks['rgb_drop'] = '；'.join(camera_errors)
        res.notices.append('相机异常已隔离，完整来源继续独立检查；异常段不参与跨段补偿、SEI对齐及组帧截图。')
    if res.camera_source_errors:
        res.unavailable_checks['rgb_drop'] = '；'.join(camera_errors + res.camera_source_errors)

    def available(key, roles):
        errors = ([f'{role}: {invalid[role]}' for role in roles if role in invalid]
                  + res.camera_source_errors)
        if errors:
            res.unavailable_checks[key] = '；'.join(errors)
        return not errors

    if res.imu_error:
        res.invalidate_imu_results()
    adaptive = bool(opt.get("adaptive_thresholds", False))
    res.depth_sync_enabled = bool(opt.get("depth_sync", True))
    res.depth_sync_threshold_us = _depth_sync_threshold_us(adaptive, opt.get('depth_sync_ms', 5.0))
    if not res.depth_sync_enabled and any(
            role.split('__', 1)[0] == 'depth' for role in res.cameras):
        res.notices.append('Depth 相机间同步未测试（用户关闭）；Depth RGB 仅与左、右、上三目比较，'
                           '不含 Depth；鱼眼内部同步及其它检查照常执行。')
    interval_ms = opt.get("interval_ms", INTERVAL_WAVE_MS)
    row_align_ms = opt.get("row_align_ms", ROW_ALIGN_THRESHOLD_US / US_PER_MS)
    cam_sync_ms = opt.get("cam_sync_ms", CAM_SYNC_THRESHOLD_US / US_PER_MS)
    imu_sync_ms = opt.get("imu_sync_ms", IMU_SYNC_THRESHOLD_US / US_PER_MS)
    rgb_drop_mult = opt.get("rgb_drop_mult", RGB_DROP_MULTIPLIER)

    def guard(keys, label, fn):
        # 评估参数相关计算失败时仅该检查项报错，完好数据继续分析，不中断整体。
        try:
            return fn()
        except (AnalysisStopped, MemoryError):
            raise
        except Exception as exc:  # noqa: BLE001
            reason = f'{label}计算失败：{describe_exception(exc)}'
            for key in keys:
                res.unavailable_checks.setdefault(key, reason)
            res.notices.append(reason)
            return None

    # 时长 / 帧率 (以首个相机为参考)
    if cameras:
        first_role = next(iter(cameras))
        ref = cameras[first_role]
        if len(ref.timestamps) >= 2:
            res.duration_s = (ref.timestamps[-1] - ref.timestamps[0]) / 1e6
            if res.duration_s > 0:
                res.fps = (len(ref.timestamps) - 1) / res.duration_s

    # 自适应阈值: 帧率基准优先取录制配置，配置缺失时才用实测帧率。
    # 计算失败时回退固定阈值，不中断其它检查。
    if adaptive:
        def _adaptive_thresholds():
            nonlocal interval_ms, row_align_ms, cam_sync_ms, imu_sync_ms
            threshold_fps = _configured_fps(cameras, res) or res.fps
            interval_ms = _adaptive_rate_threshold_ms(threshold_fps)
            row_align_ms = _adaptive_rate_threshold_ms(threshold_fps)
            cam_sync_ms = _adaptive_cam_sync_ms_single({
                role: camera for role, camera in cameras.items() if res.depth_sync_enabled
                or role.split('__', 1)[0] != 'depth'})
            if not res.imu_error and not res.imu_frequency_error:
                imu_sync_ms = _adaptive_imu_sync_ms(res.imu_frequency_hz)
        guard([], '自适应阈值', _adaptive_thresholds)

    event(log, '检查进度', part=res.name, step='相机帧数、帧间隔与同行对齐')
    depth_roles = [role for role in ('depth', 'depth_rgb') if role in res.cameras]
    fisheye_roles = [role for role in res.cameras if role not in depth_roles]
    # 多设备会话：帧数对齐与同行对齐只在各设备自己的相机组内比较，不跨设备取差值。
    # 单目设备组内没有第二路相机可比，按跳过处理且不计入失败。
    device_groups = _camera_device_groups(cameras, res.camera_device_by_role)
    if device_groups:
        device_ok = True
        for device, device_roles in device_groups.items():
            device_cameras = {role: cameras[role] for role in device_roles}
            if len(device_roles) < 2:
                reason = f'{device or "归属未知设备"}为单目相机，组内无第二路可比，跳过帧数对齐与同行对齐'
                res.frame_count_skipped_devices[device] = reason
                res.notices.append(reason)
                continue
            if available('frame_count', device_roles):
                frame_count = check_frame_count(device_cameras)
                res.frame_count_by_device[device] = frame_count
                if not frame_count.passed:
                    device_ok = False
            row_roles = [role for role in device_roles if role != 'depth']
            if len(row_roles) >= 2 and available('row_align', row_roles):
                row_align = check_row_align(
                    {role: cameras[role] for role in row_roles},
                    threshold_us=int(row_align_ms * US_PER_MS))
                res.row_align_by_device[device] = row_align
                if not row_align.passed:
                    device_ok = False
        res.checks['frame_count'] = device_ok
        res.checks['row_align'] = device_ok
    else:
        if available('frame_count', res.cameras):
            res.frame_count = check_frame_count(cameras)
            res.checks["frame_count"] = res.frame_count.passed
        if depth_roles and fisheye_roles:
            for group, roles in (('depth', depth_roles), ('fisheye', fisheye_roles)):
                if available(f'frame_count_{group}', roles):
                    frame_count = check_frame_count({role: cameras[role] for role in roles})
                    res.frame_count_groups[group] = frame_count
                    res.checks[f'frame_count_{group}'] = frame_count.passed

        row_roles = (fisheye_roles if 'depth' in res.cameras and 'depth_rgb' in res.cameras
                     else [role for role in res.cameras if role != 'depth'])
        if available('row_align', row_roles):
            res.row_align = check_row_align(
                {role: cameras[role] for role in row_roles}, threshold_us=int(row_align_ms * US_PER_MS))
            res.checks["row_align"] = res.row_align.passed

    # 理论间隔优先取录制配置帧率；配置缺失或无效时才按时间戳推定。
    configured_rates = {}
    for role, c in cameras.items():
        def _interval(role=role, c=c):
            expected = _configured_expected_us(c, role, res)
            configured_rates[role] = expected
            res.intervals[role] = check_interval(
                c, threshold_us=int(interval_ms * US_PER_MS), expected_us=expected)
            res.checks[f"interval_{role}"] = res.intervals[role].passed
        guard([f'interval_{role}'], f'相机{role}帧间隔', _interval)

    # 相机基准同步: 含深度流的产品按「同步组」计算
    # (Depth基准/Depth RGB基准/鱼眼组),
    # 其它数据保持原「全组两两最近邻」逻辑不变
    event(log, '检查进度', part=res.name, step='相机同步')
    def _cam_sync_stage():
        pair_groups = _cam_sync_pair_groups(
            res.cameras, adaptive=adaptive, depth_sync=res.depth_sync_enabled,
            depth_sync_ms=res.depth_sync_threshold_us / US_PER_MS)
        if pair_groups is not None:
            for group, pairs in pair_groups.items():
                roles = {role for pair in pairs for role in pair[:2]}
                if available(f'cam_sync_group_{group}', roles):
                    res.cam_sync_groups[group] = _cam_sync_by_pairs(
                        cameras, pairs, directed=(group != 'fisheye'))
                    res.cam_sync.update(res.cam_sync_groups[group])
            res.cam_sync_grouped = True
        else:
            sync_roles = [role for role in res.cameras if res.depth_sync_enabled
                          or role.split('__', 1)[0] != 'depth']
            if available('cam_sync', sync_roles):
                res.cam_sync = check_cam_sync(
                    {role: cameras[role] for role in sync_roles},
                    threshold_us=int(cam_sync_ms * US_PER_MS))
            else:
                for role in sync_roles:
                    res.unavailable_checks[f'cam_sync_{role}'] = res.unavailable_checks['cam_sync']
        for role, sr in res.cam_sync.items():
            res.checks[f"cam_sync_{role}"] = sr.passed
        for group, sync_results in res.cam_sync_groups.items():
            res.checks[f"cam_sync_group_{group}"] = all(
                item.passed for item in sync_results.values())
    guard([f'cam_sync_{role}' for role in cameras], '相机同步', _cam_sync_stage)

    event(log, '检查进度', part=res.name, step='IMU同步')
    def _imu_sync_stage():
        if res.imu_error:
            pass  # 本 Part 的 IMU 输入未完整读取，不用前缀或其它来源替代。
        elif res.multi_device or res.camera_device_by_role:
            # 多设备会话中，每路相机只与所属设备的 IMU 时钟比较。本设备没有
            # IMU 时保留为无匹配，不得退回合并池借用其它设备的时钟。
            res.imu_sync = {}
            for device in dict.fromkeys(res.camera_device_by_role.get(role, "") for role in cameras):
                device_cameras = {role: camera for role, camera in cameras.items()
                                  if res.camera_device_by_role.get(role, "") == device}
                if device in res.imu_frequency_error_by_device:
                    for role in device_cameras:
                        res.unavailable_checks[f'imu_sync_{role}'] = res.imu_frequency_error_by_device[device]
                    continue
                timestamps = res.imu_by_device.get(device, [])
                device_threshold_ms = (_adaptive_imu_sync_ms(
                    res.imu_frequency_hz_by_device.get(device))
                                       if adaptive else imu_sync_ms)
                sync = check_imu_sync(
                    device_cameras, timestamps,
                    threshold_us=int(device_threshold_ms * US_PER_MS),
                    source_rows=res.imu_row_by_timestamp_by_device.get(device),
                )
                res.imu_sync.update(sync)
            res.imu_sync = {role: res.imu_sync[role] for role in cameras if role in res.imu_sync}
        elif res.imu_frequency_error:
            for role in cameras:
                res.unavailable_checks[f'imu_sync_{role}'] = res.imu_frequency_error
        else:
            res.imu_sync = check_imu_sync(cameras, res.imu or [],
                                          threshold_us=int(imu_sync_ms * US_PER_MS),
                                          source_rows=res.imu_row_by_timestamp)
        for role, sr in res.imu_sync.items():
            res.checks[f"imu_sync_{role}"] = sr.passed
    guard([f'imu_sync_{role}' for role in cameras], 'IMU同步', _imu_sync_stage)

    event(log, '检查进度', part=res.name, step='夹爪同步与各流连续性')
    # 夹爪同步仅匹配相机所属设备；多设备缺少归属时不猜测、不借用其它夹爪时钟。
    def _grip_sync_stage():
        res.grip_sync = {}
        if res.camera_device_by_role:
            for role, camera in cameras.items():
                device = res.camera_device_by_role.get(role, "")
                timestamps = res.grip_by_device.get(device, [])
                if not timestamps:
                    continue
                frequency = res.grip_frequency_hz_by_device.get(device)
                error = res.grip_frequency_error_by_device.get(device)
                if not error and frequency is None:
                    error = f'{device or "归属未知设备"}缺少夹爪配置频率且无法推算；不借用其它设备配置'
                    res.grip_frequency_error_by_device[device] = error
                if error:
                    res.unavailable_checks[f'grip_sync_{role}'] = error
                    continue
                sync = check_grip_sync({role: camera}, timestamps, frequency_hz=frequency,
                                       frequency_source=res.grip_frequency_source_by_device.get(device, ''))
                if role in sync:
                    res.grip_sync[role] = sync[role]
        elif res.grip:
            if not res.grip_frequency_error and res.grip_frequency_hz is None:
                res.grip_frequency_error = '缺少夹爪配置频率且无法按时间戳推算'
            if res.grip_frequency_error:
                for role in cameras:
                    res.unavailable_checks[f'grip_sync_{role}'] = res.grip_frequency_error
            else:
                res.grip_sync = check_grip_sync(cameras, res.grip, frequency_hz=res.grip_frequency_hz,
                                               frequency_source=res.grip_frequency_source)
        for role, sr in res.grip_sync.items():
            res.checks[f"grip_sync_{role}"] = sr.passed
    guard([f'grip_sync_{role}' for role in cameras], '夹爪同步', _grip_sync_stage)

    for role, c in cameras.items():
        # 丢帧阈值基准同样优先取录制配置帧率，配置缺失时才按时间戳推定。
        def _rgb_drop(role=role, c=c):
            exp = configured_rates.get(role)
            if exp is None:
                exp = expected_interval_us(c.timestamps)   # 无配置时的兜底：1/帧率
            res.rgb_drop[role] = check_rgb_drop(c, exp, multiplier=rgb_drop_mult)
            res.checks[f"rgb_drop_{role}"] = res.rgb_drop[role].passed
        guard([f'rgb_drop_{role}'], f'相机{role}丢帧', _rgb_drop)

    def _imu_drop_stage():
        if res.imu_error:
            pass
        elif res.imu_frequency_error:
            res.checks["imu_frequency"] = False
        elif res.imu:
            imu_drop_mult = opt.get("imu_drop_mult", IMU_DROP_MULTIPLIER)
            if len(res.imu_by_device) > 1:
                # 多设备: IMU 丢帧按设备拆分, 并计算多组 IMU 差值
                all_pass = True
                for dev, dts in res.imu_by_device.items():
                    if dev in res.imu_frequency_error_by_device:
                        res.checks['imu_frequency'] = False
                        all_pass = False
                        continue
                    sources = res.imu_drop_sources_by_device.get(dev) or [dts]
                    dr = check_imu_drop_sources(
                        sources, multiplier=imu_drop_mult,
                        frequency_hz=res.imu_frequency_hz_by_device.get(dev))
                    res.imu_drop_by_device[dev] = dr
                    if not dr.passed:
                        all_pass = False
                res.checks["imu_drop"] = all_pass
                res.imu_groups_diff = check_imu_groups_diff(list(res.imu_by_device.values()))
            elif not res.imu_frequency_error_by_device:
                sources = res.imu_drop_sources or [res.imu]
                single_rate = res.imu_frequency_hz
                if single_rate is None and len(res.imu_frequency_hz_by_device) == 1:
                    single_rate = next(iter(res.imu_frequency_hz_by_device.values()))
                res.imu_drop = check_imu_drop_sources(
                    sources, multiplier=imu_drop_mult,
                    frequency_hz=single_rate)
                res.checks["imu_drop"] = res.imu_drop.passed
            else:
                res.checks['imu_frequency'] = False
        else:
            res.checks["imu_drop"] = True
    guard(['imu_drop', 'imu_frequency'], 'IMU丢帧', _imu_drop_stage)

    # grip (夹爪) 额外流连续性检查
    def _grip_drop_stage():
        if res.grip_by_device:
            grip_mult = opt.get("grip_drop_mult", opt.get("imu_drop_mult", IMU_DROP_MULTIPLIER))
            all_pass = True
            for dev, timestamps in res.grip_by_device.items():
                sources = res.grip_drop_sources_by_device.get(dev) or [timestamps]
                dr = check_imu_drop_sources(
                    sources, multiplier=grip_mult, detect_duplicates=True,
                    stream_label=f"{dev}夹爪", deduplicate_sources=False,
                    infer_frequency=True)
                dr.role = "grip"
                dr.label = f"{dev}夹爪"
                for abnormal in dr.abnormal:
                    abnormal.cam = dr.label
                res.grip_drop_by_device[dev] = dr
                all_pass = all_pass and dr.passed
            combined = DropResult(role="grip", label="夹爪", passed=all_pass)
            drops = list(res.grip_drop_by_device.values())
            combined.total_frames = sum(dr.total_frames for dr in drops)
            combined.total_gaps = sum(dr.total_gaps for dr in drops)
            combined.total_lost_frames = sum(dr.total_lost_frames for dr in drops)
            combined.backstep_count = sum(dr.backstep_count for dr in drops)
            combined.duplicate_count = sum(dr.duplicate_count for dr in drops)
            combined.max_gap_us = max((dr.max_gap_us for dr in drops), default=0)
            combined.abnormal = [item for dr in drops for item in dr.abnormal]
            combined.detail = "; ".join(f"{dev}: {dr.detail}" for dev, dr in res.grip_drop_by_device.items())
            res.grip_drop = combined
            res.checks["grip_drop"] = all_pass
        elif (any(len(source) >= 2 for source in res.grip_drop_sources)
              or (res.grip and len(res.grip) >= 2)):
            grip_mult = opt.get("grip_drop_mult", opt.get("imu_drop_mult", IMU_DROP_MULTIPLIER))
            sources = res.grip_drop_sources or [res.grip]
            res.grip_drop = check_imu_drop_sources(
                sources, multiplier=grip_mult, detect_duplicates=True,
                stream_label="夹爪", deduplicate_sources=False,
                infer_frequency=True)
            res.grip_drop.role = "grip"
            res.grip_drop.label = "夹爪"
            res.checks["grip_drop"] = res.grip_drop.passed
    guard(['grip_drop'], '夹爪丢帧', _grip_drop_stage)

    # PCM integrity is independent from diagnostic-only legacy audio PTS.
    # Never feed variable-size PCM packets to the fixed-period drop estimator.
    if not res.imu_error and not res.imu_order_valid:
        res.checks["imu_source_order"] = False

    # 汇总异常帧
    for key, item in (
            ("frame_count", res.frame_count),
            ("row_align", res.row_align),
    ):
        res.over_frames_by_check[key] = list(item.abnormal)
    for role, it in res.intervals.items():
        res.over_frames_by_check[f"interval_{role}"] = list(it.abnormal)
    for role, it in res.cam_sync.items():
        res.over_frames_by_check[f"cam_sync_{role}"] = list(it.abnormal)
    for role, it in res.imu_sync.items():
        res.over_frames_by_check[f"imu_sync_{role}"] = list(it.abnormal)
    for role, it in res.grip_sync.items():
        res.over_frames_by_check[f"grip_sync_{role}"] = list(it.abnormal)
    for role, it in res.rgb_drop.items():
        res.over_frames_by_check[f"rgb_drop_{role}"] = list(it.abnormal)
    if res.imu_drop_by_device:
        all_af: List[AbnormalFrame] = []
        for dev, dr in res.imu_drop_by_device.items():
            all_af.extend(dr.abnormal)
        res.over_frames_by_check["imu_drop"] = all_af
    elif res.imu_drop is not None:
        res.over_frames_by_check["imu_drop"] = list(res.imu_drop.abnormal)

    res.checks_completed = True
    return res


# 流式分析保留的边界样本上限（严格固定，不足时判 UNVERIFIED，不外扩）。
BOUNDARY_CAM_SAMPLES = 12
BOUNDARY_IMU_SAMPLES = 1000
BOUNDARY_GRIP_SAMPLES = 10


def extract_boundary_samples(res: PartResult) -> Dict:
    """保存跨段补偿所需的边界样本后，释放本段的大块时间戳.

    只保留尾部/头部固定条数；完整列表随后清空以便回收内存。样本不足或
    数据异常时标记 unverified，由跨段补偿判为 UNVERIFIED，不用外扩补齐。
    """
    if getattr(res, '_data_released', False):
        return res.boundary_samples
    samples = {
        'cameras': {},
        'imu_by_device': {},
        'camera_span': None,
        'imu_frequency_hz': res.imu_frequency_hz,
        'imu_frequency_hz_by_device': dict(res.imu_frequency_hz_by_device),
        'unverified': '',
    }
    for role, camera in res.cameras.items():
        timestamps = camera.timestamps
        if not timestamps:
            samples['cameras'][role] = dict(head=[], tail=[], count=0, error=camera.error)
            continue
        rows = camera.rows
        samples['cameras'][role] = dict(
            head=list(timestamps[:BOUNDARY_CAM_SAMPLES]),
            tail=list(timestamps[-BOUNDARY_CAM_SAMPLES:]),
            # 头/尾样本各自的原始行号，供跨段详情展示“最近邻源行号”。
            head_rows=list(rows[:BOUNDARY_CAM_SAMPLES]),
            tail_rows=list(rows[-BOUNDARY_CAM_SAMPLES:]) if rows else [],
            count=len(timestamps),
            first=timestamps[0], last=timestamps[-1],
            # 边界检查只需首尾记录；释放原始列表前固化。
            first_row=(rows[0] + 1) if rows else None,
            last_row=(rows[-1] + 1) if rows else None,
            label=camera.label, file=camera.file, error=camera.error)
    if res.cameras and all(c.timestamps for c in res.cameras.values()):
        samples['camera_span'] = (
            min(c.timestamps[0] for c in res.cameras.values()),
            max(c.timestamps[-1] for c in res.cameras.values()))
    for device, timestamps in res.imu_by_device.items():
        ordered = sorted(timestamps)
        row_map = res.imu_row_by_timestamp_by_device.get(device, {})
        head = ordered[:BOUNDARY_IMU_SAMPLES]
        tail = ordered[-BOUNDARY_IMU_SAMPLES:] if ordered else []
        samples['imu_by_device'][device] = dict(
            head=head, tail=tail,
            # 头/尾样本的源数据行下标（0 基，同时刻成对记录取首次出现行），
            # 供跨段详情回溯“源IMU数据行号”；释放原始序列前固化。
            head_rows=[row_map.get(t) for t in head],
            tail_rows=[row_map.get(t) for t in tail],
            count=len(ordered), first=ordered[0] if ordered else None,
            last=ordered[-1] if ordered else None)
    # IMU 源序列的边界首尾：释放原始序列前固化，供跨段边界检查使用。
    samples['imu_sources'] = []
    for sequence in res.imu_drop_sources:
        rows = getattr(sequence, 'source_rows', []) or []
        if not sequence or len(sequence) != len(rows):
            continue
        sensor = getattr(sequence, 'sensor_type', '') or ''
        samples['imu_sources'].append(dict(
            source=str(getattr(sequence, 'source', '') or ''), sensor=sensor,
            count=len(sequence), first=sequence[0], last=sequence[-1],
            first_row=rows[0] + 1, last_row=rows[-1] + 1))
    if res.imu:
        ordered = sorted(res.imu)
        head = ordered[:BOUNDARY_IMU_SAMPLES]
        tail = ordered[-BOUNDARY_IMU_SAMPLES:]
        samples['imu'] = dict(head=head, tail=tail,
                              head_rows=[res.imu_row_by_timestamp.get(t) for t in head],
                              tail_rows=[res.imu_row_by_timestamp.get(t) for t in tail],
                              count=len(ordered),
                              first=ordered[0], last=ordered[-1])
    # 夹爪时间轴只用于段内同步；保留首尾固定样本供报告与复核，其余释放。
    samples['grip_by_device'] = {}
    for device, timestamps in (res.grip_by_device or {}).items():
        ordered = sorted(timestamps)
        samples['grip_by_device'][device] = dict(
            head=ordered[:BOUNDARY_GRIP_SAMPLES],
            tail=ordered[-BOUNDARY_GRIP_SAMPLES:] if ordered else [],
            count=len(ordered),
            first=ordered[0] if ordered else None,
            last=ordered[-1] if ordered else None)
    if res.grip:
        ordered = sorted(res.grip)
        samples['grip'] = dict(head=ordered[:BOUNDARY_GRIP_SAMPLES],
                               tail=ordered[-BOUNDARY_GRIP_SAMPLES:],
                               count=len(ordered),
                               first=ordered[0], last=ordered[-1])
    res.boundary_samples = samples
    res.streamed = True
    return samples


def release_part_data(res: PartResult) -> None:
    """跨段补偿完成后释放本段时间戳，仅保留报告需要的统计量."""
    if getattr(res, '_data_released', False):
        return
    for camera in res.cameras.values():
        if camera.retained_count is None:
            camera.record_retained_edges()
        camera.timestamps = []
        camera.rows = []
    res.imu = None
    res.imu_by_device = {}
    res.imu_row_by_timestamp = {}
    res.imu_row_by_timestamp_by_device = {}
    # 原始 IMU 序列只用于丢帧统计，统计结果已在 DropResult 中，可释放。
    res.imu_drop_sources = []
    res.imu_drop_sources_by_device = {}
    # 音频逐包列表仅参与分析期计算；保留「是否存在」的标记供报告统计使用。
    for evidence in res.audio_evidence:
        evidence.had_timestamps = bool(evidence.timestamps)
        evidence.had_sizes = bool(evidence.sizes)
        evidence.retained_packets = len(evidence.timestamps)
        # 报告首尾覆盖需要 CSV 物理行号，释放逐包列表前固化。
        if evidence.source_rows:
            evidence.retained_first_row = evidence.source_rows[0]
            evidence.retained_last_row = evidence.source_rows[-1]
        evidence.timestamps = []
        evidence.system_timestamps = []
        evidence.sizes = []
        evidence.pcm_start_samples = []
        evidence.source_rows = []
        evidence.csv_records = []
    res.audio = None
    # 夹爪只保留首尾固定样本；报告仅据此判断是否存在夹爪时间轴。
    grip_samples = (res.boundary_samples or {}).get('grip_by_device', {})
    res.grip_by_device = {
        device: list(sample['head']) + list(sample['tail'])
        for device, sample in grip_samples.items() if sample.get('count')}
    res.grip = list((res.boundary_samples or {}).get('grip', {}).get('head', [])) + \
        list((res.boundary_samples or {}).get('grip', {}).get('tail', [])) or None
    res._data_released = True


def run_analysis(root: Path, options: Optional[Dict] = None,
                 progress=None, *, log=None) -> Tuple[List[PartResult], str]:
    """Public analysis runs own a log unless their caller already supplies one."""
    if log is not None:
        return _run_analysis(root, options, progress, log=log)
    from ego_checker.runtime.run_log import AnalysisRunLog
    with AnalysisRunLog() as run_log:
        run_log.configuration(root, None, options or {})
        results, error = _run_analysis(root, options, progress, log=run_log.write)
        run_log.outcome = 'FAILED' if error or any(r.error for r in results) else 'COMPLETED'
        return results, error


def _run_analysis(root: Path, options: Optional[Dict] = None,
                  progress=None, *, log=None) -> Tuple[List[PartResult], str]:
    """扫描 root 下所有 part 并逐个分析.

    progress: 可选回调 fn(part_name, index, total) 用于 GUI 进度显示.
    """
    with stage(log, '扫描数据目录'):
        parts, err = discover_parts(root, input_mode=(options or {}).get("input_mode", "single"),
                                checkpoint=(options or {}).get("_read_checkpoint"),
                                layout_mode=(options or {}).get("layout_mode", "separate"),
                                pairing_level=(options or {}).get("pairing_level", "meta"))
    event(log, '扫描结果', parts=len(parts), error=err)
    if not parts:
        return [], err
    results: List[PartResult] = []
    total = len(parts)
    # 两种落盘格式都采用滑动窗口：当前段的前后邻居都就绪后做跨段补偿并释放它。
    # 保留完整原数据的旧实现仍可通过 stream_release=False 关闭。
    streamed = bool((options or {}).get("stream_release", True))
    pending: List[Tuple[Dict, PartResult]] = []
    with PartAnalyzer(parts, dict(options or {}, _analysis_log=log),
                      analyze=analyze_part, compact=streamed,
                      checkpoint=(options or {}).get("_read_checkpoint"), log=log) as batch:
        for i, part in enumerate(parts):
            event(log, '分析进度', part=part['name'], current=i+1, total=total)
            if progress:
                progress(part["name"], i, total)
            try:
                with stage(log, f"分析 {part['name']}"):
                    result = batch.get(i)
                    if streamed and not result.error:
                        # 先固化成固定边界样本，原始时间戳随 release 释放。
                        extract_boundary_samples(result)
                    results.append(result)
            except (AnalysisStopped, MemoryError, AnalysisWorkerError, TimeoutError):
                raise
            except Exception as e:  # noqa: BLE001
                if log:
                    import traceback
                    log(f"[分段异常] {part['name']}\n{traceback.format_exc()}")
                r = PartResult(name=part["name"], path=part["path"],
                               multi_device=bool(part.get("_multi_device")),
                               session_info=part.get("session_info", {}))
                r.error = f"分析异常: {e}"
                results.append(r)
            part_progress(log, results[-1])
            if streamed:
                pending.append((part, results[-1]))
                # 中间段的前后邻居都就绪：处理它的两个方向后即可释放。
                while len(pending) >= 3:
                    _apply_streamed_triplet(log, pending[0], pending[1], pending[2], options)
                    if pending[1][1].streamed:
                        release_part_data(pending[1][1])
                    pending.pop(1)
    if streamed:
        # 收尾：剩余段已无后继邻居，只补 previous 或单独处理末段。
        while len(pending) > 1:
            _apply_streamed_triplet(log, None, pending[0], pending[1], options)
            if pending[0][1].streamed:
                release_part_data(pending[0][1])
            pending.pop(0)
        if pending:
            result = pending[0][1]
            if result.streamed:
                release_part_data(result)
        return results, ""
    # 补齐 IMU 相邻分段边界参考 (GUI 与 CLI 共用)
    # 补齐 IMU 相邻分段边界参考 (GUI 与 CLI 共用)
    try:
        with stage(log, 'IMU 跨段同步'):
            apply_imu_segment_context(parts, results, options)
    except (AnalysisStopped, MemoryError):
        raise
    except Exception as exc:  # noqa: BLE001
        if log:
            import traceback
            log('[跨段异常] IMU\n' + traceback.format_exc())
        for result in results:
            result.error = (result.error + f" IMU跨段参考失败: {describe_exception(exc)}").strip()
    # 补齐 相机同步 相邻分段边界参考 (分段切割相位错位时消除边界误报)
    try:
        with stage(log, '相机跨段同步'):
            apply_cam_segment_context(parts, results, options)
    except (AnalysisStopped, MemoryError):
        raise
    except Exception as exc:  # noqa: BLE001
        if log:
            import traceback
            log('[跨段异常] 相机\n' + traceback.format_exc())
        for result in results:
            result.error = (result.error + f" 相机跨段参考失败: {describe_exception(exc)}").strip()
    return results, ""


def _apply_streamed_triplet(log, previous, current, following, options) -> None:
    """处理中间段的两个跨段方向；失败不中断整体分析."""
    from ego_checker.analysis.context import apply_streamed_triplet
    try:
        apply_streamed_triplet(previous, current, following, options)
    except (AnalysisStopped, MemoryError):
        raise
    except Exception as exc:  # noqa: BLE001
        if log:
            import traceback
            log('[跨段异常] 相邻段补偿\n' + traceback.format_exc())
        result = current[1]
        result.error = (result.error + f" 跨段边界补偿失败: {describe_exception(exc)}").strip()


def _apply_streamed_context(log, parts, results, options) -> None:
    """平铺模式：用固定边界样本完成跨段补偿，随后释放本段原始时间戳.

    相邻段补偿只依赖边界样本，因此补偿结束后可立即丢弃完整时间戳，
    常驻内存只保留各段固定的头/尾样本。
    """
    from ego_checker.analysis.context import apply_streamed_segment_context
    try:
        with stage(log, '相邻段边界补偿'):
            apply_streamed_segment_context(parts, results, options)
    except (AnalysisStopped, MemoryError):
        raise
    except Exception as exc:  # noqa: BLE001
        if log:
            import traceback
            log('[跨段异常] 边界补偿\n' + traceback.format_exc())
        for result in results:
            result.error = (result.error + f" 跨段边界补偿失败: {describe_exception(exc)}").strip()
    for result in results:
        if result.streamed:
            release_part_data(result)
    if log:
        event(log, '跨段完成', parts=len(results),
              truncated=sum(1 for r in results if r.boundary_truncation_errors))


def load_whitelist(path: Optional[Path]) -> set:
    """读取白名单文件 (每行一个 part 名), 用于从 FAIL 统计中排除."""
    ws: set = set()
    if not path or not path.exists():
        return ws
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    ws.add(line)
    except OSError:
        pass
    return ws


def apply_whitelist(results: List[PartResult], whitelist: set) -> None:
    for r in results:
        if r.name in whitelist:
            r.whitelisted = True
