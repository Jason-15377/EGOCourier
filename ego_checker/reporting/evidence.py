"""Export retained analysis evidence without recomputing or fabricating matches."""
import csv
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
import hashlib
import html
import json
from pathlib import Path
import stat
import time
from urllib.parse import quote

from ego_checker.data.discovery import discover_parts
from ego_checker.reporting.columns import ReportColumns
from ego_checker.reporting.outputs import APP_VERSION, compute_stats, detail_blocks, generate_result_csv


@dataclass
class EvidenceBundle:
    directory: Path
    files: list
    source_errors: list


def _scalar_summary(value):
    if value is None:
        return None
    return {f.name: getattr(value, f.name) for f in fields(value)
            if isinstance(getattr(value, f.name), (str, int, float, bool, type(None)))}


def _source_paths(parts, results):
    paths = {}
    def add(path, kind):
        if path:
            p = Path(path)
            paths.setdefault(p, set()).add(kind)
    for part in parts:
        for _, files in part.get('camera_entries') or list(part.get('cameras', {}).items()):
            for p in files:
                add(p, '相机')
        for p in part.get('imu', []):
            add(p, 'IMU')
        for kind, files in part.get('extra', {}).items():
            for p in files:
                add(p, kind)
        for p in part.get('mcaps', []):
            add(p, 'MCAP')
        add(part.get('mcap'), 'MCAP')
    for result in results:
        for camera in result.cameras.values():
            add(camera.file, '相机')
        for audio in result.audio_evidence:
            source = audio.source.split('#', 1)[0]
            if Path(source).suffix.lower() in ('.csv', '.wav', '.mcap'):
                add(source, '音频')
    return sorted(paths.items(), key=lambda item: str(item[0]))


def _source_record(path, kinds, checkpoint, stopped):
    record = {'类型': '/'.join(sorted(kinds)), '字节数': '-', '修改时间ns': '-', 'SHA256': '-',
              '状态': 'UNVERIFIED', '说明': '', '源文件': str(path)}
    if stopped:
        record['说明'] = '用户已停止；证据导出不再访问源文件'
        return record
    failures = []
    for attempt in range(1, 4):
        if checkpoint:
            checkpoint()
        try:
            before = path.stat()
            if not stat.S_ISREG(before.st_mode):
                raise OSError('证据来源不是普通文件')
            sha = '-'
            if path.suffix.lower() in ('.csv', '.json'):
                digest = hashlib.sha256()
                with path.open('rb') as source:
                    for block in iter(lambda: source.read(1024*1024), b''):
                        if checkpoint:
                            checkpoint()
                        digest.update(block)
                sha = digest.hexdigest()
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise OSError('文件在证据摘要读取期间变化')
            record.update({'字节数': after.st_size, '修改时间ns': after.st_mtime_ns, 'SHA256': sha,
                           '状态': 'HASHED' if sha != '-' else 'STAT_ONLY',
                           '说明': f'证据导出时读取，第{attempt}次成功；' + ('内容摘要，不代表分析前后快照一致' if sha != '-' else '媒体仅记录大小和修改时间，未计算内容摘要')})
            return record
        except OSError as exc:
            failures.append(f'第{attempt}次：{exc}')
            if attempt < 3:
                time.sleep(.05)
    record.update({'状态': 'ERROR', '说明': '；'.join(failures)})
    return record


