"""analysis.context 的实现。"""
import bisect
import collections
import re
from itertools import chain, pairwise
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from ego_checker.analysis.sync_edges import local_sync, pending_directions, retry_edges
from ego_checker.analysis.timing import check_imu_sync, _adaptive_imu_sync_ms
from ego_checker.domain.identity import _group_token, _imu_device_name, _part_token_of, _session_device_keys
from ego_checker.domain.constants import CAM_SYNC_THRESHOLD_US, IMU_DROP_MULTIPLIER, IMU_SYNC_THRESHOLD_US, US_PER_MS
from ego_checker.domain.models import CamBoundarySample, ImuBoundarySample, PartResult


def _imu_segment_identity(part: Dict) -> Optional[Tuple[Tuple, int]]:
    """仅关联同目录系列、同源文件名、明确连续编号的单设备分段。"""
    if part.get("_multi_device"):
        return None
    mcap = part.get("mcap")
    if not mcap and len(part["imu"]) != 1:
        return None
    token = _part_token_of(part) or part["path"].name
    match = re.fullmatch(r"(part|rec|clip)?(\d+)", token, re.IGNORECASE)
    if not match:
        return None
    kind, number = (match[1] or "").lower(), int(match[2])
    sources = [("mcap", mcap)] if mcap else [
        (role, path) for role, paths in part["cameras"].items() for path in paths
    ] + [("imu", part["imu"][0])]
    keys = []
    for role, path in sources:
        path = Path(path)
        directory = path.parent.resolve()
        directory_match = re.fullmatch(r"(part|rec|clip)?_?(\d+)", directory.name,
                                      re.IGNORECASE)
        numbered_directory = (directory_match is not None
                              and (directory_match[1] or "").lower() == kind
                              and int(directory_match[2]) == number)
        if numbered_directory:
            directory = directory.parent / (kind + "{part}")
        file_token = _group_token(path.name)
        if file_token:
            file_match = re.fullmatch(r"(part|rec|clip)?(\d+)", file_token,
                                     re.IGNORECASE)
            if ((file_match[1] or "").lower(), int(file_match[2])) != (kind, number):
                return None
            name = re.sub(r"(_part|^(?:part|rec|clip))\d+",
                          lambda m: m[1].lower() + "{part}", path.name,
                          count=1, flags=re.IGNORECASE)
            if not kind:
                name = re.sub(r"^\d+(?=_)", "{part}", name, count=1)
        elif numbered_directory:
            name = path.name
        else:
            return None
        keys.append((role, str(directory / name)))
    return tuple(sorted(keys)), number


