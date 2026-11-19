"""reporting.audio 的实现。"""
from decimal import Decimal, ROUND_HALF_UP, localcontext
from pathlib import Path


def _aggregate_status(values):
    for status in ("FAIL", "ERROR", "SUSPECTED", "UNVERIFIED"):
        if status in values:
            return status
    return "PASS" if "PASS" in values else "N/A"


def _format_profile(evidence):
    profiles = {(e.rate, e.channels, e.bits) for e in evidence
                if e.rate and e.channels and e.bits}
    if len(profiles) != 1:
        return "格式未知" if not profiles else "多种格式"
    rate, channels, bits = profiles.pop()
    rate_text = f"{rate // 1000}kHz" if rate % 1000 == 0 else f"{rate}Hz"
    return f"{rate_text}/{channels}ch/{bits}bit"


def integrity_summary(evidence):
    """报告用简洁完整性结论；不显示文件名或包名。"""
    if not evidence:
        return "未评价音频"
    status = _aggregate_status([e.integrity for e in evidence])
    packets = sum(e.retained_packets if e.retained_packets is not None
                  else len(e.timestamps) for e in evidence)
    frames = sum(e.sample_frames for e in evidence)
    metrics = f"{packets}包 {frames}帧 {_format_profile(evidence)}" if packets else ""
    reasons = "；".join(dict.fromkeys(e.reason for e in evidence
                                    if e.integrity != "PASS" and e.reason))
    return " ".join(part for part in (status, metrics, reasons) if part)


def continuity_summary(evidence):
    """报告用简洁连续性结论；新旧包按各自证据能力展示。"""
    if not evidence:
        return "未评价音频"
    active = [e for e in evidence if e.status != "N/A"]
    if not active:
        reasons = "；".join(dict.fromkeys(e.reason for e in evidence if e.reason))
        return " ".join(part for part in ("N/A", reasons or "明确禁用音频") if part)
    if any(e.status == "ERROR" for e in active):
        status = "ERROR"
    else:
        status = _aggregate_status([e.continuity for e in active])
    new = [e for e in active if _has_sizes(e)]
    old = [e for e in active if _has_timestamps(e) and not _has_sizes(e)]
    parts = [status]
    if new:
        methods = "/".join(dict.fromkeys(e.continuity_method for e in new))
        parts.append(
            f"方法{methods} "
            f"缺口{sum(e.gap_count for e in new)}次 "
            f"丢失{sum(e.lost_sample_frames for e in new)}帧 "
            f"最大{max((e.max_gap_us for e in new), default=0) / 1000:.3f}ms "
            f"重叠{sum(e.overlap_count for e in new)}次 "
            f"倒退{sum(e.backsteps for e in new)} "
            f"重复{sum(e.duplicate_timestamps for e in new)} "
            f"时钟重锚{sum(e.clock_reanchor_count for e in new)}")
    if old:
        # 间隔统计使用分析期已记录的 interval_* 字段，不依赖已被释放的逐包时间戳。
        stats = [e for e in old if e.interval_max_us]
        minimum = min((e.interval_min_us for e in stats), default=0)
        maximum = max((e.interval_max_us for e in stats), default=0)
        weights = [max(1, (e.retained_packets if e.retained_packets is not None
                           else len(e.timestamps)) - 1) for e in stats]
        mean = (sum(e.interval_mean_us * w for e, w in zip(stats, weights))
                / sum(weights) if stats else 0)
        median = (sum(e.interval_median_us * w for e, w in zip(stats, weights))
                  / sum(weights) if stats else 0)
        parts.append(
            f"时间戳间隔µs(min/mean/median/max)="
            f"{minimum}/{mean:.1f}/{median:.1f}/{maximum} "
            f"聚簇{sum(e.cluster_count for e in old)} "
            f"短间隔{sum(e.short_interval_count for e in old)} "
            f"疑似缺口{sum(e.suspected_gap_count for e in old)}次 "
            f"疑似丢包{sum(e.suspected_lost_packets for e in old)} "
            f"倒退{sum(e.backsteps for e in old)} "
            f"重复{sum(e.duplicate_timestamps for e in old)} "
            f"时长一致性{_aggregate_status([e.duration_consistency for e in old])}")
    if len(parts) == 1:
        reasons = "；".join(dict.fromkeys(e.reason for e in active if e.reason))
        parts.append(reasons or "无可用时间戳")
    return " ".join(parts)


