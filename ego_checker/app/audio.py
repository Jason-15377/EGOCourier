"""app.audio 的实现。"""
from pathlib import Path
from typing import List
from ego_checker.analysis.audio import (_check_profile, _failure, _profile,
                                        check_target_duration, check_minimum_duration,
                                        check_payload_conservation)
from ego_checker.data.audio import McapAudioStreams, metadata_for, wav_info, read_csv_packets, open_mcap_messages, sha256_file, scan_wav_waveform
from ego_checker.domain.audio import AudioEvidence, CorruptAudio
from ego_checker.analysis.audio import check_continuity
from ego_checker.runtime.control import AnalysisStopped


def analyze_csv(path, device="", target_duration_s=None, drift_ppm=None,
                calculate_sha256=False, checkpoint=None):
    path = Path(path)
    e = AudioEvidence(str(path), device=device)
    try:
        if checkpoint:
            checkpoint()
        has_size = read_csv_packets(path, e, checkpoint=checkpoint)
        if not e.timestamps:
            raise CorruptAudio("空音频 CSV")
        e.non_increasing = sum(b <= a for a, b in zip(e.timestamps, e.timestamps[1:]))
        wav = path.with_name(path.name[:-8] + ".wav")  # *_pts.csv -> *.wav
        if calculate_sha256:
            e.csv_sha256 = sha256_file(path, checkpoint=checkpoint)
            e.media_sha256 = sha256_file(wav, checkpoint=checkpoint)
        wav_details = {}
        e.rate, e.channels, e.bits, e.data_bytes = wav_info(wav, details=wav_details)
        # WAV 实际采样帧数/时长独立于 CSV 字节守恒结论。即使后续发现
        # size_bytes 合计不一致，报告仍应展示从 WAV 原件得到的权威时长。
        align = e.channels * e.bits // 8
        e.sample_frames = e.data_bytes // align
        check_minimum_duration(e)
        e.waveform = scan_wav_waveform(wav, wav_details, checkpoint=checkpoint)
        e.waveform['source'] = str(wav)
        _check_profile(e, metadata_for(path))
        if e.timestamp_error:
            raise CorruptAudio(e.timestamp_error)
        if has_size:
            check_payload_conservation(e)
        check_continuity(e, verify_payload_continuity=has_size)
        check_target_duration(e, target_duration_s, drift_ppm)
        e.integrity = "PASS"
        if e.continuity == "FAIL" or e.target_duration_status == "FAIL":
            e.status = "FAIL"
        elif e.continuity == "SUSPECTED":
            e.status = "SUSPECTED"
        else:
            e.status = "PASS"
        if has_size:
            byte_note = ("PCM字节严格守恒" if not e.payload_byte_difference else
                         f"CSV/WAV载荷差{e.payload_byte_difference:+d}B，"
                         f"低于{e.payload_tolerance_frames}采样帧门槛")
            position_note = ""
            if e.continuity_method == "pcm_start_sample":
                if e.coverage_first_sample != 0:
                    position_note = f"；首包采样起点{e.coverage_first_sample}不为0"
                elif e.pcm_max_break_frames or e.pcm_total_break_frames:
                    within = (e.pcm_max_break_frames < e.pcm_single_break_limit_frames
                              and e.pcm_total_break_frames < e.pcm_total_break_limit_frames)
                    position_note = (
                        f"；采样位置差最大{e.pcm_max_break_frames}帧/"
                        f"累计{e.pcm_total_break_frames}帧，"
                        + (f"低于{e.pcm_single_break_limit_frames}/"
                           f"{e.pcm_total_break_limit_frames}帧门槛，按规则PASS"
                           if within else "达到判定门槛"))
            if e.continuity_method == "pcm_start_sample":
                e.reason = f"{byte_note}；按采样起点验证连续性{position_note}"
            else:
                e.reason = f"{byte_note}；按包大小与时间戳检查节拍"
        elif e.continuity == "FAIL":
            e.reason = "WAV 数据区完整；旧 CSV 存在时间戳倒退、重复或覆盖时长异常"
        elif e.continuity == "SUSPECTED":
            e.reason = ("WAV 数据区完整；旧 CSV 无 size_bytes，聚簇时间戳发现疑似异常"
                        "（非确定性结论）")
        else:
            e.reason = ("WAV 数据区完整；旧 CSV 无 size_bytes，聚簇时间戳节拍未见异常"
                        "（不能证明包完整性）")
        if e.target_duration_status == "FAIL":
            e.reason += "；实际音频时长超出目标时长及实测漂移容差"
        elif e.target_duration_status == "UNVERIFIED":
            e.reason += "；已配置目标时长但未提供实测漂移 ppm，目标时长未判定"
    except AnalysisStopped:
        raise
    except Exception as exc:
        _failure(e, exc)
    return e


