"""analysis.audio 的实现。"""
from ego_checker.domain.audio import AudioContinuityEvent, CorruptAudio


LEGACY_GAP_MULTIPLIER = 2.5
LEGACY_CLUSTER_SLACK_US = 20_000
LEGACY_SHORT_INTERVAL_MULTIPLIER = 0.4
LEGACY_DURATION_MIN_TOLERANCE_US = 1_000_000
LEGACY_DURATION_DRIFT_RATIO = 0.001
CLOCK_REANCHOR_JUMP_US = 20_000
MIN_AUDIO_DURATION_US = 59_998_000
PAYLOAD_DIFFERENCE_LIMIT_FRAMES = 144_000
PCM_SINGLE_BREAK_LIMIT_FRAMES = 24_000
PCM_TOTAL_BREAK_LIMIT_FRAMES = 144_000


def check_minimum_duration(evidence):
    """音频最短时长仅作诊断记录，不参与判定.

    手动录制等场景允许音频短于 59.998 秒；保留实测时长供报告展示，
    不再输出 PASS/FAIL，也不影响业务结论。
    """
    evidence.minimum_duration_us = MIN_AUDIO_DURATION_US
    evidence.minimum_duration_status = 'N/A'
    return evidence


def _legacy_clusters(deltas, nominal_us):
    """Group flush-level timestamp beats without treating each row as a packet."""
    clusters = []
    if nominal_us <= 0:
        return clusters
    accumulated = 0
    start = 0
    index = 0
    while index < len(deltas):
        delta = deltas[index]
        if (0 < delta < nominal_us * LEGACY_SHORT_INTERVAL_MULTIPLIER
                and index + 1 < len(deltas)):
            # The recorder can emit a very short beat followed by its compensating
            # long beat. Bind the pair before deciding whether there is a gap.
            accumulated += delta + deltas[index + 1]
            index += 2
        else:
            accumulated += delta
            index += 1
        if accumulated >= nominal_us:
            clusters.append((start, index - 1, accumulated))
            accumulated = 0
            start = index
    if accumulated > 0 and deltas:
        clusters.append((start, len(deltas) - 1, accumulated))
    return clusters