def _apply_single_imu_segment_context(parts: List[Dict], results: List[PartResult],
                              options: Optional[Dict] = None, *, recompute=True) -> None:
    """为 IMU 同步补齐相邻分段首/尾参考，保留原始分段及其他检查结果。

    前后分段编号、源文件系列、相机时间顺序及 IMU 边界连续性都必须匹配。
    取相机覆盖边界所需的完整邻片区间，不跨缺片、会话或缺样/时间倒退。
    GUI 与 CLI 在生成最终结果前共同调用。
    """
    opt = options or {}
    entries = collections.defaultdict(list)
    for part, result in zip(parts, results):
        result.imu_sync_context.clear()
        result.imu_sync = {role: local_sync(sr) for role, sr in result.imu_sync.items()}
        if result.imu_error:
            result.invalidate_imu_results()
            continue
        threshold_ms = (_adaptive_imu_sync_ms(result.imu_frequency_hz)
                        if opt.get('adaptive_thresholds', False)
                        else opt.get('imu_sync_ms', IMU_SYNC_THRESHOLD_US / US_PER_MS))
        threshold_us = int(threshold_ms * US_PER_MS)
        changed = {role: result.cameras[role] for role, sr in result.imu_sync.items()
                   if sr.threshold_us != threshold_us and role in result.cameras}
        if changed and not result.imu_frequency_error:
            # Explicitly changing a threshold invalidates the local baseline.
            result.imu_sync.update(check_imu_sync(changed, result.imu or [],
                threshold_us=threshold_us, source_rows=result.imu_row_by_timestamp))
        identity = _imu_segment_identity(part)
        if (identity is not None and not result.error and not result.imu_error
                and not result.imu_frequency_error and result.imu_order_valid
                and result.imu and result.cameras):
            entries[identity].append((part, result))

    # Cache only the current neighborhood, never every segment in the recording.
    # Sorting is a lookup view; original records (including duplicates) stay intact.
    ordered = collections.OrderedDict()

    def ordered_imu(result):
        key = id(result)
        if key not in ordered:
            if len(ordered) >= 3:
                ordered.popitem(last=False)
            ordered[key] = sorted(result.imu)
        ordered.move_to_end(key)
        return ordered[key]

    def consecutive(left: PartResult, right: PartResult) -> bool:
        if left.cameras.keys() != right.cameras.keys():
            return False
        if any(not left.cameras[k].timestamps or not right.cameras[k].timestamps
               or left.cameras[k].timestamps[-1] >= right.cameras[k].timestamps[0]
               for k in left.cameras):
            return False
        rates = [r.imu_frequency_hz for r in (left, right)]
        if any(rate is None or rate <= 0 for rate in rates) or rates[0] != rates[1]:
            return False
        limit = (1_000_000.0 / rates[0]) * opt.get(
            "imu_drop_mult", IMU_DROP_MULTIPLIER)
        return 0 < ordered_imu(right)[0] - ordered_imu(left)[-1] < limit

    for (series, number), current in entries.items():
        if opt.get('_read_checkpoint'):
            opt['_read_checkpoint']()
        if len(current) != 1:
            continue  # 编号不唯一时不猜测对应关系。
        _part, result = current[0]
        needed = pending_directions(result.imu_sync)
        if not needed:
            continue
        for direction, offset in (("previous", -1), ("next", 1)):
            if direction not in needed:
                continue
            neighbors = entries.get((series, number + offset), [])
            if len(neighbors) != 1:
                continue
            neighbor_part, neighbor = neighbors[0]
            left, right = (neighbor, result) if offset < 0 else (result, neighbor)
            if not consecutive(left, right):
                continue
            begin = min(c.timestamps[0] for c in result.cameras.values())
            end = max(c.timestamps[-1] for c in result.cameras.values())
            if offset < 0:
                # 上一段只能提供其尾部后缀；即便相机/IMU覆盖区间交叠，也必须延伸到末样本。
                lo = max(0, bisect.bisect_left(ordered_imu(neighbor), begin) - 1)
                hi = len(ordered_imu(neighbor))
            else:
                # 下一段只能提供其头部前缀；从第 0 个样本开始覆盖当前段尾边界。
                lo = 0
                hi = min(len(ordered_imu(neighbor)), bisect.bisect_right(ordered_imu(neighbor), end) + 1)
            samples = ordered_imu(neighbor)[lo:hi]
            # Validate every intervening sample, including the seam to this part.
            sample_chain = (chain(samples, ordered_imu(result)[:1]) if offset < 0
                            else chain(ordered_imu(result)[-1:], samples))
            rates = [r.imu_frequency_hz for r in (result, neighbor)]
            if any(rate is None or rate <= 0 for rate in rates) or rates[0] != rates[1]:
                continue
            period = 1_000_000.0 / rates[0]
            limit = period * opt.get("imu_drop_mult", IMU_DROP_MULTIPLIER)
            if not samples or any(not 0 <= b-a < limit for a, b in pairwise(sample_chain)):
                continue
            source = neighbor_part.get("mcap") or neighbor_part["imu"][0]
            result.imu_sync_context[direction] = ImuBoundarySample(
                neighbor.name, Path(source), ordered_imu(neighbor)[-1 if offset < 0 else 0],
                samples, [neighbor.imu_row_by_timestamp.get(t) for t in samples])
    ordered.clear()
    if not recompute:
        return
    for result in results:
        if opt.get('_read_checkpoint'):
            opt['_read_checkpoint']()
        if result.imu_sync and not result.imu_frequency_error:
            candidates = {direction: {"imu": list(zip(
                sample.timestamps_us, sample.row_indices))}
                for direction, sample in result.imu_sync_context.items()}
            result.imu_sync = {
                role: retry_edges(sr, candidates, opt.get("_read_checkpoint"), {'imu': 'IMU'})
                for role, sr in result.imu_sync.items()}
            for role, sr in result.imu_sync.items():
                result.checks[f"imu_sync_{role}"] = sr.passed
                result.over_frames_by_check[f"imu_sync_{role}"] = list(sr.abnormal)