def format_duration(seconds):
    """Format seconds as 秒, 分+秒, or 小时+分钟+秒."""
    value = seconds if isinstance(seconds, Decimal) else Decimal(str(seconds))
    centiseconds = int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    centiseconds = max(0, centiseconds)
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, second_units = divmod(remainder, 6_000)
    whole_seconds, fraction = divmod(second_units, 100)
    seconds_text = str(whole_seconds)
    if fraction:
        seconds_text += f".{fraction:02d}".rstrip("0")
    if hours:
        return f"{hours}小时{minutes}分钟{seconds_text}秒"
    if minutes:
        return f"{minutes}分{seconds_text}秒"
    return f"{seconds_text}秒"


def _duration_text(sample_frames, rate):
    if not rate:
        return "0秒"
    with localcontext() as context:
        context.prec = 30
        seconds = Decimal(sample_frames) / Decimal(rate)
    return format_duration(seconds)


def duration_summary(evidence):
    """Report WAV/PCM duration for every audio stream in one part."""
    if not evidence:
        return "未评价音频"
    active = [e for e in evidence if e.status != "N/A"]
    if not active:
        reasons = "；".join(dict.fromkeys(e.reason for e in evidence if e.reason))
        return " ".join(part for part in ("N/A", reasons or "无音频文件") if part)
    measured = [e for e in active if e.rate and e.sample_frames]
    if not measured:
        if all(e.status == "FAIL" and e.reason == "缺失文件" for e in active):
            return "FAIL 缺失文件"
        status = ("ERROR" if any(e.status == "ERROR" for e in active) else
                  "FAIL" if any(e.status == "FAIL" for e in active) else "UNVERIFIED")
        reasons = "；".join(dict.fromkeys(e.reason for e in active if e.reason))
        return " ".join(part for part in (status, reasons, "；时长未验证") if part)
    values = [_duration_text(e.sample_frames, e.rate) for e in measured]
    if len(values) == 1:
        result = values[0]
    else:
        result = "；".join(f"音频流{i}={value}" for i, value in enumerate(values, 1))
    timestamp_errors = list(dict.fromkeys(e.timestamp_error for e in measured if e.timestamp_error))
    if timestamp_errors:
        result += '；' + '；'.join(timestamp_errors)
    missing = len(active) - len(measured)
    if missing:
        missing_items = [e for e in active if not (e.rate and e.sample_frames)]
        missing_status = ("ERROR" if any(e.status == "ERROR" for e in missing_items) else
                          "FAIL" if any(e.status == "FAIL" for e in missing_items) else
                          "UNVERIFIED")
        missing_text = "；".join(dict.fromkeys(e.reason for e in missing_items if e.reason))
        missing_text = missing_text or "无有效音频时长"
        result += f"；{missing_status} {missing}路{missing_text}"
    targets = [e for e in measured if e.target_duration_status != "N/A"]
    if targets:
        target_parts = []
        for e in targets:
            target_text = format_duration(e.target_duration_s)
            diff_ms = e.target_duration_difference_us / 1000
            if e.target_duration_status == "UNVERIFIED":
                target_parts.append(f"目标{target_text} UNVERIFIED 缺少实测ppm")
            else:
                target_parts.append(
                    f"目标{target_text} {e.target_duration_status} "
                    f"差{diff_ms:+.3f}ms/容差{e.target_duration_tolerance_us / 1000:.3f}ms")
        result += "；" + "；".join(target_parts)
    return result


