"""独立的同源相邻分段连续性检查，不修改主分析结果或同步参考。"""
import collections
import csv
from dataclasses import dataclass, field
import math
from pathlib import Path
import re
import time

from ego_checker.data.camera_frequency import resolve_camera_frequency
from ego_checker.data.csv_readers import get_csv_spec
from ego_checker.data.imu_frequency import resolve_imu_frequency
from ego_checker.domain.identity import _group_token, _imu_device_name, _part_token_of, _session_device_keys


@dataclass
class Edge:
    count: int = 0
    first: int = 0
    last: int = 0
    first_row: int = 0
    last_row: int = 0
    sample: list = field(default_factory=list)
    first_location: str = ''
    last_location: str = ''
    sensor_label: str = ''
    frequency_hz: float = 0.0
    frequency_source: str = ''
    error: str = ''
    error_status: str = 'UNVERIFIED'

    def add(self, timestamp, row, *, collect_sample=False):
        if not self.count:
            self.first, self.first_row = timestamp, row
        self.last, self.last_row = timestamp, row
        self.count += 1
        if collect_sample and len(self.sample) < 200:
            self.sample.append(timestamp)


def _frozen_camera(result, path):
    """按源文件取已固化的相机首尾记录；缺失时返回 None 以便上游报未验证。"""
    for camera in result.cameras.values():
        if Path(camera.file) != path:
            continue
        sample = (result.boundary_samples or {}).get('cameras', {}).get(camera.role)
        if not sample or sample.get('error') or not sample.get('count'):
            return None
        return Edge(
            count=sample['count'], first=sample['first'], last=sample['last'],
            first_row=sample.get('first_row') or 0,
            last_row=sample.get('last_row') or 0)
    return None

@dataclass
class Source:
    part: str
    path: Path
    device: str
    kind: str
    series: tuple = ()
    number: int = -1
    streams: dict = field(default_factory=dict)
    status: str = ""
    reason: str = ""
    frequency_hz: float = 0.0
    frequency_source: str = ""
    loaded: bool = False
    role: str = ""


def _mcap_edges(result, source, audit, frequency_cache):
    edges = {}
    camera_roles = {entry['role'] for entry in audit.get('mcap_streams', [])
                    if entry['kind'] == '相机'}
    for entry in audit.get('mcap_streams', []):
        if entry['kind'] != source.kind:
            continue
        key = (entry['role'], entry['topic'], entry['schema'], entry['timestamp_basis'])
        if key in edges:
            edges[key].error = '同文件存在多个相同角色、topic和schema的通道，来源不唯一'
            edges[key].error_status = 'UNVERIFIED'
            continue
        def location(ordinal):
            return (f"channel={entry['channel_id']} topic={entry['topic']} "
                    f"schema={entry['schema'] or '未声明'} 通道消息序号={ordinal} "
                    f"时间戳来源={entry['timestamp_basis']}") if ordinal is not None else '-'
        edge = Edge(count=entry['count'], first=entry['first'], last=entry['last'],
                    first_row=None, last_row=None,
                    first_location=location(entry['first_message']),
                    last_location=location(entry['last_message']),
                    sensor_label=f"{entry['role'] or 'IMU'}：{entry['topic']} [{entry['schema']}]",
                    error=entry['error'], error_status='ERROR')
        edges[key] = edge
        if not edge.count and not edge.error:
            edge.error, edge.error_status = 'MCAP通道无有效时间戳', 'UNVERIFIED'
        if source.kind == '相机' and not edge.error:
            cameras = [c for c in result.cameras.values() if Path(c.file) == source.path]
            role = cameras[0].role if len(cameras) == 1 and len(camera_roles) == 1 else entry['role']
            frequency = resolve_camera_frequency(source.path, role, frequency_cache)
            edge.frequency_hz = frequency.rate_hz or 0.0
            edge.frequency_source = frequency.source
            if frequency.error:
                edge.error, edge.error_status = frequency.error, 'UNVERIFIED'
    return edges, '' if edges else f'MCAP未提供{source.kind}通道的首轮边界记录'