def apply_imu_segment_context(parts: List[Dict], results: List[PartResult],
                              options: Optional[Dict] = None) -> None:
    """单设备与多设备共用严格的相邻分段连续性检查和完整边界覆盖。"""
    opt = options or {}
    single_parts, single_results = [], []
    devices = collections.defaultdict(list)
    for part, result in zip(parts, results):
        result.imu_sync_context.clear()
        if result.imu_error:
            result.invalidate_imu_results()
            continue
        if result.error:
            continue  # 保留已完成的独立检查，不用异常段补偿或重算无效相机。
        if not part.get("_multi_device"):
            single_parts.append(part)
            single_results.append(result)
            continue
        # 多设备数据没有明确相机归属时不猜测、不使用合并 IMU 池。
        if not result.camera_device_by_role:
            continue
        device_keys = _session_device_keys(part)
        for device, timestamps in result.imu_by_device.items():
            imu_files = [f for f in part.get("imu", [])
                         if device_keys.get(Path(f), _imu_device_name(Path(f))) == device]
            mcaps = [f for f in part.get("mcaps", [])
                     if device_keys.get(Path(f), _imu_device_name(Path(f))) == device]
            # 身份不唯一时不猜测对应关系；单个设备的时钟单独参与连续性检查。
            if len(mcaps) > 1 or (not mcaps and len(imu_files) != 1):
                continue
            projected = dict(part, _multi_device=False, imu=imu_files,
                             mcap=mcaps[0] if mcaps else None)
            device_cameras = ({
                role: camera for role, camera in result.cameras.items()
                if result.camera_device_by_role.get(role) == device
            } if result.camera_device_by_role else result.cameras)
            device_rate = result.imu_frequency_hz_by_device.get(device)
            proxy = PartResult(name=result.name, path=result.path,
                               cameras=device_cameras, imu=timestamps,
                               imu_sync={role: local_sync(sr) for role, sr in result.imu_sync.items()
                                         if role in device_cameras},
                               imu_frequency_hz=device_rate,
                               imu_frequency_source=result.imu_frequency_source_by_device.get(device, ""),
                               imu_frequency_error=result.imu_frequency_error_by_device.get(device, ""),
                               imu_row_by_timestamp=result.imu_row_by_timestamp_by_device.get(device, {}),
                               error=result.error, imu_error=result.imu_error,
                               imu_order_valid=result.imu_order_valid)
            devices[device].append((projected, proxy, result))
    _apply_single_imu_segment_context(single_parts, single_results, opt)
    for device, group in devices.items():
        _apply_single_imu_segment_context([p for p, _, _ in group],
            [proxy for _, proxy, _ in group], opt)
        for _, proxy, result in group:
            if proxy.imu_sync_context:
                result.imu_sync_context[device] = proxy.imu_sync_context
            result.imu_sync.update(proxy.imu_sync)
            for role, sr in proxy.imu_sync.items():
                result.checks[f"imu_sync_{role}"] = sr.passed
                result.over_frames_by_check[f"imu_sync_{role}"] = list(sr.abnormal)


def _cam_segment_identity(part: Dict) -> Optional[Tuple]:
    """相机同步跨段关联标识 (同目录系列 + 明确连续编号).

    返回 (系列键, 编号) 或 None (无法解析编号时不参与)。
    单设备与多设备会话 (双目+腕带) 均适用: 系列键 = part 的上级目录。
    """
    token = _part_token_of(part) or part.get("name", "")
    m = re.fullmatch(r"(part|rec|clip)?_?(\d+)", token, re.IGNORECASE)
    if not m:
        return None
    number = int(m.group(2))
    path = Path(part["path"])
    series_dir = path.parent if re.fullmatch(r"(part|rec|clip)_?\d+", path.name, re.IGNORECASE) else path
    series = str(series_dir.resolve())
    session_id = part.get("session_info", {}).get("id")
    if session_id:
        series = (series, session_id)
    return (series, number)