def diagnostic_summary(evidence):
    active = [e for e in evidence if e.status != 'N/A']
    if not active:
        return ''
    parts = []
    covered = [e for e in active if e.coverage_first_sample is not None]
    if covered:
        parts.append('首尾覆盖' + _aggregate_status([e.coverage_status for e in covered]))
        for e in covered:
            if e.coverage_status == 'FAIL':
                parts.append(f'起点{e.coverage_first_sample}/应为0，末包结束{e.coverage_end_sample}'
                             f'/实际{e.sample_frames}帧')
            elif (e.coverage_first_sample != 0
                  or e.coverage_end_sample != e.sample_frames):
                parts.append(
                    f'首尾差异门槛内PASS：起点{e.coverage_first_sample}，'
                    f'末包结束{e.coverage_end_sample}/实际{e.sample_frames}帧')
    if len(covered) < len(active):
        parts.append('无采样位置的流首尾覆盖UNVERIFIED')
    for e in active:
        wave = e.waveform
        if not wave:
            parts.append('波形未验证')
            continue
        state = ('未完成' if not wave['complete'] else
                 'SUSPECTED' if wave['zero_run_count'] or wave['invalid_samples'] else 'PASS')
        parts.append(f"波形{state} RMS={wave['rms']:.6g} 峰值={wave['peak']:.6g} "
                     f"全零帧{wave['zero_ratio']:.2%} "
                     f"最长全零{wave['max_zero_frames'] / e.rate:.6f}秒 "
                     f"长全零段{wave['zero_run_count']}处")
        if wave['zero_run_count']:
            parts.append('长静音/疑似补零，不能单凭波形确认丢包')
    return '；'.join(parts)


def diagnostic_rows(evidence):
    rows = []
    for e in evidence:
        if e.status == 'N/A':
            continue
        for marker in e.marker_rows:
            pcm_start = marker.get('pcm_start_sample')
            rows.append([
                e.source, 'CSV标记行', f"CSV物理行{marker['physical_line']}",
                f"kind={marker['kind']} timestamp_us={marker['timestamp_us']} "
                f"size_bytes={marker['size_bytes']} "
                f"pcm_start_sample={'-' if pcm_start is None else pcm_start}",
                '零载荷已校验；保留原始记录，仅从PCM数学计算中排除',
            ])
        if e.minimum_duration_us and e.rate:
            rows.append([e.source, '最短时长', f'{e.sample_frames}采样帧/{e.rate}Hz',
                         f'{e.sample_frames / e.rate:.9f}秒',
                         '仅记录实测时长，不设最短时长卡控'])
        if e.coverage_first_sample is not None:
            if e.source_rows:
                first_row, last_row = e.source_rows[0], e.source_rows[-1]
            else:
                first_row = e.retained_first_row if e.retained_first_row is not None else '未保存'
                last_row = e.retained_last_row if e.retained_last_row is not None else '未保存'
            rows.append([e.source, '首尾覆盖', f'CSV物理行{first_row} / {last_row}',
                         f'首包起点{e.coverage_first_sample}，末包结束{e.coverage_end_sample}',
                         f'起点须为0；单处<{e.pcm_single_break_limit_frames}帧且'
                         f'累计<{e.pcm_total_break_limit_frames}帧可PASS；'
                         f'实际末尾{e.sample_frames}（不含末端）；{e.coverage_status}'])
        else:
            rows.append([e.source, '首尾覆盖', '-', '-',
                         'UNVERIFIED：缺少相对当前音频的采样位置'])
        w = e.waveform
        if not w:
            rows.append([e.source, '波形扫描', '-', '-', '未验证'])
            continue
        source = w.get('source', e.source)
        rows.append([source, '波形扫描', f'采样帧[0,{w["frames"]})',
                     f'RMS={w["rms"]:.9g} 峰值={w["peak"]:.9g} 全零帧={w["zero_frames"]}',
                     '完整扫描' if w['complete'] else '未完成'])
        rows.append([source, '波形统计', '归一化幅值；多声道按采样帧计时',
                     f'最长全零{w["max_zero_frames"]}帧；最长恒值{w["max_constant_frames"]}帧；'
                     f'满幅采样值{w["clipped_samples"]}；非有限值{w["invalid_samples"]}',
                     '仅辅助诊断，不单独证明丢包'])
        for run in w['zero_runs']:
            start, end = run['start_frame'], run['end_frame']
            if w['data_offset'] is not None:
                location = (f'文件字节[{w["data_offset"] + start * w["frame_bytes"]},'
                            f'{w["data_offset"] + end * w["frame_bytes"]})')
            else:
                location = f'MCAP本通道消息{run["first_record"]}～{run["last_record"]}'
            rows.append([source, '长全零段', f'采样帧[{start},{end})；{location}',
                         f'{(end - start) / e.rate:.9f}秒',
                         '静音/疑似补零，不能确认丢包'])
        if w['zero_runs_omitted']:
            rows.append([source, '长全零段', '-', f'另有{w["zero_runs_omitted"]}段',
                         '详情保留前100段，统计覆盖全部数据'])
    return rows