def analyze_mcap(path, device="", target_duration_s=None, drift_ppm=None,
                 calculate_sha256=False, preloaded_streams=None, checkpoint=None):
    """Read file order and RawAudio payload, including legal no-summary MCAP files."""
    path = Path(path)
    streams = preloaded_streams if preloaded_streams is not None else McapAudioStreams(path, checkpoint=checkpoint)
    try:
        if checkpoint:
            checkpoint()
        media_sha256 = sha256_file(path, checkpoint=checkpoint) if calculate_sha256 else ""
        meta = metadata_for(path)
        if preloaded_streams is None:
            with open_mcap_messages(path, checkpoint=checkpoint) as messages:
                for schema, channel, message in messages:
                    streams.consume(schema, channel, message)
        for key, e in list(streams.items()):
            e.device = device
            e.media_sha256 = media_sha256
            if e.reason:
                continue
            try:
                _check_profile(e, meta)
                if key in streams.waveforms:
                    e.waveform = streams.waveforms[key].finish()
                    e.waveform['source'] = e.source
                check_continuity(e, verify_payload_continuity=True)
                check_minimum_duration(e)
                check_target_duration(e, target_duration_s, drift_ppm)
                if not e.timestamps:
                    raise CorruptAudio("音频通道无有效 PCM")
                e.integrity = "PASS"
                if e.continuity == "FAIL" or e.target_duration_status == "FAIL":
                    e.status = "FAIL"
                elif e.continuity == "SUSPECTED":
                    e.status = "SUSPECTED"
                else:
                    e.status = "PASS"
                e.reason = "RawAudio 载荷完整；按包大小与时间戳检查节拍"
                if e.target_duration_status == "FAIL":
                    e.reason += "；实际音频时长超出目标时长及实测漂移容差"
                elif e.target_duration_status == "UNVERIFIED":
                    e.reason += "；已配置目标时长但未提供实测漂移 ppm，目标时长未判定"
            except AnalysisStopped:
                raise
            except Exception as exc:
                # A failed profile/PCM validation must not leak its prefix or
                # prevent another fully decoded channel from being analyzed.
                failed = _failure(AudioEvidence(e.source, device=device), exc)
                failed.reason = f"数据读取异常：{e.source}: {failed.reason}"
                failed.media_sha256 = media_sha256
                streams[key] = failed
        if not streams:
            required = _profile(meta).get("enabled")
            is_wrist = "WRISTCAM-L" in device.upper() or "WRISTCAM-R" in device.upper()
            if is_wrist and required is not True:
                e = AudioEvidence(
                    str(path), status="N/A", integrity="N/A", continuity="N/A",
                    reason="腕带无音频流", device=device)
            elif required is True:
                e = AudioEvidence(
                    str(path), status="FAIL", integrity="FAIL", continuity="FAIL",
                    reason="缺失文件", device=device)
            elif required is False:
                e = AudioEvidence(
                    str(path), status="N/A", integrity="N/A", continuity="N/A",
                    reason="明确禁用音频", device=device)
            else:
                e = AudioEvidence(
                    str(path), reason="无音频消息；未证明音频已禁用", device=device)
            e.media_sha256 = media_sha256
            return [e]
    except AnalysisStopped:
        raise
    except Exception as exc:
        # No summary != corrupt. Actual parser/CRC/truncation errors stay visible.
        if type(exc).__name__ in ("CRCValidationError", "EndOfFile", "InvalidMagic") or isinstance(exc, EOFError):
            exc = CorruptAudio(str(exc))
        e = AudioEvidence(str(path), device=device)
        if calculate_sha256 and 'media_sha256' in locals():
            e.media_sha256 = media_sha256
        e = _failure(e, exc)
        e.reason = f"数据读取异常：{path}: {e.reason}"
        # Container/read failures invalidate every channel, including ones
        # collected before the failure. Never publish a partial file as valid.
        return [e]
    return list(streams.values())