def _result_edges(result, source, frequency_cache=None):
    """O(number of streams): use the original endpoints from the first read.

    Whole-sequence order checks belong to the part's drop checks. A boundary
    deliberately describes just the last/first source records.
    """
    audit = result.source_read_audits.get(str(source.path), {})
    if audit.get('error'):
        return {}, audit['error']
    if not audit.get('signature'):
        return {}, '首轮读取未提供来源完整性记录，边界未验证'
    try:
        stat = source.path.stat()
    except OSError as exc:
        return {}, str(exc)
    if audit['signature'] != (stat.st_size, stat.st_mtime_ns):
        return {}, '文件在首轮读取后发生变化，不能复用首尾记录'
    if source.path.suffix.lower() == '.mcap':
        return _mcap_edges(result, source, audit, frequency_cache)
    if source.kind == '相机':
        streams = [("相机", c.timestamps, c.rows) for c in result.cameras.values()
                   if Path(c.file) == source.path and not c.error]
        if result.streamed:
            # 流式分析已释放原始时间戳：改用释放前固化的首尾记录。
            frozen = _frozen_camera(result, source.path)
            if frozen is None:
                return {}, '缺少完整且唯一的首尾来源记录'
            return {'相机': frozen}, ''
    else:
        if result.streamed:
            # 流式分析已释放 IMU 源序列：改用释放前固化的首尾记录。
            edges = {}
            for item in (result.boundary_samples or {}).get('imu_sources', []):
                if item['source'] != str(source.path):
                    continue
                sensor = item['sensor']
                if not sensor:
                    token = re.search(r'(?:^|_)(accel|gyro)(?:_|\.)', source.path.name, re.I)
                    sensor = token[1].lower() if token else 'IMU（源文件未标类型）'
                if sensor in edges:
                    return {}, '缺少完整且唯一的首尾来源记录'
                edges[sensor] = Edge(
                    count=item['count'], first=item['first'], last=item['last'],
                    first_row=item['first_row'], last_row=item['last_row'])
            return (edges, '') if edges else ({}, '缺少完整且唯一的首尾来源记录')
        streams = []
        for sequence in result.imu_drop_sources:
            if getattr(sequence, 'source', '') != str(source.path):
                continue
            sensor = getattr(sequence, 'sensor_type', '')
            if not sensor:
                token = re.search(r'(?:^|_)(accel|gyro)(?:_|\.)', source.path.name, re.I)
                sensor = token[1].lower() if token else 'IMU（源文件未标类型）'
            streams.append((sensor, sequence, getattr(sequence, 'source_rows', [])))
    edges = {}
    for sensor, timestamps, rows in streams:
        if not timestamps or len(timestamps) != len(rows) or sensor in edges:
            return {}, '缺少完整且唯一的首尾来源记录'
        edges[sensor] = Edge(count=len(timestamps), first=timestamps[0], last=timestamps[-1],
                             first_row=rows[0]+1, last_row=rows[-1]+1)
    return edges, '' if edges else '首轮读取未提供本来源记录'


