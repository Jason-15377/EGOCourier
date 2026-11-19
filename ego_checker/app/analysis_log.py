"""Analysis progress and execution errors, without per-record data diagnostics."""
from contextlib import contextmanager
from pathlib import Path
from time import perf_counter


def event(log, label, **values):
    if log is not None:
        log(f'[{label}] ' + ' | '.join(f'{key}={value}' for key,value in values.items()))


@contextmanager
def stage(log, name):
    started = perf_counter()
    if log:
        log(f'[阶段开始] {name}')
    try:
        yield
    except BaseException:
        if log:
            log(f'[阶段异常] {name}，耗时 {perf_counter()-started:.3f} 秒')
        raise
    else:
        if log:
            log(f'[阶段完成] {name}，耗时 {perf_counter()-started:.3f} 秒')


def input_plan(log, part):
    event(log, '分段输入', part=part['name'], path=part['path'])
    if log is None:
        return
    def sources(value, kind):
        if isinstance(value, (str, Path)):
            event(log, '输入文件', part=part['name'], kind=kind, source=value)
        elif isinstance(value, dict):
            for key, entry in value.items():
                sources(entry, kind+'/'+str(key))
        elif isinstance(value, (list, tuple)):
            for entry in value:
                sources(entry, kind)
    for key in ('cameras', 'imu', 'mcap', 'mcaps', 'extra'):
        sources(part.get(key), key)


def part_progress(log, result):
    """Record completion and input/processing errors, not PASS/FAIL data evidence."""
    r = result
    event(log, '分段完成', part=r.name, path=r.path, cameras=len(r.cameras),
          imu_records=len(r.imu) if r.imu else 0, audio_streams=len(r.audio_evidence))
    for reason in (r.error, r.imu_error, r.imu_frequency_error, r.grip_frequency_error):
        if reason:
            event(log, '处理异常', part=r.name, reason=reason)
    for camera in r.cameras.values():
        if camera.error:
            event(log, '读取异常', part=r.name, source=camera.file, reason=camera.error)
    for audio in r.audio_evidence:
        if audio.status == 'ERROR' or audio.reason.startswith('音频文件缺失：'):
            event(log, '读取异常', part=r.name, source=audio.source, reason=audio.reason)
    for reason in r.imu_frequency_validation_errors:
        event(log, '配置异常', part=r.name, reason=reason)
    for field in ('imu_frequency_error_by_device', 'grip_frequency_error_by_device'):
        for device, reason in getattr(r, field).items():
            event(log, '配置异常', part=r.name, device=device, reason=reason)