def missing_audio(part, camera_roles=None):
    """Resolve expected stream from local metadata, never from lack of files alone."""
    e = AudioEvidence(str(part["path"]))
    try:
        roles = set(camera_roles or part.get("cameras", {}))
        wrist_only = bool(roles) and all(
            role.split("__", 1)[0] in ("wrist_left", "wrist_right")
            for role in roles
        )
        if wrist_only:
            e.status = "N/A"
            e.integrity = "N/A"
            e.continuity = "N/A"
            e.reason = "无音频文件"
            return e
        sources = [p for paths in part.get("cameras", {}).values() for p in paths]
        sources += part.get("imu", [])
        profiles = [_profile(metadata_for(source)) for source in sources]
        required = any(p.get("enabled") is True for p in profiles)
        disabled = bool(profiles) and all(p.get("enabled") is False for p in profiles)
        if required:
            e.status = "FAIL"
            e.integrity = "FAIL"
            e.continuity = "FAIL"
            e.reason = "缺失文件"
        elif disabled:
            e.status = "N/A"
            e.integrity = "N/A"
            e.continuity = "N/A"
            e.reason = "明确禁用音频"
        else:
            e.reason = "未发现音频数据；未证明音频已禁用"
    except Exception as exc:
        _failure(e, exc)
    return e

from ego_checker.domain.audio import CorruptAudio
from ego_checker.domain.audio import AudioContinuityEvent
from ego_checker.domain.audio import AudioEvidence
from ego_checker.analysis.audio import check_continuity
from ego_checker.data.audio import _exact
from ego_checker.data.audio import wav_info
from ego_checker.data.audio import metadata_for
from ego_checker.analysis.audio import _profile
from ego_checker.analysis.audio import _check_profile
from ego_checker.analysis.audio import _failure
from ego_checker.data.audio import _varint
from ego_checker.data.audio import _fields
from ego_checker.domain.audio import aggregate
from ego_checker.reporting.audio import _aggregate_status
from ego_checker.reporting.audio import _format_profile
from ego_checker.reporting.audio import integrity_summary
from ego_checker.reporting.audio import continuity_summary
from ego_checker.reporting.audio import format_duration
from ego_checker.reporting.audio import _duration_text
from ego_checker.reporting.audio import duration_summary
from ego_checker.reporting.audio import packet_integrity_summary
from ego_checker.reporting.audio import drop_summary
from ego_checker.reporting.audio import device_summary
from ego_checker.reporting.audio import summary

def extract_mcap_audio(mcap_path: Path) -> List[int]:
    """Legacy single-stream API: preserve file order and surface failed evidence."""
    from ego_checker.app.audio import analyze_mcap
    evidence = analyze_mcap(mcap_path)
    if any(e.status not in ("PASS", "N/A") for e in evidence):
        raise ValueError("; ".join(e.reason for e in evidence))
    streams = [e for e in evidence if e.timestamps]
    if len(streams) > 1:
        raise ValueError("多音频流必须按通道独立分析")
    return streams[0].timestamps if streams else []