def _identity(path, part):
    """Full source path with only the verified segment component replaced."""
    token = _part_token_of(part)
    match = re.fullmatch(r"(part|rec|clip)?(\d+)", token, re.I)
    if not match:
        return None
    kind, number = (match[1] or "").lower(), int(match[2])
    directory = path.resolve().parent
    directory_match = re.fullmatch(r"(part|rec|clip)_?(\d+)", directory.name, re.I)
    changed = False
    if directory_match:
        if (directory_match[1].lower(), int(directory_match[2])) != (kind, number):
            return None
        directory = directory.parent / (kind + "{part}")
        changed = True
    name = path.name
    file_token = _group_token(name)
    if file_token:
        fm = re.fullmatch(r"(part|rec|clip)?(\d+)", file_token, re.I)
        if not fm or ((fm[1] or "").lower(), int(fm[2])) != (kind, number):
            return None
        name = re.sub(r"(_part|^(?:part|rec|clip))\d+", r"\1{part}", name, count=1, flags=re.I)
        if not kind:
            name = re.sub(r"^\d+(?=_)", "{part}", name, count=1)
        changed = True
    elif directory_match:
        # 文件名末尾的 _<编号> 不是标准 part 标记（如 ..._092829_0001.mcap），
        # 但所在目录已是编号一致的 part 目录：归一化该尾缀，使相邻段同源配对。
        stem, suffix = Path(name).stem, Path(name).suffix
        trailing = re.search(r"_(\d+)$", stem)
        if trailing and int(trailing[1]) == number:
            name = stem[:trailing.start()] + "_{part}" + suffix
            changed = True
    return (str(directory / name), number) if changed else None


def _read_edges(path, kind, checkpoint=None):
    """Retry complete files; a failed read never returns its collected endpoints."""
    failures = []
    for attempt in range(1, 4):
        if checkpoint:
            checkpoint()
        try:
            before = path.stat()
            spec = get_csv_spec(path)
            if not spec.ok or not spec.has_header:
                raise ValueError(spec.reason or "无表头，无法确认来源行和时间戳单位")
            streams = {}
            with path.open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.reader(stream, delimiter=spec.delim, strict=True)
                header = [v.strip() for v in next(reader)]
                if len(header) != len(set(header)):
                    raise ValueError("表头列名重复")
                column = header.index("device_timestamp_us") if "device_timestamp_us" in header else (
                    header.index("timestamp_us") if "timestamp_us" in header else spec.col)
                scale = 1.0 if header[column] in ("timestamp_us", "device_timestamp_us") else spec.scale
                next_line = reader.line_num + 1
                for values in reader:
                    line = next_line
                    next_line = reader.line_num + 1
                    if checkpoint and line % 4096 == 0:
                        checkpoint()
                    if len(values) != len(header):
                        raise ValueError(f"CSV第{line}物理行列数不符")
                    row = dict(zip(header, values))
                    if row.get("presentation_time_us", "").strip() and float(row["presentation_time_us"]) < 0:
                        continue
                    if path.name.lower().endswith("_frames.csv") and "FAILED" in row.get("drop_reason", "").upper():
                        continue
                    value = values[column].strip()
                    timestamp = int(value) if scale == 1 and re.fullmatch(r"\d+", value) else int(float(value)*scale)
                    if timestamp < 0:
                        raise ValueError(f"CSV第{line}物理行时间戳为负")
                    sensor = "相机"
                    if kind == "IMU":
                        sensor = row.get("type", "").strip()
                        if "type" in row and not sensor:
                            raise ValueError(f"CSV第{line}物理行IMU类型为空")
                        if not sensor:
                            token = re.search(r"(?:^|_)(accel|gyro)(?:_|\.)", path.name, re.I)
                            sensor = token[1].lower() if token else "IMU（源文件未标类型）"
                    streams.setdefault(sensor, Edge()).add(timestamp, line-1, collect_sample=kind == 'IMU')
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("文件在读取期间发生变化")
            if not streams:
                raise ValueError("无可分析记录（空文件或全部被过滤）")
            return streams, ""
        except (OSError, UnicodeError, ValueError, OverflowError, csv.Error, StopIteration) as exc:
            failures.append(f"第{attempt}次：{exc}")
            if attempt < 3:
                time.sleep(.05)
    return {}, "完整读取失败（已尝试3次）：" + "；".join(failures)