def apply_cam_segment_context(parts: List[Dict], results: List["PartResult"],
                              options: Optional[Dict] = None) -> None:
    """Only retry failed edge queries, preserving both camera directions."""
    opt = options or {}
    checkpoint = opt.get("_read_checkpoint")
    entries = collections.defaultdict(list)
    for part, result in zip(parts, results):
        result.cam_sync_context.clear()
        result.cam_sync = {role: local_sync(sr) for role, sr in result.cam_sync.items()}
        for group, sync in result.cam_sync_groups.items():
            result.cam_sync_groups[group] = {
                role: result.cam_sync.get(role, local_sync(sr)) for role, sr in sync.items()}
        if result.error or not result.cameras:
            continue
        identity = _cam_segment_identity(part)
        if identity is not None:
            entries[identity].append((part, result))

    for (series, number), current in entries.items():
        if checkpoint:
            checkpoint()
        if len(current) != 1:
            continue
        part, result = current[0]
        needed = pending_directions(result.cam_sync)
        candidates, samples = {}, {}
        for direction, offset in (("previous", -1), ("next", 1)):
            if direction not in needed:
                continue
            neighbors = entries.get((series, number + offset), [])
            if len(neighbors) != 1:
                continue
            npart, neighbor = neighbors[0]
            if result.cameras.keys() != neighbor.cameras.keys():
                continue
            extra, extra_rows, sources = {}, {}, {}
            for role, camera in neighbor.cameras.items():
                if not camera.timestamps or camera.error:
                    continue
                drop = neighbor.rgb_drop.get(role)
                if drop and drop.backstep_count:
                    continue
                queries = [query.timestamp for sr in result.cam_sync.values()
                           for query in sr.edge_queries
                           if query.direction == direction and role in query.targets]
                if not queries:
                    continue
                # Only the adjacent suffix/prefix needed by failed queries.
                # A long cut offset may require more than one reference frame.
                start = max(0, bisect.bisect_left(camera.timestamps, min(queries))-1) if offset < 0 else 0
                stop = len(camera.timestamps) if offset < 0 else min(
                    len(camera.timestamps), bisect.bisect_right(camera.timestamps, max(queries))+1)
                extra[role] = list(camera.timestamps[start:stop])
                extra_rows[role] = list(camera.rows[start:stop])
                sources[role] = Path(camera.file)
            candidates[direction] = {
                role: list(zip(timestamps, extra_rows[role]))
                for role, timestamps in extra.items()}
            samples[direction] = CamBoundarySample(
                direction=direction, neighbor_part=npart.get("name", ""),
                source=next(iter(sources.values()), None), sources=sources,
                extra_ts=extra, extra_rows=extra_rows)
        result.cam_sync = {
            role: retry_edges(sr, candidates, checkpoint,
                              {key: camera.label for key, camera in result.cameras.items()})
            for role, sr in result.cam_sync.items()}
        for role, sr in result.cam_sync.items():
            result.checks[f"cam_sync_{role}"] = sr.passed
            result.over_frames_by_check[f"cam_sync_{role}"] = list(sr.abnormal)
        for group, sync in result.cam_sync_groups.items():
            result.cam_sync_groups[group] = {role: result.cam_sync[role] for role in sync}
            result.checks[f"cam_sync_group_{group}"] = all(
                sr.passed for sr in result.cam_sync_groups[group].values())
        if any(sr.context_matches for sr in result.cam_sync.values()):
            result.cam_sync_context.update(samples)



def _boundary_note(result: PartResult, direction: str, target: str, reason: str) -> None:
    """记录因样本不足或数据异常导致的跨段失效，报告中单独展示."""
    text = f'{direction}段{target}：{reason}'
    if text not in result.boundary_truncation_errors:
        result.boundary_truncation_errors.append(text)
    notice = f'跨段补偿判 FAIL，{text}'
    if notice not in result.notices:
        result.notices.append(notice)


