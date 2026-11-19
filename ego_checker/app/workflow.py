"""分析、可选媒体后处理和报告发布；由调用方提供进度及结束回调。"""
import collections
import hashlib
import re
import sys
import threading
import traceback
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from time import perf_counter
import ego_checker.app.api as fc
import ego_checker.reporting.outputs as rep
import ego_checker.media.video as fe
from ego_checker.reporting.boundaries import collect_boundary_rows
from ego_checker.app.options import parse_capture_part_limit, parse_oos_group_limit, select_capture_part_indexes
from ego_checker.app.parallel import AnalysisWorkerError, PartAnalyzer
from ego_checker.runtime.control import AnalysisStopped as _AnalysisStopped
from ego_checker.runtime.errors import describe_exception, failure_message
from ego_checker.runtime.run_log import AnalysisRunLog
from ego_checker.app.analysis_log import event, part_progress, stage
from ego_checker.data.mcap import SEI_TOLERANCE_US


_REPORT_TIME = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}')


def session_report_key(item):
    """录制会话键；PartResult 与 part 字典都可用."""
    info = (item.get('session_info') if isinstance(item, dict) else item.session_info) or {}
    return str(info.get('id') or ''), str(info.get('session_start') or '')


def session_report_label(key):
    """会话显示名：主设备开始时间，缺失时用会话标识尾部."""
    session_id, started = key
    if started:
        return started.replace('T', ' ')
    tail = session_id.split('|')[-1] if session_id else '未识别会话'
    match = _REPORT_TIME.search(tail)
    return match.group(0).replace('T', ' ') if match else tail