def _row(left, right=None, sensor="-", status="UNVERIFIED", reason=""):
    a = left.streams.get(sensor)
    b = right.streams.get(sensor) if right else None
    return {"设备": left.device, "数据类别": left.kind,
            "传感器类型": (a.sensor_label if a else b.sensor_label if b else '') or sensor,
            "前分段": left.part, "后分段": right.part if right else "-",
            "前末数据行": a.last_row if a and a.last_row is not None else "-",
            "后首数据行": b.first_row if b and b.first_row is not None else "-",
            "前末时间戳us": a.last if a and a.last is not None else "-",
            "后首时间戳us": b.first if b and b.first is not None else "-",
            "边界间隔ms": "-", "理论周期ms": "-", "丢帧间隔阈值ms": "-",
            "估算缺失数": "-", "结论": status, "说明": reason,
            "前源记录": a.last_location if a and a.last_location else '-',
            "后源记录": b.first_location if b and b.first_location else '-',
            "前源文件": str(left.path), "后源文件": str(right.path) if right else "-"}


def analyze_boundaries(parts, results, options=None, *, checkpoint=None, run_notice="",
                       progress=None):
    """每个同源相邻边界只输出一次。读取与主报告隔离，MCAP不猜造CSV行号。"""
    options = options or {}
    matched = collections.defaultdict(list)
    for result in results or []:
        matched[(result.name, str(Path(result.path).resolve()))].append(result)
    # 单设备模式下同一目录可产出多个同名分段（各设备一个 MCAP），仅凭名称与路径
    # 无法区分。此时按 parts/results 的同索引关系配对：两者由同一分析循环按序产生。
    paired = list(results or [])
    by_index = paired if len(paired) == len(parts) else None
    groups = collections.defaultdict(lambda: collections.defaultdict(list))
    rows = []
    frequency_cache = {}
    for position, part in enumerate(parts):
        candidates = matched.get((part["name"], str(Path(part["path"]).resolve())), [])
        if len(candidates) == 1:
            result = candidates[0]
        elif by_index is not None and position < len(by_index):
            # 同索引结果必须确属该分段（名称与路径一致），否则不配对。
            same = by_index[position]
            result = (same if (same.name == part["name"]
                               and str(Path(same.path).resolve())
                               == str(Path(part["path"]).resolve())) else None)
        else:
            result = None
        devices = _session_device_keys(part)
        entries = part.get("camera_entries") or list(part.get("cameras", {}).items())
        roles = {Path(p): role for role, paths in entries for p in paths}
        sources = [("相机", Path(p)) for _, paths in entries for p in paths]
        sources += [("IMU", Path(p)) for p in part.get("imu", [])]
        # Several role entries may reference one container. Its per-channel
        # snapshot already preserves all records; visit that container once.
        seen_containers = set()
        unique_sources = []
        for item in sources:
            if item[1].suffix.lower() == '.mcap':
                if item in seen_containers:
                    continue
                seen_containers.add(item)
            unique_sources.append(item)
        sources = unique_sources
        mcaps = part.get("mcaps") or ([part["mcap"]] if part.get("mcap") else [])
        for p in mcaps:
            for kind in ('相机', 'IMU'):
                if (kind, Path(p)) not in sources:
                    sources.append((kind, Path(p)))
        for kind, path in sources:
            if checkpoint and not run_notice:
                checkpoint()
            source = Source(part["name"], path, devices.get(path, _imu_device_name(path)), kind)
            source.role = roles.get(path, '')
            identity = _identity(path, part)
            if identity is None:
                continue  # 无可确认的同源分段，不启动边界读取。
            if run_notice:
                source.status, source.reason = "UNVERIFIED", run_notice + "；未执行独立边界读取"
            elif results is not None and result is None:
                source.status, source.reason = "UNVERIFIED", "缺少唯一的本次分段分析结果"
            elif result is not None and (result.error or (kind == "IMU" and result.imu_error)):
                source.status, source.reason = "ERROR", result.error or result.imu_error
            elif kind == "IMU" and result is not None:
                if result.multi_device or result.imu_frequency_hz_by_device:
                    source.frequency_hz = result.imu_frequency_hz_by_device.get(source.device, 0.0)
                    source.frequency_source = result.imu_frequency_source_by_device.get(source.device, "")
                    rate_error = result.imu_frequency_error_by_device.get(source.device, "")
                else:
                    source.frequency_hz = result.imu_frequency_hz or 0.0
                    source.frequency_source = result.imu_frequency_source
                    rate_error = result.imu_frequency_error
                if rate_error:
                    source.status, source.reason = "ERROR", rate_error
            if path.suffix.lower() == '.mcap' and not source.status:
                audit = result.source_read_audits.get(str(path), {}) if result is not None else {}
                if 'mcap_streams' not in audit or not result.checks_completed:
                    source.status, source.reason = 'UNVERIFIED', 'MCAP缺少本次完整提取的通道边界记录；不重复扫描或猜造CSV行号'
            elif path.suffix.lower() != '.csv' and not source.status:
                source.status, source.reason = 'UNVERIFIED', '该来源格式尚不支持边界核验'
            if result is not None and result.checks_completed and not source.status:
                source.loaded = True
                source.streams, error = _result_edges(result, source, frequency_cache)
                if error:
                    source.status, source.reason = 'ERROR', error
                    if path.suffix.lower() == '.mcap' and not any(
                            entry['kind'] == kind for entry in audit.get('mcap_streams', [])):
                        source.status = 'UNVERIFIED'
            series, source.number = identity
            source.series = (series, source.device, kind)
            groups[source.series][source.number].append(source)

    # Count source slots and adjacent pairs, including coverage/error checks.
    # Each slot advances only after the preceding work has returned.
    total = sum(2 * len(by_number) - 1 for by_number in groups.values() if len(by_number) > 1)
    completed = 0

    def advance(detail):
        nonlocal completed
        if progress:
            progress(detail, completed, total)
        if checkpoint and not run_notice:
            checkpoint()
        completed += 1

    for by_number in groups.values():
        numbers = sorted(by_number)
        if len(numbers) == 1:
            continue
        # 只读取确实参与连续编号且来源唯一的边界，孤立/缺段记录不触发CSV读取。
        for number in numbers:
            candidates = by_number[number]
            advance(f"读取/检查 {candidates[0].kind} 来源 {candidates[0].path}")
            if len(candidates) != 1 or not any(len(by_number.get(n, [])) == 1 for n in (number-1, number+1)):
                continue
            source = candidates[0]
            if not source.status:
                error = ''
                if not source.loaded:
                    # Explicit standalone report / legacy caller without a first read.
                    source.streams, error = _read_edges(source.path, source.kind, checkpoint)
                if not error and source.kind == '相机' and source.path.suffix.lower() != '.mcap':
                    frequency = resolve_camera_frequency(source.path, source.role, frequency_cache)
                    source.frequency_hz = frequency.rate_hz or 0.0
                    source.frequency_source = frequency.source
                    if frequency.error:
                        source.status, source.reason = 'UNVERIFIED', frequency.error
                if not error and source.kind == "IMU" and not source.frequency_hz:
                    class TimestampSequences(list):
                        pass
                    timestamps = TimestampSequences()
                    timestamps.source_sequences = [
                        edge.sample for edge in source.streams.values()]
                    frequency = resolve_imu_frequency(source.path, timestamps)
                    if frequency.error:
                        source.status, source.reason = "ERROR", frequency.error
                    else:
                        source.frequency_hz = frequency.rate_hz
                        source.frequency_source = frequency.source
                if error:
                    source.status, source.reason = "ERROR", error
        for previous, following in zip(numbers, numbers[1:]):
            aa, bb = by_number[previous], by_number[following]
            advance(f"核验 {aa[0].device} {aa[0].kind}：{aa[0].part} → {bb[0].part}")
            if len(aa) != 1 or len(bb) != 1:
                for source in aa + bb:
                    rows.append(_row(source, reason="同源分段编号不唯一，未选择边界配对"))
                continue
            a, b = aa[0], bb[0]
            sensors = sorted(a.streams.keys() | b.streams.keys()) or ["-"]
            for sensor in sensors:
                row = _row(a, b, sensor)
                rows.append(row)
                if a.status or b.status:
                    row["结论"] = "ERROR" if "ERROR" in (a.status, b.status) else "UNVERIFIED"
                    row["说明"] = "；".join(s.reason for s in (a, b) if s.reason)
                    continue
                if following != previous+1:
                    row["说明"] = "分段编号不连续，不跨缺段计算丢帧或同步"
                    continue
                x, y = a.streams.get(sensor), b.streams.get(sensor)
                if not x or not y:
                    row["说明"] = "一侧缺少同类型传感器记录"
                    continue
                if x.error or y.error:
                    row['结论'] = 'ERROR' if any(e.error and e.error_status == 'ERROR' for e in (x, y)) else 'UNVERIFIED'
                    row['说明'] = '；'.join(e.error for e in (x, y) if e.error)
                    continue
                delta = y.first-x.last
                row["边界间隔ms"] = delta/1000
                # 理论周期只取决于本传感器频率，与边界间隔正负无关；
                # 先算出周期，重叠/重复的边界也应给出周期供核对。
                if a.kind == "IMU":
                    if (a.frequency_hz <= 0 or b.frequency_hz <= 0
                            or a.frequency_hz != b.frequency_hz):
                        row["说明"] = "两段录制配置的IMU频率缺失或不同，无法确认统一丢帧阈值"
                        if delta <= 0:
                            row["结论"] = "REVIEW"
                            row["说明"] = ("时间范围重叠或边界时间戳重复；可能涉及预滚或时钟异常，"
                                           "需结合采集契约复核；不折算丢帧")
                        continue
                    period = 1_000_000.0 / a.frequency_hz
                    rate_detail = (f"IMU频率={a.frequency_hz:g}Hz；来源："
                                   f"{a.frequency_source} / {b.frequency_source}")
                else:
                    source_rate = x.frequency_hz or a.frequency_hz
                    next_rate = y.frequency_hz or b.frequency_hz
                    if source_rate <= 0 or source_rate != next_rate:
                        row["说明"] = "两段相机配置帧率缺失或不同，无法确认统一丢帧阈值"
                        if delta <= 0:
                            row["结论"] = "REVIEW"
                            row["说明"] = ("时间范围重叠或边界时间戳重复；可能涉及预滚或时钟异常，"
                                           "需结合采集契约复核；不折算丢帧")
                        continue
                    period = 1_000_000.0 / source_rate
                    rate_detail = (f"相机配置帧率={source_rate:g}Hz；来源："
                                   f"{x.frequency_source or a.frequency_source} / {y.frequency_source or b.frequency_source}")
                multiplier = float(options.get("imu_drop_mult" if a.kind == "IMU" else "rgb_drop_mult", 1.5))
                if not math.isfinite(multiplier) or multiplier <= 1:
                    row["结论"], row["说明"] = "ERROR", "丢帧周期倍数必须为大于1的有限数"
                    continue
                threshold = period*multiplier
                row["理论周期ms"], row["丢帧间隔阈值ms"] = period/1000, threshold/1000
                if delta <= 0:
                    row["结论"] = "REVIEW"
                    row["说明"] = ("时间范围重叠或边界时间戳重复；可能涉及预滚或时钟异常，"
                                   "需结合采集契约复核；不折算丢帧；" + rate_detail)
                    continue
                row["估算缺失数"] = max(0, round(delta/period)-1) if delta >= threshold else 0
                row["结论"] = "FAIL" if delta >= threshold else "PASS"
                row["说明"] = "按源文件顺序取前段末条和后段首条；" + rate_detail
    if progress:
        progress("边界核验完成" if total else "无可核验的跨段边界", completed, total)
    return rows