def _mark_sync_failed(result: PartResult, direction: str, target: str, reason: str,
                      kinds=('cam_sync', 'imu_sync')) -> None:
    """跨段补偿无法判定时把对应检查判为 FAIL，并保留异常详情."""
    failed = []
    for kind in kinds:
        source = getattr(result, kind, None) or {}
        for role, sync in source.items():
            result.checks[f'{kind}_{role}'] = False
            failed.append(role)
    _boundary_note(result, direction, target, reason)


def apply_streamed_triplet(prev, current, following, options=None):
    """处理 current 的两个跨段方向后即可释放 current.

    prev/current/following 均为 (part, result) 或 None。current 的 previous
    邻居是 prev、next 邻居是 following。同一 result 的两个方向必须一次性
    下发：retry_edges 以 local_sync 重建，分两次调用会互相覆盖。
    """
    from ego_checker.app.analyzer import BOUNDARY_CAM_SAMPLES, BOUNDARY_IMU_SAMPLES
    if current is None:
        return
    part, result = current
    if result.error:
        return
    opt = options or {}
    limit_cam = int(BOUNDARY_CAM_SAMPLES)
    limit_imu = int(BOUNDARY_IMU_SAMPLES)

    def camera_sample(item, role):
        return (item.boundary_samples or {}).get('cameras', {}).get(role)

    def imu_sample(item, device):
        data = item.boundary_samples or {}
        if device:
            return data.get('imu_by_device', {}).get(device)
        return data.get('imu')   # 单设备：无设备维度时使用合并 IMU 样本

    def adjacent(item, other, identity):
        """item 与 other 是否同系列且编号相差 1（任一方向）."""
        if item is None or other is None:
            return False
        left, right = identity(item[0]), identity(other[0])
        return (left is not None and right is not None and left[0] == right[0]
                and abs(left[1] - right[1]) == 1)

    # 相机：单设备与多设备均适用（与 apply_cam_segment_context 同一口径）
    cam_dirs = []
    if 'previous' in pending_directions(result.cam_sync) and adjacent(
            current, prev, _cam_segment_identity):
        cam_dirs.append(('previous', prev))
    if 'next' in pending_directions(result.cam_sync) and adjacent(
            current, following, _cam_segment_identity):
        cam_dirs.append(('next', following))
    if cam_dirs:
        _apply_camera_directions(result, cam_dirs, limit_cam, opt, camera_sample)

    # 前段与后段与 current 相邻的一侧同时处理：它们各自只有一个方向待补，
    # 与上面的中间段分开下发，不会互相覆盖。
    for neighbor, direction in ((prev, 'next'), (following, 'previous')):
        if neighbor is None:
            continue
        if (direction in pending_directions(neighbor[1].cam_sync)
                and adjacent(current, neighbor, _cam_segment_identity)):
            _apply_camera_directions(neighbor[1], [(direction, current)],
                                     limit_cam, opt, camera_sample)

    # IMU：单设备按源文件系列判定相邻；多设备按会话系列/编号判定相邻，
    # 设备隔离由逐设备候选与相机所属设备映射保证（与 apply_imu_segment_context 同口径）。
    def imu_identity(part):
        if part.get('_multi_device'):
            return _cam_segment_identity(part)
        return _imu_segment_identity(part)

    imu_dirs = []
    if 'previous' in pending_directions(result.imu_sync) and adjacent(
            current, prev, imu_identity):
        imu_dirs.append(('previous', prev))
    if 'next' in pending_directions(result.imu_sync) and adjacent(
            current, following, imu_identity):
        imu_dirs.append(('next', following))
    if imu_dirs:
        _apply_imu_directions(result, imu_dirs, limit_imu, opt, imu_sample)
    for neighbor, direction in ((prev, 'next'), (following, 'previous')):
        if (neighbor is not None
                and direction in pending_directions(neighbor[1].imu_sync)
                and adjacent(current, neighbor, imu_identity)):
            _apply_imu_directions(neighbor[1], [(direction, current)],
                                  limit_imu, opt, imu_sample)