def export_evidence(results, out_dir, root, params=None, *, parts=None, boundary_rows=None,
                    run_notice='', checkpoint=None):
    """One new directory per run; incomplete exports retain their error status."""
    if parts is None:
        parts, error = discover_parts(Path(root), input_mode=(params or {}).get('input_mode', 'single'))
        if error:
            raise ValueError(f'证据输入发现失败：{error}')
    parent = Path(out_dir)
    parent.mkdir(parents=True, exist_ok=True)
    created = datetime.now(timezone(timedelta(hours=8)))
    stem = '数据与证据_' + created.strftime('%Y%m%d_%H%M%S_%f') + '_output'
    number = 0
    while True:
        directory = parent / (stem if not number else f'{stem}_{number}')
        try:
            directory.mkdir()
            break
        except FileExistsError:
            number += 1
    files = []
    source_errors = []
    status_path = directory/'export_status.json'

    def camera_reference(result, camera):
        candidates = [p for p in parts if p['name']==result.name and Path(p['path']).resolve()==Path(result.path).resolve()]
        groups = [files for part in candidates
                  for _,files in part.get('camera_entries') or list(part.get('cameras',{}).items())
                  if camera.file in [Path(f) for f in files]]
        if len(groups)==1 and len(groups[0])==1:
            return str(camera.file), camera.file.suffix.lower()=='.csv', ''
        sources = sorted({str(p) for group in groups for p in group})
        return (json.dumps(sources,ensure_ascii=False) if sources else str(camera.file), False,
                '相机序列来源不唯一或缺少归属描述；未确认的CSV行号不填')

    def status(state, reason=''):
        status_path.write_text(json.dumps({'state': state, 'reason': reason,
                                          'created_beijing': created.isoformat(),
                                          'run_notice': run_notice}, ensure_ascii=False, indent=2), encoding='utf-8')

    def write_csv(name, headers, rows):
        path = directory/name
        with path.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=headers)
            writer.writeheader()
            for index, row in enumerate(rows):
                if checkpoint and not run_notice and index % 4096 == 0:
                    checkpoint()
                writer.writerow(row)
        files.append(path)

    def write_json(name, data):
        path = directory/name
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
        files.append(path)

    status('WRITING')
    try:
        parameters = {k:v for k,v in (params or {}).items() if not k.startswith('_') and not callable(v)}
        write_json('analysis_summary.json', {
            'tool_version': APP_VERSION, 'root': str(root), 'created_beijing': created.isoformat(),
            'parameters': parameters, 'statistics': compute_stats(results), 'run_notice': run_notice,
            'boundary_rows': len(boundary_rows or []),
            'boundary_scope': '没有同源多分段时跳过边界读取，不生成边界明细',
            'evidence_scope': '本次分析实际保留的数据与结果。未重新执行匹配，不补造正常帧逐条匹配、源行号或读取尝试。输入清单列出分段描述和分析结果引用的文件，不是整个数据目录快照。输入摘要在证据导出阶段采集，不证明分析前后输入一致。',
            'parts': [dict(_scalar_summary(r), path=str(r.path), business_status=r.business_status,
                           checks=r.checks, camera_counts={k:c.count for k,c in r.cameras.items()}) for r in results],
        })
        path = generate_result_csv(results, directory/'segment_results.csv', params)
        files.append(path)

        camera_headers = ['分段','相机','帧数','首时间戳us','尾时间戳us','间隔结果JSON','丢帧结果JSON','错误','源文件']
        write_csv('camera_checks.csv', camera_headers, (
            dict(zip(camera_headers,[r.name, role,c.count,c.timestamps[0] if c.timestamps else '-',
                                    c.timestamps[-1] if c.timestamps else '-',
                                    json.dumps(_scalar_summary(r.intervals.get(role)),ensure_ascii=False),
                                    json.dumps(_scalar_summary(r.rgb_drop.get(role)),ensure_ascii=False),c.error,camera_reference(r,c)[0]]))
            for r in results for role,c in r.cameras.items()))
        camera_data_headers = ['分段','相机','分析序列位置_从1开始','CSV数据行号','时间戳us','来源说明','源文件']
        def camera_data():
            for r in results:
                for role,c in r.cameras.items():
                    source,known_rows,note=camera_reference(r,c)
                    for i,ts in enumerate(c.timestamps):
                        yield dict(zip(camera_data_headers,[r.name,role,i+1,
                            c.rows[i]+1 if known_rows and i<len(c.rows) and c.rows[i]>=0 else '-',ts,note,source]))
        write_csv('camera_timestamps.csv', camera_data_headers, camera_data())

        sync_headers = ['分段','检查项','相机','阈值us','总帧数','匹配帧数','超阈帧数','最大偏差us','平均偏差us','跨段命中数','结论']
        write_csv('sync_checks.csv', sync_headers, (
            dict(zip(sync_headers,[r.name,kind,role,s.threshold_us,s.total_frames,s.matched_frames,s.over_count,
                                  s.max_diff_us,s.mean_diff_us,s.context_hits,
                                  'ERROR' if r.error or (kind=='imu_sync' and r.imu_error) else 'PASS' if s.passed else 'FAIL']))
            for r in results for kind,checks in [('cam_sync',r.cam_sync),('imu_sync',r.imu_sync),('grip_sync',r.grip_sync)]
            for role,s in checks.items()))
        imu_headers = ['分段','设备或统计对象','统计结果JSON','错误']
        write_csv('imu_checks.csv', imu_headers, (
            dict(zip(imu_headers,[r.name,device,json.dumps(_scalar_summary(drop),ensure_ascii=False),r.imu_error]))
            for r in results for device,drop in (r.imu_drop_by_device or {'全部IMU':r.imu_drop}).items()))
        sequence_headers = ['分段','序列编号_从1开始','序列内位置_从1开始','时间戳us','传感器类型','CSV数据行号','来源说明','源文件']
        write_csv('imu_analysis_sequences.csv', sequence_headers, (
            dict(zip(sequence_headers,[r.name,n+1,i+1,ts,getattr(seq,'sensor_type','') or '-',
                seq.source_rows[i]+1 if i<len(getattr(seq,'source_rows',())) else '-',
                '主分析保留的原始记录，未去重；未知来源字段显示 -',getattr(seq,'source','') or '-']))
            for r in results if not r.imu_error for n,seq in enumerate(r.imu_drop_sources) for i,ts in enumerate(seq)))

        # Export the exact detail contract shared by GUI and HTML, without its display truncation.
        columns = ReportColumns(results)
        detail_headers = ['分段','检查项','详情标题','列名JSON','值JSON']
        def details():
            for r in results:
                for check, descriptor in zip(columns.headers(), columns.row_details(r)):
                    for title, headers, rows in detail_blocks(descriptor):
                        for row in rows:
                            yield dict(zip(detail_headers,[r.name,check,title,json.dumps(headers,ensure_ascii=False),json.dumps(row,ensure_ascii=False)]))
        write_csv('check_details.csv', detail_headers, details())
        error_headers = ['分段','主分析错误','IMU读取错误','读取尝试说明']
        write_csv('analysis_errors.csv', error_headers, (
            dict(zip(error_headers,[r.name,r.error,r.imu_error,'仅保存工具实际错误文本；未保留的尝试过程不补造']))
            for r in results if r.error or r.imu_error))
        audio_headers = ['分段','设备','音频结果JSON','源文件']
        write_csv('audio_checks.csv', audio_headers, (
            dict(zip(audio_headers,[r.name,getattr(a,'device',''),json.dumps(_scalar_summary(a),ensure_ascii=False),a.source]))
            for r in results for a in r.audio_evidence))
        if boundary_rows:
            write_csv('segment_boundaries.csv', list(boundary_rows[0]), boundary_rows)

        source_headers = ['类型','字节数','修改时间ns','SHA256','状态','说明','源文件']
        def sources():
            for path,kinds in _source_paths(parts, results):
                record = _source_record(path,kinds,checkpoint,bool(run_notice))
                if record['状态']=='ERROR':
                    source_errors.append(f"{path}: {record['说明']}")
                yield record
        write_csv('source_manifest.csv', source_headers, sources())
        manifest_headers = ['证据文件','字节数','SHA256']
        def manifest():
            for path in list(files):
                digest = hashlib.sha256()
                with path.open('rb') as stream:
                    for block in iter(lambda:stream.read(1024*1024),b''):
                        if checkpoint and not run_notice:
                            checkpoint()
                        digest.update(block)
                yield dict(zip(manifest_headers,[path.name,path.stat().st_size,digest.hexdigest()]))
        write_csv('evidence_manifest.csv', manifest_headers, manifest())
        # Publish completion only after all data and their manifest are closed.
        # The mutable status file and the manifest itself are not self-hashed.
        status('COMPLETE_WITH_SOURCE_ERRORS' if source_errors else 'COMPLETE', '\n'.join(source_errors))
        files.append(status_path)
    except BaseException as exc:
        try:
            status('INCOMPLETE', f'{type(exc).__name__}: {exc}')
        except OSError:
            pass  # Preserve the original write failure (e.g. a full disk).
        raise
    return EvidenceBundle(directory, files, source_errors)


def render_evidence_section(bundle, report_path):
    if bundle is None:
        return ''
    report_dir = Path(report_path).parent.resolve()
    esc = lambda value: html.escape(str(value), quote=True)
    document = '<section id="analysis-evidence"><h2>数据与证据文件</h2>'
    document += f'<p>本次文件保存于：{esc(bundle.directory.name)}。保留实际分析结果及可追溯的来源信息。</p>'
    if bundle.source_errors:
        document += '<p>ERROR：部分输入摘要读取失败，详见source_manifest.csv与export_status.json。</p>'
    document += '<ul>'
    for path in bundle.files:
        relative = path.resolve().relative_to(report_dir).as_posix()
        document += f'<li><a href="{quote(relative)}">{esc(path.name)}</a></li>'
    return document + '</ul></section>'