def _timestamp_quality(evidence, deltas, strict):
    """Classify device timestamp order and identify provable clock reanchors."""
    reanchors = set()
    if len(evidence.system_timestamps) == len(evidence.timestamps) and deltas:
        system_deltas = [b - a for a, b in zip(
            evidence.system_timestamps, evidence.system_timestamps[1:])]
        positive_system = sorted(delta for delta in system_deltas if delta > 0)
        system_nominal = (positive_system[len(positive_system) // 2]
                          if positive_system else 0)
        for index, (device_delta, system_delta) in enumerate(zip(deltas, system_deltas)):
            if device_delta >= 0 or system_delta <= 0 or not system_nominal:
                continue
            smooth_tolerance = max(20_000, system_nominal * 0.5)
            offset_before = (evidence.timestamps[index]
                             - evidence.system_timestamps[index])
            offset_after = (evidence.timestamps[index + 1]
                            - evidence.system_timestamps[index + 1])
            if (abs(system_delta - system_nominal) <= smooth_tolerance
                    and abs(offset_after - offset_before) > CLOCK_REANCHOR_JUMP_US):
                reanchors.add(index)
    evidence.clock_reanchor_count = len(reanchors)
    unexplained_backsteps = evidence.backsteps - len(reanchors)
    if strict and (evidence.duplicate_timestamps or unexplained_backsteps):
        evidence.timestamp_quality = "FAIL"
    elif evidence.non_increasing:
        evidence.timestamp_quality = "SUSPECTED"
    elif len(evidence.timestamps) >= 2:
        evidence.timestamp_quality = "PASS"
    else:
        evidence.timestamp_quality = "UNVERIFIED"
    return reanchors


def _check_pcm_positions(evidence, packet_frames):
    evidence.continuity_method = "pcm_start_sample"
    frame_differences = []
    for index in range(len(packet_frames) - 1):
        expected_sample = evidence.pcm_start_samples[index] + packet_frames[index]
        frame_delta = evidence.pcm_start_samples[index + 1] - expected_sample
        if not frame_delta:
            continue
        frame_differences.append(frame_delta)
        deviation_us = frame_delta * 1_000_000 / evidence.rate
        expected_us = packet_frames[index] * 1_000_000 / evidence.rate
        if frame_delta > 0:
            evidence.gap_count += 1
            evidence.lost_sample_frames += frame_delta
            evidence.max_gap_us = max(evidence.max_gap_us, round(deviation_us))
            anomaly = "采样位置缺口"
            affected = frame_delta
        else:
            evidence.overlap_count += 1
            evidence.overlap_sample_frames += -frame_delta
            anomaly = "采样位置重叠"
            affected = -frame_delta
        evidence.max_timing_error_us = max(
            evidence.max_timing_error_us, round(abs(deviation_us)))
        evidence.continuity_events.append(AudioContinuityEvent(
            index + 1,
            evidence.timestamps[index], evidence.timestamps[index + 1],
            packet_frames[index],
            round(evidence.timestamps[index] + expected_us),
            deviation_us, affected, anomaly,
        ))
    evidence.pcm_single_break_limit_frames = PCM_SINGLE_BREAK_LIMIT_FRAMES
    evidence.pcm_total_break_limit_frames = PCM_TOTAL_BREAK_LIMIT_FRAMES
    evidence.pcm_max_break_frames = max(
        (abs(value) for value in frame_differences), default=0)
    evidence.pcm_total_break_frames = sum(abs(value) for value in frame_differences)
    return (evidence.pcm_max_break_frames >= PCM_SINGLE_BREAK_LIMIT_FRAMES
            or evidence.pcm_total_break_frames >= PCM_TOTAL_BREAK_LIMIT_FRAMES)


def _check_sample_coverage(evidence, packet_frames):
    """Positions are relative to the current WAV, with an exclusive end position."""
    if not evidence.pcm_start_samples:
        return False
    evidence.pcm_single_break_limit_frames = PCM_SINGLE_BREAK_LIMIT_FRAMES
    evidence.pcm_total_break_limit_frames = PCM_TOTAL_BREAK_LIMIT_FRAMES
    evidence.coverage_first_sample = evidence.pcm_start_samples[0]
    evidence.coverage_end_sample = evidence.pcm_start_samples[-1] + packet_frames[-1]
    tail_delta = evidence.coverage_end_sample - evidence.sample_frames
    evidence.pcm_total_break_frames = max(
        evidence.pcm_total_break_frames, max(0, tail_delta))
    failed = (evidence.coverage_first_sample != 0
              or evidence.pcm_max_break_frames >= PCM_SINGLE_BREAK_LIMIT_FRAMES
              or evidence.pcm_total_break_frames >= PCM_TOTAL_BREAK_LIMIT_FRAMES)
    evidence.coverage_status = "FAIL" if failed else "PASS"
    return failed


def check_target_duration(evidence, target_duration_s=None, drift_ppm=None):
    """Apply a target verdict only when both target and measured drift are supplied."""
    if target_duration_s is None:
        if drift_ppm is not None:
            raise CorruptAudio("音频时钟漂移 ppm 必须与目标时长同时提供")
        return evidence
    target = float(target_duration_s)
    if target <= 0:
        raise CorruptAudio("音频目标时长必须大于 0")
    evidence.target_duration_s = target
    actual_us = evidence.sample_frames * 1_000_000 / evidence.rate
    evidence.target_duration_difference_us = actual_us - target * 1_000_000
    if drift_ppm is None:
        evidence.target_duration_status = "UNVERIFIED"
        return evidence
    drift = float(drift_ppm)
    if drift < 0:
        raise CorruptAudio("音频时钟漂移 ppm 不能为负数")
    evidence.target_duration_tolerance_us = max(1000.0, target * drift)
    evidence.target_duration_status = (
        "PASS" if abs(evidence.target_duration_difference_us)
        <= evidence.target_duration_tolerance_us else "FAIL"
    )
    return evidence


def check_payload_conservation(evidence):
    """Accept a bounded WAV/CSV byte difference while retaining the exact delta."""
    if not evidence.sizes:
        return evidence
    align = evidence.channels * evidence.bits // 8
    if not align or any(size <= 0 or size % align for size in evidence.sizes):
        raise CorruptAudio("PCM 载荷不是完整采样帧")
    evidence.payload_bytes = sum(evidence.sizes)
    evidence.payload_byte_difference = evidence.payload_bytes - evidence.data_bytes
    evidence.payload_tolerance_frames = PAYLOAD_DIFFERENCE_LIMIT_FRAMES
    tolerance_bytes = PAYLOAD_DIFFERENCE_LIMIT_FRAMES * align
    if abs(evidence.payload_byte_difference) < tolerance_bytes:
        evidence.payload_conservation = "PASS"
        return evidence
    evidence.payload_conservation = "FAIL"
    raise CorruptAudio(
        "CSV size_bytes 合计与 WAV 实际 PCM 字节数差异达到"
        f"{PAYLOAD_DIFFERENCE_LIMIT_FRAMES}采样帧门槛："
        f"CSV={evidence.payload_bytes}B，WAV={evidence.data_bytes}B，"
        f"差={evidence.payload_byte_difference:+d}B")


def check_continuity(evidence, verify_payload_continuity=False):
    deltas = [b - a for a, b in zip(evidence.timestamps, evidence.timestamps[1:])]
    evidence.duplicate_timestamps = sum(delta == 0 for delta in deltas)
    evidence.backsteps = sum(delta < 0 for delta in deltas)
    evidence.non_increasing = evidence.duplicate_timestamps + evidence.backsteps
    positive = [delta for delta in deltas if delta > 0]
    if positive:
        ordered = sorted(positive)
        middle = len(ordered) // 2
        evidence.interval_min_us = ordered[0]
        evidence.interval_max_us = ordered[-1]
        evidence.interval_mean_us = sum(ordered) / len(ordered)
        evidence.interval_median_us = (ordered[middle] if len(ordered) % 2 else
                                   (ordered[middle - 1] + ordered[middle]) / 2)
    if evidence.rate and evidence.channels and evidence.bits:
        align = evidence.channels * evidence.bits // 8
        if any(n <= 0 or n % align for n in evidence.sizes):
            raise CorruptAudio("PCM 载荷不是完整采样帧")
        evidence.sample_frames = evidence.data_bytes // align
        evidence.wav_duration_us = evidence.sample_frames * 1_000_000 / evidence.rate
    evidence.csv_span_us = (evidence.timestamps[-1] - evidence.timestamps[0]
                            if len(evidence.timestamps) >= 2 else 0)
    if verify_payload_continuity and evidence.sizes and len(evidence.sizes) == len(evidence.timestamps):
        align = evidence.channels * evidence.bits // 8
        packet_frames = [size // align for size in evidence.sizes]
        if len(evidence.timestamps) < 2:
            coverage_failed = _check_sample_coverage(evidence, packet_frames)
            evidence.continuity = "FAIL" if coverage_failed else "UNVERIFIED"
            return evidence
        reanchors = _timestamp_quality(evidence, deltas, strict=True)
        if evidence.pcm_start_samples:
            pcm_failed = _check_pcm_positions(evidence, packet_frames)
            coverage_failed = _check_sample_coverage(evidence, packet_frames)
            for index in sorted(reanchors):
                expected_us = packet_frames[index] * 1_000_000 / evidence.rate
                evidence.continuity_events.append(AudioContinuityEvent(
                    index + 1,
                    evidence.timestamps[index], evidence.timestamps[index + 1],
                    packet_frames[index],
                    round(evidence.timestamps[index] + expected_us),
                    evidence.timestamps[index + 1]
                    - (evidence.timestamps[index] + expected_us),
                    "采样位置连续", "时钟重锚（系统时钟连续）",
                ))
            for index, delta in enumerate(deltas):
                if index in reanchors or delta > 0:
                    continue
                expected_us = packet_frames[index] * 1_000_000 / evidence.rate
                anomaly = "时间戳重复" if delta == 0 else "时间戳倒退"
                evidence.continuity_events.append(AudioContinuityEvent(
                    index + 1,
                    evidence.timestamps[index], evidence.timestamps[index + 1],
                    packet_frames[index],
                    round(evidence.timestamps[index] + expected_us),
                    delta - expected_us,
                    "采样位置连续", anomaly,
                ))
            if pcm_failed or coverage_failed or evidence.timestamp_quality == "FAIL":
                evidence.continuity = "FAIL"
            elif evidence.timestamp_quality == "SUSPECTED":
                evidence.continuity = "SUSPECTED"
            else:
                evidence.continuity = "PASS"
            return evidence
        evidence.continuity_method = "timestamp_us+size_bytes"
        expected_us_by_packet = [
            frames * 1_000_000 / evidence.rate for frames in packet_frames[:-1]
        ]
        timing_errors = [
            delta - expected
            for delta, expected in zip(deltas, expected_us_by_packet)
        ]
        tolerances = [
            max(
                evidence.tolerance_frames * 1_000_000 / evidence.rate,
                min(5000.0, expected * 0.40),
            )
            for expected in expected_us_by_packet
        ]
        # 某些传输端会在一次调度中提交两个相邻音频包，使时间戳表现为
        # “极短间隔 + 补偿性长间隔”（或相反），但两包合计跨度仍正确。
        # 这只能说明提交时刻抖动，不能据此虚构 PCM 缺失/重叠。仅在两个
        # 实际间隔都为正、偏差异号且最多 3 个间隔的合计回到容差内时抵消；持续缺口、
        # 时间戳倒退及采样位置证据仍按确定性异常处理。
        compensated = set()
        index = 0
        while index < len(timing_errors):
            current = timing_errors[index]
            if deltas[index] <= 0 or abs(current) <= tolerances[index]:
                index += 1
                continue
            cumulative = current
            opposite = False
            matched_end = None
            for end in range(index + 1, min(len(timing_errors), index + 3)):
                if deltas[end] <= 0:
                    break
                following = timing_errors[end]
                cumulative += following
                opposite = opposite or current * following < 0
                if (opposite and abs(cumulative)
                        <= max(tolerances[index:end + 1])):
                    matched_end = end
                    break
            if matched_end is None:
                index += 1
                continue
            compensated.update(range(index, matched_end + 1))
            index = matched_end + 1
        for index, (delta_us, expected_us, timing_error_us, tolerance_us) in enumerate(
                zip(deltas, expected_us_by_packet, timing_errors, tolerances)):
            if index in reanchors:
                continue
            if index in compensated:
                continue
            # 设备时间戳包含提交/调度抖动。容差下限为 2 个采样帧，
            # 上限取包时长的 40%（严格小于半包），并封顶 5 ms；整包缺失
            # 仍会明显越界。
            tolerance_rounded = round(tolerance_us)
            if not evidence.timing_tolerance_min_us:
                evidence.timing_tolerance_min_us = tolerance_rounded
            else:
                evidence.timing_tolerance_min_us = min(
                    evidence.timing_tolerance_min_us, tolerance_rounded)
            evidence.timing_tolerance_max_us = max(
                evidence.timing_tolerance_max_us, tolerance_rounded)
            evidence.max_timing_error_us = max(
                evidence.max_timing_error_us, round(abs(timing_error_us)))
            frame_delta = round(timing_error_us * evidence.rate / 1_000_000)
            if timing_error_us > tolerance_us:
                evidence.gap_count += 1
                affected = max(1, frame_delta)
                evidence.lost_sample_frames += affected
                evidence.max_gap_us = max(
                    evidence.max_gap_us, round(timing_error_us))
                evidence.continuity_events.append(AudioContinuityEvent(
                    index + 1, evidence.timestamps[index], evidence.timestamps[index + 1],
                    packet_frames[index], round(evidence.timestamps[index] + expected_us),
                    timing_error_us, affected, "缺失"))
            elif timing_error_us < -tolerance_us:
                evidence.overlap_count += 1
                affected = max(1, -frame_delta)
                evidence.overlap_sample_frames += affected
                if delta_us < 0:
                    anomaly_type = "时间戳倒退/重叠"
                elif delta_us == 0:
                    anomaly_type = "时间戳重复/重叠"
                else:
                    anomaly_type = "重叠"
                evidence.continuity_events.append(AudioContinuityEvent(
                    index + 1, evidence.timestamps[index], evidence.timestamps[index + 1],
                    packet_frames[index], round(evidence.timestamps[index] + expected_us),
                    timing_error_us, affected, anomaly_type))
        if evidence.timestamp_quality == "FAIL":
            evidence.continuity = "FAIL"
        elif (evidence.gap_count or evidence.overlap_count
              or evidence.timestamp_quality == "SUSPECTED"):
            # 只有包大小和提交时间戳时，时间跳变不能证明 PCM 采样位置真的
            # 缺失或重叠；保留详细证据并标为疑似。pcm_start_sample 的
            # 确定性故障已在上面的独立分支判为 FAIL。
            evidence.continuity = "SUSPECTED"
        else:
            evidence.continuity = "PASS"
    else:
        # 单列旧 CSV 是 flush 级节拍，不可把每行假定为等长音频包。先合并
        # “短拍 + 紧随的大拍”，再以聚簇基线做宽松的疑似异常判断。
        nominal = evidence.interval_median_us
        _timestamp_quality(evidence, deltas, strict=False)
        evidence.short_interval_count = sum(
            0 < delta < nominal * LEGACY_SHORT_INTERVAL_MULTIPLIER
            for delta in deltas) if nominal > 0 else 0
        clusters = _legacy_clusters(deltas, nominal)
        evidence.cluster_count = len(clusters)
        for start, end, total_us in clusters:
            baseline_us = nominal * 2 if total_us > nominal * 1.5 else nominal
            deviation_us = total_us - baseline_us
            if deviation_us > (baseline_us * LEGACY_GAP_MULTIPLIER
                               + LEGACY_CLUSTER_SLACK_US):
                evidence.suspected_gap_count += 1
                lost = max(1, round(deviation_us / nominal))
                evidence.suspected_lost_packets += lost
                evidence.continuity_events.append(AudioContinuityEvent(
                    start + 1,
                    evidence.timestamps[start],
                    evidence.timestamps[end + 1],
                    None,
                    round(evidence.timestamps[start] + baseline_us),
                    deviation_us,
                    f"未知（疑似丢包{lost}）",
                    "疑似缺口",
                ))
        for index, delta in enumerate(deltas):
            if delta <= 0:
                kind = "时间戳重复" if delta == 0 else "时间戳倒退"
                evidence.continuity_events.append(AudioContinuityEvent(
                    index + 1, evidence.timestamps[index], evidence.timestamps[index + 1],
                    None, round(evidence.timestamps[index] + nominal), delta - nominal,
                    "未知", kind))
        if evidence.csv_span_us > 0 and evidence.wav_duration_us > 0:
            evidence.duration_difference_us = (
                evidence.wav_duration_us - evidence.csv_span_us)
            evidence.duration_tolerance_us = max(
                LEGACY_DURATION_MIN_TOLERANCE_US,
                evidence.csv_span_us * LEGACY_DURATION_DRIFT_RATIO,
            )
            evidence.duration_consistency = (
                "PASS" if abs(evidence.duration_difference_us)
                <= evidence.duration_tolerance_us else "FAIL"
            )
        if evidence.non_increasing or evidence.duration_consistency == "FAIL":
            evidence.continuity = "FAIL"
            if evidence.non_increasing:
                evidence.timestamp_quality = "FAIL"
        else:
            evidence.continuity = "SUSPECTED" if evidence.suspected_gap_count else "PASS"
    return evidence


def _profile(meta):
    audio = meta.get("streams", {}).get("audio", {})
    return audio if isinstance(audio, dict) else {}


def _check_profile(evidence, meta):
    audio = _profile(meta)
    if audio.get("enabled") is False:
        raise CorruptAudio("元数据禁用音频但存在音频载荷")
    for key, actual in (("sample_rate", evidence.rate), ("channels", evidence.channels),
                        ("bits_per_sample", evidence.bits)):
        expected = audio.get(key, audio.get("sample_rate_hz") if key == "sample_rate" else None)
        if expected is not None and int(expected) != actual:
            raise CorruptAudio("音频格式与元数据不符: " + key)


def _failure(e, exc):
    e.status = "FAIL" if isinstance(exc, CorruptAudio) else (
        "UNVERIFIED" if isinstance(exc, (FileNotFoundError, NotImplementedError)) else "ERROR")
    e.integrity = e.status
    if isinstance(exc, FileNotFoundError):
        e.reason = f"音频文件缺失：{exc.filename or str(exc)}"
    else:
        e.reason = f"{type(exc).__name__}: {exc}"
    if e.timestamp_error and e.timestamp_error not in e.reason:
        e.reason = e.timestamp_error + '；' + e.reason
    return e