def apply_streamed_segment_context(parts: List[Dict], results: List[PartResult],
                                   options: Optional[Dict] = None) -> None:
    """流式版本：只用 boundary_samples 的固定边界样本做相邻段补偿.

    严格固定样本数（相机 12 / IMU 1000）。所需区间超出已保留样本时该方向判 FAIL
    并记录异常详情，不外扩补齐、不猜测。同一分段的两个方向一次性下发。
    """
    from ego_checker.app.analyzer import BOUNDARY_CAM_SAMPLES, BOUNDARY_IMU_SAMPLES
    opt = options or {}
    limit_cam = int(BOUNDARY_CAM_SAMPLES)
    limit_imu = int(BOUNDARY_IMU_SAMPLES)
    entries = collections.defaultdict(list)
    for part, result in zip(parts, results):
        identity = _imu_segment_identity(part)
        if identity is not None and not result.error:
            entries[identity].append((part, result))
    if not entries:
        return

    def camera_sample(result, role):
        return (result.boundary_samples or {}).get('cameras', {}).get(role)

    def imu_sample(result, device):
        data = result.boundary_samples or {}
        if device:
            return data.get('imu_by_device', {}).get(device)
        return data.get('imu')

    for (series, number), current in entries.items():
        if opt.get('_read_checkpoint'):
            opt['_read_checkpoint']()
        if len(current) != 1:
            continue
        _part, result = current[0]
        cam_needed = pending_directions(result.cam_sync)
        imu_needed = pending_directions(result.imu_sync)
        if not (cam_needed or imu_needed):
            continue
        cam_dirs, imu_dirs = [], []
        for direction, offset in (("previous", -1), ("next", 1)):
            neighbors = entries.get((series, number + offset), [])
            if len(neighbors) != 1:
                continue
            npart, neighbor = neighbors[0]
            if direction in cam_needed:
                cam_dirs.append((direction, (npart, neighbor)))
            if direction in imu_needed:
                imu_dirs.append((direction, (npart, neighbor)))
        if cam_dirs:
            _apply_camera_directions(result, cam_dirs, limit_cam, opt, camera_sample)
        if imu_dirs:
            _apply_imu_directions(result, imu_dirs, limit_imu, opt, imu_sample)


def _collect_camera_direction(result, neighbor, npart, direction, offset,
                              limit_cam, opt, camera_sample, candidates, truncated,
                              boundary_sources=None):
    """收集单个方向可用的邻居候选；越界记入 truncated，不直接下发."""
    boundary_sources = boundary_sources if boundary_sources is not None else {}
    for role in result.cameras:
        if role not in neighbor.cameras:
            continue
        own = camera_sample(result, role)
        peer = camera_sample(neighbor, role)
        if not own or not peer or own.get('error') or peer.get('error'):
            continue
        # 相邻段必须连续：前段末条早于后段首条，与原有口径一致。
        left, right = (peer, own) if offset < 0 else (own, peer)
        if left.get('last') is None or right.get('first') is None:
            continue
        if left['last'] >= right['first']:
            continue
        # 以段内基线为准收集待补查询：已补偿结果的 edge_queries 属于基线，
        # 重复下发时不能把已解决的查询再次计入覆盖范围。
        queries = [query.timestamp for sync in result.cam_sync.values()
                   for query in local_sync(sync).edge_queries
                   if query.direction == direction and role in query.targets]
        if not queries:
            continue
        # 与 apply_cam_segment_context 同口径：查询点必须落在相邻段的保留样本区间内。
        # 超出即判 FAIL，不用更多样本补齐。
        available = peer.get('tail') if offset < 0 else peer.get('head')
        available_rows = peer.get('tail_rows') if offset < 0 else peer.get('head_rows')
        if not available:
            truncated.append(role)
            continue
        if offset < 0:
            covered = min(queries) >= available[0] or peer.get('count', 0) <= limit_cam
        else:
            covered = max(queries) <= available[-1] or peer.get('count', 0) <= limit_cam
        if not covered:
            truncated.append(role)
            continue
        rows_for = available_rows if available_rows and len(available_rows) == len(available) else [None] * len(available)
        candidates.setdefault(direction, {}).setdefault(role, []).extend(
            (ts, row) for ts, row in zip(available, rows_for))
        # 报告需展示跨段样本来源；流式释放前把实际使用的邻段区间固化下来。
        boundary_sources.setdefault(direction, {}).setdefault('extra', {})[role] = list(available)
        boundary_sources[direction].setdefault('rows', {})[role] = list(rows_for)
        boundary_sources[direction]['part'] = npart
        boundary_sources[direction].setdefault('sources', {})[role] = Path(peer.get('file'))