def _has_timestamps(evidence) -> bool:
    """逐包列表可能已在流式分析后释放，此时按释放前记录的标记判断."""
    if evidence.had_timestamps is not None:
        return bool(evidence.had_timestamps)
    return bool(evidence.timestamps)


def _has_sizes(evidence) -> bool:
    if evidence.had_sizes is not None:
        return bool(evidence.had_sizes)
    return bool(evidence.sizes)


def packet_integrity_summary(evidence):
    """Summarize CSV packet payload conservation and packet-timeline integrity.

    timestamp_us + size_bytes can locate a suspected timeline gap, but without
    pcm_start_sample it cannot prove PCM-frame loss.
    """
    if not evidence:
        return "未评价音频"
    active = [e for e in evidence if e.status != "N/A"]
    if not active:
        reasons = "；".join(dict.fromkeys(e.reason for e in evidence if e.reason))
        return " ".join(part for part in ("N/A", reasons or "无音频文件") if part)
    if all(e.status == "FAIL" and e.reason == "缺失文件" for e in active):
        return "FAIL 缺失文件"

    statuses = []
    packets = sum(e.retained_packets if e.retained_packets is not None
                  else len(e.timestamps) for e in active)
    # 逐包列表可能已释放；优先使用分析期已算好的合计值。
    payload_bytes = sum(e.payload_bytes if e.payload_bytes is not None
                        else sum(e.sizes) for e in active if _has_sizes(e))
    pcm_bytes = sum(e.data_bytes for e in active if e.data_bytes)
    for e in active:
        if e.status == "ERROR":
            statuses.append("ERROR")
        elif e.integrity == "FAIL" or e.continuity == "FAIL":
            statuses.append("FAIL")
        elif not _has_timestamps(e) or not _has_sizes(e):
            statuses.append("UNVERIFIED")
        else:
            statuses.append("PASS")
    status = _aggregate_status(statuses)
    parts = [status]
    if packets:
        parts.append(f"{packets}包")
    sized = [e for e in active if _has_sizes(e)]
    if sized:
        if pcm_bytes:
            relation = "=" if payload_bytes == pcm_bytes else "≠"
            parts.append(f"CSV载荷{payload_bytes}B{relation}WAV/PCM {pcm_bytes}B")
        else:
            parts.append(f"载荷{payload_bytes}B")
        differences = [e for e in sized
                       if e.payload_byte_difference and e.payload_conservation == "PASS"]
        if differences:
            parts.append("载荷差门槛内PASS=" + "/".join(
                f"{e.payload_byte_difference:+d}B"
                f"（{abs(e.payload_byte_difference) // (e.channels * e.bits // 8)}帧）"
                f"<{e.payload_tolerance_frames}帧"
                for e in differences))
        gaps = sum(e.gap_count for e in sized)
        overlaps = sum(e.overlap_count for e in sized)
        suspected_timing = any(
            e.continuity == "SUSPECTED" and (e.gap_count or e.overlap_count)
            for e in sized)
        gap_label = "疑似时间线缺口" if suspected_timing else "时间线缺口"
        parts.append(f"{gap_label}{gaps}次")
        if overlaps:
            overlap_label = "疑似重叠" if suspected_timing else "重叠"
            parts.append(f"{overlap_label}{overlaps}次")
        positioned = [e for e in sized if e.continuity_method == "pcm_start_sample"]
        if positioned:
            parts.append("采样位置差=" + "/".join(
                f"最大{e.pcm_max_break_frames}帧、累计{e.pcm_total_break_frames}帧"
                f"（门槛{e.pcm_single_break_limit_frames}/{e.pcm_total_break_limit_frames}帧）"
                for e in positioned))
    else:
        parts.append("缺少size_bytes，包完整性未验证")
    csv_hashes = list(dict.fromkeys(e.csv_sha256 for e in active if e.csv_sha256))
    media_hashes = list(dict.fromkeys(e.media_sha256 for e in active if e.media_sha256))
    if csv_hashes:
        parts.append("SHA256(CSV)=" + "/".join(csv_hashes))
    if media_hashes:
        media_label = "MCAP" if all("#" in e.source for e in active if e.media_sha256) else "WAV"
        parts.append(f"SHA256({media_label})=" + "/".join(media_hashes))
    markers = [marker for e in active for marker in e.marker_rows]
    if markers and status in ("FAIL", "ERROR"):
        # 仅在包完整性异常时说明零载荷标记的处理口径，正常结果不展示细节。
        locations = "/".join(str(marker["physical_line"]) for marker in markers)
        parts.append(
            f"part_end标记{len(markers)}行（CSV物理行{locations}；"
            "零载荷已校验，保留原始顺序，仅排除PCM计算）")
    if status in ("FAIL", "ERROR", "UNVERIFIED"):
        reasons = "；".join(dict.fromkeys(
            e.reason for e in active
            if e.reason and (e.integrity != "PASS"
                             or not _has_timestamps(e) or not _has_sizes(e))))
        if reasons:
            parts.append(reasons)
    return " ".join(parts)


