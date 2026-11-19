"""Compare declared triad rates with original per-sensor record coverage."""
import math


def audit_magnetic_frequency(profile, sequences):
    """Each source contributes its own intervals; never join file boundaries."""
    reference = profile['reference_hz']
    configured = profile['sensor_rates_hz']
    rows = []
    for sensor in ('accel', 'gyro', 'mag'):
        sources = []
        intervals, span_us, count = 0, 0, 0
        errors = [error for error in profile['configuration_errors']
                  if error.startswith(sensor + '被')]
        for sequence in sequences:
            kind = getattr(sequence, 'sensor_type', '').strip('/').split('/')[-1].lower()
            if kind != sensor:
                continue
            source_rows = getattr(sequence, 'source_rows', ())
            length = len(sequence)
            count += length
            sources.append(dict(source=getattr(sequence, 'source', ''), records=length,
                first_us=sequence[0] if length else None, last_us=sequence[-1] if length else None,
                first_csv_line=source_rows[0] + 2 if source_rows else None,
                last_csv_line=source_rows[-1] + 2 if source_rows else None))
            if length < 2 or sequence[-1] <= sequence[0]:
                errors.append('源记录不足或首尾跨度非正，实测频率未验证')
                continue
            intervals += length - 1
            span_us += sequence[-1] - sequence[0]
        measured = intervals * 1_000_000.0 / span_us if span_us > 0 else None
        rounded = math.floor(measured + 0.5) if measured is not None else None
        declared = configured.get(sensor)
        if not sources:
            errors.append('未取得可区分该传感器类型的源记录，实测频率未验证')
        if declared is None:
            errors.append('JSON频率缺失或无效')
        if reference is None:
            errors.append('磁力计基准无效，阈值未取得')
        elif declared is not None and declared != reference:
            errors.append(f'JSON {declared:g}Hz与mag基准 {reference:g}Hz不一致')
        if rounded is not None:
            if declared is not None and rounded != math.floor(declared + 0.5):
                errors.append(f'JSON {declared:g}Hz与实测 {measured:.6f}Hz（整数{rounded}Hz）不一致')
            if reference is not None and rounded != math.floor(reference + 0.5):
                errors.append(f'实测整数{rounded}Hz与mag基准 {reference:g}Hz不一致')
        rows.append(dict(device=profile['device'], sensor=sensor, configured_hz=declared,
            reference_hz=reference, measured_hz=measured, rounded_hz=rounded,
            record_count=count, interval_count=intervals, span_us=span_us,
            sources=sources, configuration_source=profile['configuration_source'],
            status='ERROR' if errors else 'PASS', reason='；'.join(errors)))
    return rows