def _apply_camera_directions(result, directions, limit_cam, opt, camera_sample):
    """一次下发所有可用方向，避免单方向调用互相覆盖补偿结果.

    directions: [(direction, (part, result)), ...]
    """
    candidates = {}
    truncated = []
    boundary_sources = {}
    for direction, neighbor in directions:
        offset = -1 if direction == 'previous' else 1
        _collect_camera_direction(result, neighbor[1], neighbor[0], direction, offset,
                                  limit_cam, opt, camera_sample, candidates, truncated,
                                  boundary_sources)
    if truncated:
        _mark_sync_failed(result, '/'.join(sorted({d for d, _ in directions})),
                          '/'.join(sorted(set(truncated))),
                          f'超出固定保留样本（相机 {limit_cam} 条）', kinds=('cam_sync',))
        return
    if not candidates:
        return
    # 同一分段的两个方向可能先后经滑动窗口下发（先作为邻居补一个方向，
    # 之后作为中间段补另一个方向）。retry_edges 以段内基线重建，这里必须
    # 把先前方向已固化的候选一并带上，否则后一次下发会覆盖前一次的补偿。
    for direction, sample in result.cam_sync_context.items():
        if direction in candidates or not sample.extra_ts:
            continue
        candidates[direction] = {
            role: list(zip(ts, (sample.extra_rows or {}).get(role)
                           or [None] * len(ts)))
            for role, ts in sample.extra_ts.items()}
    result.cam_sync = {
        role: retry_edges(sync, candidates, opt.get('_read_checkpoint'),
                          {key: camera.label for key, camera in result.cameras.items()})
        for role, sync in result.cam_sync.items()}
    for role, sync in result.cam_sync.items():
        result.checks[f'cam_sync_{role}'] = sync.passed
        result.over_frames_by_check[f'cam_sync_{role}'] = list(sync.abnormal)
    for group, sync in result.cam_sync_groups.items():
        result.cam_sync_groups[group] = {role: result.cam_sync[role] for role in sync}
        result.checks[f'cam_sync_group_{group}'] = all(
            item.passed for item in result.cam_sync_groups[group].values())
    # 报告跨段样本来源：仅在实际发生跨段命中时展示，结构与非流式一致。
    if any(sync.context_matches for sync in result.cam_sync.values()):
        for direction, item in boundary_sources.items():
            extra = item.get('extra') or {}
            if not extra:
                continue
            npart = item.get('part')
            sources = item.get('sources') or {}
            result.cam_sync_context[direction] = CamBoundarySample(
                direction=direction,
                neighbor_part=(npart.get('name', '') if isinstance(npart, dict) else ''),
                source=next(iter(sources.values()), None), sources=sources,
                extra_ts={role: list(ts) for role, ts in extra.items()},
                extra_rows={role: list((item.get('rows') or {}).get(role) or []) for role in extra})