def drop_summary(evidence):
    """Report confirmed PCM sample-frame loss separately from suspected gaps."""
    if not evidence:
        return "未评价音频"
    active = [e for e in evidence if e.status != "N/A"]
    if not active:
        reasons = "；".join(dict.fromkeys(e.reason for e in evidence if e.reason))
        return " ".join(part for part in ("N/A", reasons or "无音频文件") if part)
    if all(e.status == "FAIL" and e.reason == "缺失文件" for e in active):
        return "FAIL 缺失文件"
    if any(e.status == "ERROR" for e in active):
        status = "ERROR"
    else:
        status = _aggregate_status([e.continuity for e in active])
    sized = [e for e in active if _has_sizes(e)]
    legacy = [e for e in active if _has_timestamps(e) and not _has_sizes(e)]
    parts = [status]
    if sized:
        lost_frames = sum(e.lost_sample_frames for e in sized)
        with localcontext() as context:
            context.prec = 30
            lost_us = sum((Decimal(e.lost_sample_frames) * Decimal(1_000_000)
                           / Decimal(e.rate) for e in sized if e.rate), Decimal(0))
        lost_ms = f"{lost_us / Decimal(1000):.6f}".rstrip("0").rstrip(".") or "0"
        suspected_timing = status == "SUSPECTED"
        gap_label = "疑似缺口" if suspected_timing else "缺口"
        loss_label = "估算缺失" if suspected_timing else "丢失"
        overlap_label = "疑似重叠" if suspected_timing else "重叠"
        parts.append(
            f"方法{'/'.join(dict.fromkeys(e.continuity_method for e in sized))} "
            f"{gap_label}{sum(e.gap_count for e in sized)}次 "
            f"{loss_label}{lost_frames}采样帧/{lost_ms}ms "
            f"{overlap_label}{sum(e.overlap_count for e in sized)}次 "
            f"倒退{sum(e.backsteps for e in sized)} "
            f"重复{sum(e.duplicate_timestamps for e in sized)} "
            f"时钟重锚{sum(e.clock_reanchor_count for e in sized)}")
    if legacy:
        parts.append(
            f"聚簇{sum(e.cluster_count for e in legacy)} "
            f"短间隔抑制{sum(e.short_interval_count for e in legacy)}处 "
            f"疑似缺口{sum(e.suspected_gap_count for e in legacy)}次 "
            f"疑似丢包{sum(e.suspected_lost_packets for e in legacy)}，"
            f"时长一致性{_aggregate_status([e.duration_consistency for e in legacy])}，"
            f"仅反映时间戳节拍和覆盖跨度，不证明包完整性")
    if status in ("ERROR", "UNVERIFIED") and sized:
        reasons = "；".join(dict.fromkeys(
            e.reason for e in sized if e.continuity != "PASS" and e.reason))
        if reasons:
            parts.append(reasons)
    if len(parts) == 1:
        reasons = "；".join(dict.fromkeys(e.reason for e in active if e.reason))
        parts.append(reasons or "未完成逐包连续性验证")
    return " ".join(parts)