def _safe_name(text):
    """报告文件名只能包含文件名允许的字符；路径分隔符等替换为下划线."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(text))
    return cleaned.strip(' .') or '未识别会话'


def session_report_name(key):
    """报告文件名：主设备会话开始时间；同开始时间多会话时追加 uuid 短哈希."""
    label = _safe_name(session_report_label(key)).replace(' ', '_').replace(':', '')
    return f"Ego同步帧测试报告_{label}{_session_suffix(key)}.html"


def _session_suffix(key):
    session_id, _started = key
    if not session_id:
        return ''
    return '_' + hashlib.sha256(session_id.encode('utf-8')).hexdigest()[:8]


class AnalysisTask:
    def __init__(self, root, out_root, params, options, *, log, progress, done, failed, stopped):
        self._emit_log = log
        self._emit_progress = progress
        self._emit_done = done
        self._emit_failed = failed
        self._emit_stopped = stopped
        self.root = root
        self.out_root = out_root
        self.params = params
        self.options = options
        self._pause = threading.Event()   # set=暂停
        self._stop = threading.Event()    # set=停止
        self._pause.clear()
        self._logged_session_warnings = set()
        self._progress_value = 0
        self._current_stage = "准备分析"
        self._analysis_log = None
        self.log_path = None
        self._outcome = 'FAILED'
        self._failure_reported = False
        self._part_batch = None


    def pause(self):
        self._pause.set()
        if self._part_batch is not None:
            self._part_batch.pause()
        self._log('[分析控制] 请求暂停')


    def resume(self):
        self._pause.clear()
        if self._part_batch is not None:
            self._part_batch.resume()
        self._log('[分析控制] 继续分析')


    def stop(self):
        self._stop.set()
        self._pause.clear()               # 解除暂停, 让线程走到停止点
        if self._part_batch is not None:
            self._part_batch.stop()
        self._log('[分析控制] 请求停止')


    def is_paused(self):
        return self._pause.is_set()


    def was_stopped(self):
        """本次 run 是否由用户停止（停止时仍会发出带部分报告的 done 回调）。"""
        return bool(getattr(self, "_pending_stopped", False))


    def _wait_if_paused(self):
        """在每个工作单元前调用: 暂停时阻塞等待, 停止时抛异常退出."""
        while self._pause.is_set() and not self._stop.is_set():
            self._stop.wait(0.1)
        if self._stop.is_set():
            raise _AnalysisStopped()


    def _log(self, msg):
        if self._analysis_log is not None:
            self._analysis_log.write(msg)
        else:
            self._emit_log(msg)


    def _report_failure(self, exc, prefix=""):
        self._outcome = 'TIMEOUT' if isinstance(exc, TimeoutError) else 'FAILED'
        if self._failure_reported:
            return
        self._failure_reported = True
        message = failure_message(exc, self._current_stage)
        if prefix:
            message = prefix + "\n" + message
        try:
            self._log(f"[异常] {message}\n{traceback.format_exc()}")
        except Exception:
            # A full disk or a second allocation failure must not suppress the
            # already prepared failure message sent to the UI.
            pass
        self._emit_failed(message)


    @contextmanager
    def _stage(self, name, index, total, *, partial=False):
        """Progress counts completed work, not elapsed time; one stage = 1000 units."""
        started = perf_counter()
        self._current_stage = name
        self._log(f"[阶段开始] {name}")
        last_detail = None

        def progress(detail="", completed=0, count=0):
            nonlocal last_detail
            if detail and (detail, completed, count) != last_detail:
                self._log(f'[阶段进度] {name}：{detail}（{completed}/{count}）')
                last_detail = detail, completed, count
            if not partial:
                # The final unit belongs to successful stage exit, not its last callback.
                fraction = min(999, max(0, int(1000 * completed / count))) if count else 0
                self._progress_value = max(self._progress_value, index * 1000 + fraction)
            title = f"{name}：{detail}" if detail else name
            if count:
                title += f"（{completed}/{count}）"
            if partial:
                title = "已停止，整理部分报告：" + title
            self._emit_progress(title, self._progress_value, total * 1000)
            if not partial:
                self._wait_if_paused()

        try:
            progress()
            yield progress
        except _AnalysisStopped:
            self._log(f"[阶段停止] {name}，耗时 {perf_counter() - started:.3f} 秒")
            raise
        except Exception:
            self._log(f"[阶段异常] {name}，耗时 {perf_counter() - started:.3f} 秒")
            raise
        else:
            self._log(f"[阶段完成] {name}，耗时 {perf_counter() - started:.3f} 秒")
            if not partial:
                self._progress_value = (index + 1) * 1000
                self._emit_progress(f"{name}：完成", self._progress_value, total * 1000)


    def _log_part(self, r):
        """Report analysis progress and execution errors only."""
        part_progress(self._log, r)
        for warning in r.session_info.get("warnings", []):
            key = (str(r.path), warning)
            if key not in self._logged_session_warnings:
                self._log(f"[会话警告] {r.path}：{warning}")
                self._logged_session_warnings.add(key)


    def _export_sei(self, r, sei_out_root):
        """提取 SEI 时间戳随源数据保存；依赖本段全量相机/IMU 时间戳."""
        if r.error or not r.cameras:
            return
        if any(c.file.suffix.lower() == ".mcap" for c in r.cameras.values()):
            sei_dir = sei_out_root / r.name
            for role, c in r.cameras.items():
                fe.save_sei_csv(c.timestamps, sei_dir / f"{role}_sei.csv")
            # 同时把 mcap 里提取的 IMU 数据也保存出来
            if r.imu_by_device and not r.imu_error:
                for dev, its in r.imu_by_device.items():
                    fe.save_sei_csv(its, sei_dir / f"{dev}_imu_sei.csv")
            elif r.imu and not r.imu_error:
                fe.save_sei_csv(r.imu, sei_dir / "imu_sei.csv")
            # MCAP 的 SEI 与字段1时间差在提取时逐条核对；超容差/缺失只报告，
            # 不改变已按字段1完成的分析。报告“SEI对齐”列据此判定并展示明细。
            audit = r.sei_align_audit or {}
            anomalies = []
            for role, info in audit.items():
                for sample in info.get('samples', []):
                    delta = sample.get('delta_us')
                    anomalies.append(fc.AbnormalFrame(
                        csv_line=None, row_index=None,
                        value_us=abs(delta) if delta is not None else 0,
                        threshold_us=SEI_TOLERANCE_US,
                        cam=role, ts_a=sample.get('timestamp_us'),
                        ts_b=sample.get('sei_timestamp'),
                        detail=(f"消息序号={sample.get('sequence')}："
                                f"{sample.get('reason')}")))
            abnormal = sum(info.get('missing', 0) + info.get('out_of_tolerance', 0)
                           for info in audit.values())
            r.checks["sei_align"] = abnormal == 0
            r.over_frames_by_check["sei_align"] = anomalies[:200]
            if abnormal:
                detail = "、".join(
                    f"{role} 缺失{info.get('missing', 0)}/超容差{info.get('out_of_tolerance', 0)}"
                    for role, info in audit.items()
                    if info.get('missing') or info.get('out_of_tolerance'))
                r.notices.append(
                    f"SEI对齐：{abnormal}条消息SEI缺失或与字段1时间差超出容差"
                    f"{SEI_TOLERANCE_US}µs（{detail}）；已按字段1时间戳继续分析")
            self._log(f"  Part {r.name}: MCAP 相机时间戳及可用 IMU 已保存到 {sei_dir}；"
                      f"SEI异常 {abnormal} 条")
            return
        cam_ts = {role: c.timestamps for role, c in r.cameras.items()}
        self._log(f"  Part {r.name}:")
        sei_res = fe.run_sei_alignment(r.path, cam_ts,
                                       sei_out_root / r.name,
                                       log=self._log,
                                       video_by_role=fe.resolve_camera_mp4s(
                                           {role: c.file for role, c in r.cameras.items()}))
        if not sei_res:
            r.checks["sei_align"] = True
            return
        all_pass = all(v["pass"] for v in sei_res.values()) and bool(sei_res)
        r.checks["sei_align"] = all_pass
        r.over_frames_by_check["sei_align"] = []
        for role, v in sei_res.items():
            if not v["pass"] and v["mismatch"] > 0:
                csv_ts = list(cam_ts.get(role, []))
                sei_ts = list(v.get("sei_ts", []))
                m = max(len(csv_ts), len(sei_ts))
                for i in range(m):
                    s = sei_ts[i] if i < len(sei_ts) else None
                    cc = csv_ts[i] if i < len(csv_ts) else None
                    if s is not None and cc is not None and abs(s - cc) <= SEI_TOLERANCE_US:
                        continue
                    dev = abs(s - cc) if (s is not None and cc is not None) else 0
                    reason = ("SEI或CSV时间戳缺失" if s is None or cc is None
                              else f"偏差{dev}µs超出容差{SEI_TOLERANCE_US}µs")
                    r.over_frames_by_check["sei_align"].append(
                        fc.AbnormalFrame(
                            csv_line=i + 2, row_index=i,
                            value_us=dev, threshold_us=SEI_TOLERANCE_US,
                            cam=role, ts_a=cc, ts_b=s,
                            detail=f"SEI({role})第{i}行：{reason}"))
        r.over_frames_by_check["sei_align"] = \
            r.over_frames_by_check["sei_align"][:200]

    def _capture_frames(self, r, cap_root):
        """截取本段不同步帧与同步帧组；依赖本段全量相机时间戳."""
        if r.error:
            self._log(f"  Part {r.name}: 分析异常，无法截取")
            return
        synced_n = int(self.options.get("synced_groups", 10))
        oos_limit = parse_oos_group_limit(
            self.options.get("oos_group_limit", "ALL"))
        camera_ts = {role: c.timestamps for role, c in r.cameras.items()}
        oos_map = {}
        oos_groups = []
        if self.options.get("capture_oos"):
            for role, sr in r.cam_sync.items():
                if sr.abnormal:
                    oos_map[role] = [a.row_index for a in sr.abnormal
                                     if a.row_index >= 0]
            # 每个不同步基准帧都带上其他设备/相机的最近帧，
            # 按基准角色分组并独立编号。
            oos_groups = fe.make_oos_groups(
                r.cam_sync, camera_ts, max_groups=oos_limit)
        # 同步帧组: 随机截取 synced_n 组
        groups = None
        if self.options.get("capture_synced"):
            groups = fe.make_synced_groups(camera_ts, synced_n)
        self._log(f"  Part {r.name}: 不同步帧 {len(oos_groups)} 组(多设备), "
                  f"同步帧 {len(groups) if groups else 0} 组")
        # 混合结构 (如 D3B SD_MP4) 同时有 MP4 与 depth MCAP，两类源要分别截取；
        # 不能因看到一个 MCAP 就跳过其余 MP4。多设备会话的视频在不同子目录，
        # 通过每路相机的 CSV 源文件精确配对。
        video_by_role = fe.resolve_camera_mp4s(
            {role: c.file for role, c in r.cameras.items() if c.file})
        mcap_roles = {}
        for role, camera in r.cameras.items():
            if camera.file and camera.file.suffix.lower() == ".mcap":
                mcap_roles.setdefault(camera.file, []).append(role)
        # 单个源截取失败不应中断整体分析。
        try:
            if video_by_role:
                fe.capture_part_frames(
                    r.path, cap_root, oos_map, groups, log=self._log,
                    oos_groups=oos_groups, video_by_role=video_by_role,
                    part_name=r.name)
            for mcap_path, source_roles in mcap_roles.items():
                fe.capture_mcap_frames(
                    mcap_path,
                    camera_ts, oos_map, groups, cap_root, log=self._log,
                    oos_groups=oos_groups,
                    role_aliases=fe.mcap_capture_role_aliases(source_roles),
                    part_name=r.name)
            if not video_by_role and not mcap_roles:
                self._log(f"  Part {r.name}: 未找到可截取的 MP4/MCAP 源")
        except (_AnalysisStopped, MemoryError):
            raise
        except Exception as e:  # noqa: BLE001
            self._log(f"  Part {r.name}: 帧截取失败, 已跳过 ({describe_exception(e)})")

    def _select_capture_results(self, results):
        """按 options 选出要截取的 Part；首尾必选，中间优先不同步 Part."""
        part_limit = parse_capture_part_limit(
            self.options.get("capture_part_limit", "ALL"))
        indexes = select_capture_part_indexes(results, part_limit)
        return [results[index] for index in indexes]

    def _streamed_triplet(self, previous, current, following):
        """处理中间段的两个跨段方向；失败不中断整体分析."""
        from ego_checker.analysis.context import apply_streamed_triplet
        try:
            apply_streamed_triplet(previous, current, following, self.params)
        except (_AnalysisStopped, MemoryError):
            raise
        except Exception as exc:  # noqa: BLE001
            self._log(f"[跨段异常] 相邻段补偿\n{traceback.format_exc()}")
            result = current[1]
            result.error = (result.error + f" 跨段边界补偿失败: {describe_exception(exc)}").strip()

    def _release_after_compensation(self, result):
        """跨段补偿完成（或该段无需补偿）后释放原始时间戳."""
        if result.error:
            # 异常段不参与补偿，也不保留原始数据。
            fc.release_part_data(result)
            return
        if not result.streamed:
            fc.extract_boundary_samples(result)
        fc.release_part_data(result)

    def _write_reports(self, results, root, out_root, grand,
                       run_notice="", parts=None):
        """仅生成HTML；正常完成与用户停止后的部分报告共用.

        「多次录制平铺」模式下每个录制会话单独出一份报告，文件名带主设备开始时间。
        """
        self._current_stage = "统计分析结果"
        if self.params.get("layout_mode") == "flat":
            return self._write_session_reports(results, root, out_root, grand,
                                               run_notice=run_notice, parts=parts)
        stats = rep.compute_stats(results)
        self._log(f"[统计完成] 已整理 {stats['total']} 个 Part，准备生成报告")

        out_html = rep.unique_path(out_root / rep.REPORT_NAME)
        with self._stage("跨段边界测试", grand - 2, grand, partial=bool(run_notice)) as progress:
            boundary_rows = collect_boundary_rows(
                results, root, self.params, parts=parts,
                checkpoint=None if run_notice else self._wait_if_paused,
                run_notice=run_notice, progress=progress)
            event(self._log, '边界检查完成', count=len(boundary_rows))
        with self._stage("生成合并 HTML 报告", grand - 1, grand, partial=bool(run_notice)):
            rep.generate_html(results, out_html, root, self.params,
                              run_notice=run_notice, boundary_rows=boundary_rows,
                              checkpoint=None if run_notice else self._wait_if_paused)
        self._log(f"HTML 报告已保存: {out_html}")
        return out_html

    def _write_session_reports(self, results, root, out_root, grand,
                               *, run_notice="", parts=None):
        """平铺落盘：按录制会话分组，每组单独生成一份报告."""
        groups = collections.OrderedDict()
        for result in results:
            groups.setdefault(session_report_key(result), []).append(result)
        part_by_session = collections.defaultdict(list)
        for part in parts or []:
            part_by_session[session_report_key(part)].append(part)
        stats = rep.compute_stats(results)
        self._log(f"[统计完成] 已整理 {stats['total']} 个 Part、{len(groups)} 个录制会话，"
                  "准备分别生成报告")
        written = []
        for index, (key, session_results) in enumerate(groups.items(), 1):
            label = session_report_label(key)
            self._log(f"[会话 {index}/{len(groups)}] {label}：{len(session_results)} 个 Part")
            out_html = rep.unique_path(out_root / session_report_name(key))
            session_parts = part_by_session.get(key) or []
            if not session_parts:
                # 不传 parts 会让边界检查回退为全量扫描，导致报告带其它会话的边界。
                self._log(f"[会话 {index}] {label}：缺少对应的 Part 定义，跨段边界跳过")
            with self._stage(f"跨段边界测试 {label}", grand - 2, grand,
                             partial=bool(run_notice)) as progress:
                boundary_rows = (collect_boundary_rows(
                    session_results, root, self.params,
                    parts=session_parts,
                    checkpoint=None if run_notice else self._wait_if_paused,
                    run_notice=run_notice, progress=progress)
                    if session_parts else [])
            with self._stage(f"生成 HTML 报告 {label}", grand - 1, grand,
                             partial=bool(run_notice)):
                rep.generate_html(session_results, out_html, root, self.params,
                                  run_notice=run_notice, boundary_rows=boundary_rows,
                                  checkpoint=None if run_notice else self._wait_if_paused)
            self._log(f"HTML 报告已保存: {out_html}")
            written.append(out_html)
        return written[0] if len(written) == 1 else written


    def run(self):
        self._failure_reported = False
        self._outcome = 'FAILED'
        self._current_stage = '创建分析日志'
        self.log_path = None
        self._pending_done = None
        self._pending_stopped = False
        try:
            with AnalysisRunLog(emit=self._emit_log) as run_log:
                self._analysis_log = run_log
                self.log_path = run_log.path
                if getattr(self, 'startup_log_path', None):
                    self._log(f'工具启动及原生异常日志: {self.startup_log_path}')
                run_log.configuration(self.root, self.out_root, self.params, self.options)
                self._run()
                run_log.outcome = self._outcome
                self._current_stage = '保存分析日志'
            self._analysis_log = None
            if self._pending_done is not None and not self._failure_reported:
                self._current_stage = '更新分析结果'
                self._emit_done(*self._pending_done)
                if self._pending_stopped:
                    self._emit_stopped()
        except Exception as exc:
            self._analysis_log = None
            self._report_failure(exc)
        finally:
            self._analysis_log = None
            self._part_batch = None


    def _run(self):
        root = Path(self.root)
        out_root = Path(self.out_root)
        parts = []
        results = []
        n = 0
        grand = 2
        discovery_complete = False
        try:
            self._current_stage = "准备分析及输出目录"
            out_root.mkdir(parents=True, exist_ok=True)
            self._log("=" * 64)
            self._log(f"[{datetime.now():%H:%M:%S}] 数采分析工具 开始")
            self._log(f"数据根目录: {root}")
            self._log(f"输出目录: {out_root}")

            # 发现 part
            mode = self.params.get("input_mode", "single")
            self._current_stage = "扫描数据目录"
            self._log(f"会话数据类型: {mode}")
            self._emit_progress("扫描数据目录", 0, grand * 1000)
            with stage(self._log, '扫描数据目录'):
                parts, err = fc.discover_parts(
                    root, input_mode=mode, checkpoint=self._wait_if_paused,
                    layout_mode=self.params.get("layout_mode", "separate"),
                    pairing_level=self.params.get("pairing_level", "meta"))
            discovery_complete = True
            if err:
                self._log(f"[错误] {err}")
                self._emit_failed(f"发生阶段：{self._current_stage}\n错误：{err}")
                return
            n = len(parts)
            capture_enabled = bool(self.options.get("capture_oos")
                                   or self.options.get("capture_synced"))
            streamed = bool(self.options.get("stream_release", True)) and not capture_enabled
            grand = (n + 4 + bool(self.options.get("sei_align"))
                     + bool(capture_enabled) + bool(streamed))
            self._log(f"发现 {n} 个 part (录制段)")

            # 逐个分析, 进度 + 运行时日志。
            # 与 run_analysis 同一套滑动窗口：当前段的前后邻居都就绪后立即完成
            # 两个方向的跨段补偿并释放该段原始时间戳，常驻内存最多 3 段，
            # 不会随分段数增长。SEI 对齐只依赖本段数据，在释放前逐段完成。
            # 帧截取需要先知道全部分段才能确定选中项，开启时本次运行退回
            # 全量驻留，保证截取结果与旧版逐帧一致。
            pending = []          # [(part, result), ...] 尚未完成跨段补偿的段
            sei_enabled = bool(self.options.get("sei_align"))
            if sei_enabled:
                self._log("\n[SEI 时间戳对齐]（随分段分析执行，结果写入 sei_extracted）")
            batch = PartAnalyzer(
                parts, dict(self.params, _read_checkpoint=self._wait_if_paused, _analysis_log=self._log),
                analyze=fc.analyze_part, compact=streamed and not sei_enabled,
                checkpoint=self._wait_if_paused, log=self._log)
            self._part_batch = batch
            if self._pause.is_set():
                batch.pause()
            if self._stop.is_set():
                batch.stop()
            with batch:
                for i, p in enumerate(parts):
                    self._wait_if_paused()
                    self._log(f"[分析进度] Part {i+1}/{n}：{p['name']}")
                    with self._stage(f"分析 {p['name']}", i, grand):
                        try:
                            r = batch.get(i)
                        except (_AnalysisStopped, MemoryError, AnalysisWorkerError, TimeoutError):
                            raise
                        except Exception as e:  # noqa: BLE001
                            r = fc.PartResult(name=p["name"], path=p["path"],
                                              multi_device=bool(p.get("_multi_device")),
                                              session_info=p.get("session_info", {}))
                            r.error = f"分析异常: {describe_exception(e)}"
                            self._log(f"[分段异常] {p['name']}\n{traceback.format_exc()}")
                        results.append(r)
                        self._log_part(r)
                        if sei_enabled:
                            # SEI 必须在释放前读取本段全量时间戳。
                            self._export_sei(r, root / "sei_extracted")
                        if streamed:
                            if not r.error:
                                try:
                                    fc.extract_boundary_samples(r)
                                except (_AnalysisStopped, MemoryError):
                                    raise
                                except Exception as exc:  # noqa: BLE001
                                    # 固化失败只影响该段跨段补偿，不中断整次分析。
                                    reason = describe_exception(exc)
                                    self._log(f"[跨段] IMU 边界参考失败: {r.name}: {reason}")
                                    r.error = (r.error +
                                               f" IMU跨段参考失败: {reason}").strip()
                            pending.append((p, r))
                            # 中间段前后邻居都就绪：补偿后立即释放，内存不随段数累积。
                            while len(pending) >= 3:
                                self._streamed_triplet(pending[0], pending[1], pending[2])
                                self._release_after_compensation(pending[1][1])
                                pending.pop(1)

            self._part_batch = None
            # SEI 已随分段分析逐段完成；此处只补发阶段结束事件，
            # 保持既有阶段可见性与进度口径，不重新读取已释放的时间戳。
            if sei_enabled:
                self._current_stage = "SEI 时间戳对齐"
                self._log("[阶段完成] SEI 时间戳对齐")
                self._emit_progress("SEI 时间戳对齐：完成", self._progress_value,
                                    grand * 1000)

            # 流式口径的收尾：剩余段已无后继邻居，补完 previous 后释放。
            # 使用原有阶段名，保持暂停/停止响应点与进度展示口径不变。
            if streamed:
                try:
                    with self._stage("IMU 跨段同步", n, grand) as progress:
                        progress('相邻段边界补偿', 0, max(1, len(pending)))
                        completed = 0
                        while len(pending) > 1:
                            self._streamed_triplet(None, pending[0], pending[1])
                            self._release_after_compensation(pending[0][1])
                            pending.pop(0)
                            completed += 1
                            progress('相邻段边界补偿', completed, max(1, len(pending) + completed))
                        if pending:
                            self._release_after_compensation(pending[0][1])
                except (_AnalysisStopped, MemoryError):
                    raise
                except Exception as e:  # noqa: BLE001
                    reason = describe_exception(e)
                    self._log(f"[跨段] IMU 边界参考失败: {reason}\n{traceback.format_exc()}")
                    for result in results:
                        result.error = (result.error + f" IMU跨段参考失败: {reason}").strip()

            # 非流式口径：全部分段分析完成后统一做跨段补偿。
            # 流式口径的 IMU 相邻段补偿已在分析循环内随滑动窗口完成。
            if not streamed:
                try:
                    with self._stage("IMU 跨段同步", n, grand):
                        fc.apply_imu_segment_context(parts, results,
                            dict(self.params, _read_checkpoint=self._wait_if_paused))
                except (_AnalysisStopped, MemoryError):
                    raise
                except Exception as e:  # noqa: BLE001
                    reason = describe_exception(e)
                    self._log(f"[跨段] IMU 边界参考失败: {reason}\n{traceback.format_exc()}")
                    for result in results:
                        result.error = (result.error + f" IMU跨段参考失败: {reason}").strip()

            # 流式口径的相机相邻段补偿已随滑动窗口完成；释放后只剩边界样本，
            # 再跑全量补偿会把已补偿结果重置回段内基线，覆盖跨段判定。
            if not streamed:
                try:
                    with self._stage("相机跨段同步", n + 1, grand):
                        fc.apply_cam_segment_context(parts, results,
                            dict(self.params, _read_checkpoint=self._wait_if_paused))
                except (_AnalysisStopped, MemoryError):
                    raise
                except Exception as e:  # noqa: BLE001
                    reason = describe_exception(e)
                    self._log(f"[跨段] 相机边界参考失败: {reason}\n{traceback.format_exc()}")
                    for result in results:
                        result.error = (result.error + f" 相机跨段参考失败: {reason}").strip()
            else:
                # 补偿已随滑动窗口完成；保留本阶段的进度口径与暂停/停止响应点。
                with self._stage("相机跨段同步", n + 1, grand) as progress:
                    progress("已随滑动窗口完成", 0, 1)

            # 白名单
            self._current_stage = "处理白名单"
            wl_path = self.options.get("whitelist")
            if wl_path:
                wl = fc.load_whitelist(Path(wl_path))
                fc.apply_whitelist(results, wl)
                self._log(f"白名单: 载入 {len(wl)} 条, 命中 "
                          f"{sum(1 for r in results if r.whitelisted)} 个 part")
            else:
                self._log("白名单: 未指定")

            # SEI 对齐只依赖本段数据，已在上面的循环里于释放前逐段完成。
            # 帧截取需要先知道全部分段才能确定选中的 Part，只在全量驻留口径下执行。
            if not streamed and capture_enabled:
                with self._stage("帧图片截取", n + 2 + bool(self.options.get("sei_align")), grand) as progress:
                    self._log("\n[帧图片截取]")
                    cap_root = out_root / "frame_dumps"
                    capture_results = self._select_capture_results(results)
                    self._log(f"截取 Part: {len(capture_results)}/{len(results)} "
                              f"[{', '.join(r.name for r in capture_results)}]")
                    for part_index, r in enumerate(capture_results):
                        progress(r.name, part_index, len(capture_results))
                        self._wait_if_paused()
                        self._capture_frames(r, cap_root)
                    self._log("[帧截取] 完成")

            # 统计 + 报告 (纳入进度, 进度 100% 时报告已生成)
            self._wait_if_paused()
            out_html = self._write_reports(
                results, root, out_root, grand, parts=parts)

            self._log(f"[{datetime.now():%H:%M:%S}] 分析完成 ✓")
            self._outcome = 'COMPLETED'
            self._current_stage = "更新分析结果"
            self._pending_done = results, str(out_root), str(out_html)
        except _AnalysisStopped:
            self._outcome = 'STOPPED'
            analyzed_count = len(results)
            self._log(f"[{datetime.now():%H:%M:%S}] 分析已被用户停止，正在生成部分报告…")
            try:
                for part in parts[analyzed_count:]:
                    result = fc.PartResult(name=part["name"], path=part["path"],
                                           multi_device=bool(part.get("_multi_device")),
                                           session_info=part.get("session_info", {}))
                    result.data_folder = str(part["path"].name)
                    result.error = "用户停止，未执行分析"
                    results.append(result)

                wl_path = self.options.get("whitelist")
                if wl_path:
                    fc.apply_whitelist(results, fc.load_whitelist(Path(wl_path)))

                notice = (f"用户已停止运行：已完成 {analyzed_count}/{len(parts)} 个 Part 分析；"
                          "未完成 Part 已标记为 ERROR；跨段同步、边界测试及 SEI/帧截取等后处理可能不完整。")
                if not discovery_complete:
                    notice = "用户已停止运行：目录扫描未完成，Part 数量及覆盖范围未确认，未执行数据分析。"
                out_html = self._write_reports(
                    results, root, out_root, grand, run_notice=notice, parts=parts)
                self._log(f"[{datetime.now():%H:%M:%S}] 用户停止后的部分报告已生成 ✓")
                self._pending_done = results, str(out_root), str(out_html)
                self._pending_stopped = True
            except Exception as e:  # noqa: BLE001
                self._report_failure(e, "用户停止后生成报告失败")
        except Exception as e:  # noqa: BLE001
            self._report_failure(e)