def _apply_imu_directions(result, directions, limit_imu, opt, imu_sample):
    """IMU 多方向一次下发，避免单方向调用互相覆盖补偿结果.

    候选按设备收集，但段内查询的目标角色固定为 'imu'（check_imu_sync 口径），
    下发时按相机所属设备取对应候选，保持多设备时钟隔离。
    """
    candidates = {}
    truncated = []
    span = (result.boundary_samples or {}).get('camera_span')
    if span is None:
        return
    for direction, neighbor in directions:
        offset = -1 if direction == 'previous' else 1
        _collect_imu_direction(result, neighbor[1], direction, offset,
                               limit_imu, opt, imu_sample, candidates, truncated)
    if truncated:
        _mark_sync_failed(result, '/'.join(sorted({d for d, _ in directions})),
                          '/'.join(sorted(set(truncated))) + ' IMU',
                          f'超出固定保留样本（IMU {limit_imu} 条）', kinds=('imu_sync',))
        return
    if not candidates:
        return
    # 与相机相同：先后下发的方向必须合并先前已固化的候选，避免互相覆盖。
    # 单设备上下文按方向索引，多设备按设备/方向索引。
    for key, sample in result.imu_sync_context.items():
        if isinstance(sample, ImuBoundarySample):
            if key not in candidates:
                candidates[key] = {'': list(zip(sample.timestamps_us,
                                                sample.row_indices))}
            continue
        for direction, item in sample.items():
            entries = candidates.setdefault(direction, {})
            if key not in entries:
                entries[key] = list(zip(item.timestamps_us, item.row_indices))
    device_of_role = result.camera_device_by_role or {}
    updated = {}
    for role, sync in result.imu_sync.items():
        device = device_of_role.get(role, '')
        role_candidates = {
            direction: {'imu': entries.get(device, [])}
            for direction, entries in candidates.items()}
        updated[role] = retry_edges(sync, role_candidates,
                                    opt.get('_read_checkpoint'), {'imu': 'IMU'})
    result.imu_sync = updated
    for role, sync in result.imu_sync.items():
        result.checks[f'imu_sync_{role}'] = sync.passed
        result.over_frames_by_check[f'imu_sync_{role}'] = list(sync.abnormal)
    # 记录跨段样本来源，与非流式口径一致：单设备按方向索引，多设备按设备/方向。
    for direction, neighbor in directions:
        entries = candidates.get(direction) or {}
        for device, records in entries.items():
            if not records:
                continue
            timestamps = [ts for ts, _row in records]
            rows = [row for _ts, row in records]
            sample = ImuBoundarySample(
                neighbor[0].get('name', '') if isinstance(neighbor[0], dict) else '',
                Path(str(neighbor[0].get('mcap') or (neighbor[0].get('imu') or [''])[0]))
                if isinstance(neighbor[0], dict) else None,
                timestamps[-1] if direction == 'previous' else timestamps[0],
                timestamps, rows)
            if device:
                result.imu_sync_context.setdefault(device, {})[direction] = sample
            else:
                result.imu_sync_context[direction] = sample


def _imu_devices(result):
    """IMU 分组键；单设备数据没有设备维度时用空键回退到合并 IMU."""
    devices = list(result.imu_by_device or ())
    return devices if devices else ['']


def _collect_imu_direction(result, neighbor, direction, offset,
                           limit_imu, opt, imu_sample, candidates, truncated):
    """收集单个方向的 IMU 候选；越界记入 truncated."""
    span = (result.boundary_samples or {}).get('camera_span')
    if span is None:
        return
    edge = span[0] if offset < 0 else span[1]
    for device in _imu_devices(result):
        own = imu_sample(result, device)
        peer = imu_sample(neighbor, device)
        if not own or not peer or not own.get('count') or not peer.get('count'):
            continue
        def rate_of(item):
            if device:
                return item.imu_frequency_hz_by_device.get(device)
            return item.imu_frequency_hz   # 单设备：使用合并 IMU 频率
        rates = [rate_of(result), rate_of(neighbor)]
        if any(rate is None or rate <= 0 for rate in rates) or rates[0] != rates[1]:
            continue
        limit = (1_000_000.0 / rates[0]) * opt.get('imu_drop_mult', IMU_DROP_MULTIPLIER)
        # 连续性 = 后段首样本 − 前段末样本；previous 方向时邻居在前。
        gap = (own['first'] - peer['last']) if offset < 0 else (peer['first'] - own['last'])
        if not 0 < gap < limit:
            continue
        available = peer.get('tail') if offset < 0 else peer.get('head')
        if not available:
            truncated.append(device)
            continue
        outside = edge < available[0] if offset < 0 else edge > available[-1]
        if outside and peer.get('count', 0) > limit_imu:
            truncated.append(device)
            continue
        # 与非流式口径一致：借用的边界窗口内部也必须连续，缺样不跨接补齐。
        if any(not 0 <= b - a < limit for a, b in pairwise(available)):
            continue
        available_rows = peer.get('tail_rows') if offset < 0 else peer.get('head_rows')
        rows_for = (available_rows if available_rows and len(available_rows) == len(available)
                    else [None] * len(available))
        candidates.setdefault(direction, {})[device] = list(zip(available, rows_for))