def device_summary(evidence, formatter):
    """Keep each device's audio verdict independent in a shared report cell."""
    groups = {}
    for item in evidence:
        groups.setdefault(item.device or "未知设备", []).append(item)
    if len(groups) <= 1:
        return formatter(evidence)
    return "；".join(
        f"{device}:{formatter(items)}" for device, items in groups.items()
    )


def summary(evidence):
    def source_label(source):
        path, marker, channel = source.partition("#")
        return Path(path).name + marker + channel

    def interval_stats(e):
        if len(e.timestamps) < 2:
            return "间隔=-"
        return (f"间隔µs(min/mean/median/max)={e.interval_min_us}/"
                f"{e.interval_mean_us:.1f}/{e.interval_median_us:.1f}/{e.interval_max_us}")

    def continuity_stats(e):
        if e.sizes:
            tolerance = (str(e.timing_tolerance_min_us)
                         if e.timing_tolerance_min_us == e.timing_tolerance_max_us
                         else f"{e.timing_tolerance_min_us}~{e.timing_tolerance_max_us}")
            return (f"时间戳容差={tolerance}µs 最大时序偏差={e.max_timing_error_us}µs "
                    f"缺口={e.gap_count} 估算缺失采样帧={e.lost_sample_frames} "
                    f"最大缺口={e.max_gap_us / 1000:.3f}ms "
                    f"重叠={e.overlap_count} 估算重叠采样帧={e.overlap_sample_frames}")
        return (f"聚簇={e.cluster_count} 短间隔={e.short_interval_count} "
                f"疑似缺口={e.suspected_gap_count} "
                f"疑似丢包={e.suspected_lost_packets} "
                f"WAV/CSV时长一致性={e.duration_consistency} "
                f"时长差={e.duration_difference_us / 1000:.3f}ms")

    return "; ".join(f"{e.status} {source_label(e.source)}: {e.reason}; "
                     f"包={len(e.timestamps)} PCM采样帧={e.sample_frames} 字节={e.data_bytes} "
                     f"格式={e.rate}/{e.channels}/{e.bits} "
                     f"倒退={e.backsteps} 重复={e.duplicate_timestamps} "
                     f"时间戳质量={e.timestamp_quality} 时钟重锚={e.clock_reanchor_count} "
                     f"连续性方法={e.continuity_method} "
                     f"{interval_stats(e)} {continuity_stats(e)} "
                     f"连续性={e.continuity}" for e in evidence)
