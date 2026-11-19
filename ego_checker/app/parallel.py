"""Bounded Part workers, ordered delivery and explicit process ownership."""
import ctypes
import multiprocessing
from multiprocessing.connection import wait
import os
from pathlib import Path
import pickle
import time
import traceback

from ego_checker.runtime.control import AnalysisStopped
from ego_checker.runtime.worker_group import WorkerGroup, start_worker_group

MIB = 1024 * 1024
ANALYSIS_WORKER_CHOICES = (0, 1, 2, 4, 6, 8)
MEMORY_RESERVE_BYTES = 512 * MIB


class AnalysisWorkerError(RuntimeError):
    """A lost worker or invalid IPC response; never classify as valid input."""


def available_memory():
    """Available physical memory, or None when the OS cannot report it."""
    try:
        if os.name == 'nt':
            class MemoryStatus(ctypes.Structure):
                _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong)] + [
                    (name, ctypes.c_ulonglong) for name in (
                        'total', 'available', 'page_total', 'page_available',
                        'virtual_total', 'virtual_available', 'extended')]
            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return status.available
        else:
            with open('/proc/meminfo', encoding='ascii') as stream:
                for line in stream:
                    if line.startswith('MemAvailable:'):
                        return int(line.split()[1]) * 1024
    except (OSError, ValueError, AttributeError):
        pass
    return None


def _source_bytes(part):
    paths = list(part.get('imu', []))
    paths.extend(path for files in part.get('cameras', {}).values() for path in files)
    paths.extend(path for files in part.get('extra', {}).values() for path in files)
    paths.extend(part.get('mcaps', []))
    if part.get('mcap'):
        paths.append(part['mcap'])
    total = 0
    for path in {Path(p) for p in paths}:
        try:
            total += path.stat().st_size
        except OSError:
            pass  # The actual reader reports missing/unreadable source files.
    return total


def worker_plan(parts, options, *, compact=True, memory=None):
    requested = options.get('analysis_workers', 0)
    if type(requested) is not int or requested not in ANALYSIS_WORKER_CHOICES:
        raise ValueError('分析进程数必须为 0（自动）、1、2、4、6 或 8')
    sizes = [_source_bytes(part) for part in parts]
    estimate = max(768 * MIB, max(sizes, default=0) * 8 + 256 * MIB)
    if not compact:
        return 1, estimate, '完整时间轴模式（SEI、截图或关闭流式释放）'
    if len(parts) < 2 or requested == 1:
        return 1, estimate, '单段或指定串行'
    if not requested and sum(sizes) < 8 * MIB:
        return 1, estimate, '小数据使用串行，避免进程启动开销'
    cores = os.cpu_count() or 1
    count = min(requested or max(ANALYSIS_WORKER_CHOICES), cores, len(parts))
    available = available_memory() if memory is None else memory
    if available is None:
        return 1, estimate, '无法确认可用内存，使用串行'
    count = min(count, max(1, (available - MEMORY_RESERVE_BYTES) // estimate))
    return count, estimate, (f'按核心数、任务数和可用内存限制并发（核心数={cores}，'
                             f'任务数={len(parts)}，可用内存={available // MIB}MiB，'
                             f'系统预留={MEMORY_RESERVE_BYTES // MIB}MiB）')


def _worker_main(connection, paused, stopped):
    """Only this module and analysis code are imported in child processes."""
    start_worker_group()
    from ego_checker.app import analyzer
    from ego_checker.domain.models import PartResult

    def checkpoint():
        while paused.is_set() and not stopped.is_set():
            stopped.wait(0.1)
        if stopped.is_set():
            raise AnalysisStopped()

    try:
        while not stopped.is_set():
            job = connection.recv()
            if job is None:
                break
            index, part, options = job
            options['_read_checkpoint'] = checkpoint
            options['_analysis_log'] = lambda message: connection.send(('log', index, message))
            try:
                checkpoint()
                try:
                    result = analyzer.analyze_part(part, options)
                    if not result.error:
                        analyzer.extract_boundary_samples(result)
                except (AnalysisStopped, MemoryError):
                    raise
                except Exception as exc:
                    connection.send(('log', index, '[分段异常]\n' + traceback.format_exc()))
                    result = PartResult(name=part['name'], path=part['path'],
                                        multi_device=bool(part.get('_multi_device')),
                                        session_info=part.get('session_info', {}))
                    result.error = '分析异常: ' + str(exc)
                checkpoint()
                # Send the entire object graph so EdgeQuery.event aliases survive.
                analyzer.release_part_data(result)
                connection.send(('result', index, result))
            except AnalysisStopped:
                connection.send(('stopped', index, None))
                break
            except MemoryError:
                connection.send(('memory_error', index, None))
                break
    except (EOFError, BrokenPipeError, ConnectionResetError):
        pass  # Parent owns cleanup; never start replacement workers here.
    finally:
        connection.close()


class PartAnalyzer:
    """At most workers jobs exist, including completed out-of-order results."""

    def __init__(self, parts, options, *, analyze, compact=True, checkpoint=None, log=None):
        self.parts = parts
        self.options = dict(options or {})
        self.analyze = analyze
        self.checkpoint = checkpoint
        self.log = log
        self.workers, self.estimate, reason = worker_plan(parts, self.options, compact=compact)
        self.timeout = float(self.options.get('analysis_timeout_s', 0))
        if not 0 <= self.timeout < float('inf'):
            raise ValueError('分析超时必须为有限的非负秒数，0 表示不限制')
        self.child_options = {k: v for k, v in self.options.items()
                              if k not in ('_read_checkpoint', '_analysis_log')}
        if getattr(analyze, '__module__', '') != 'ego_checker.app.analyzer':
            self.workers, reason = 1, '自定义分析回调使用调用方所在进程'
        if self.workers > 1:
            try:
                pickle.dumps((parts, self.child_options), protocol=5)
            except (TypeError, AttributeError, pickle.PicklingError):
                self.workers, reason = 1, '运行参数无法跨进程传输，使用串行'
        self._slots = []
        self._results = {}
        self._submitted = self._consumed = 0
        self._closed = False
        self._pause_started = None
        self._context = multiprocessing.get_context('spawn')
        self._paused = self._context.Event()
        self._stopped = self._context.Event()
        self._reason = reason

    def _write_log(self, message):
        if self.log:
            try:
                self.log(message)
            except Exception as exc:
                raise AnalysisWorkerError('无法保存分析子进程日志') from exc

    def __enter__(self):
        self._write_log(f'[分析调度] 实际进程数={self.workers}；{self._reason}；'
                        f'每任务内存估算={self.estimate // MIB}MiB；在途任务上限={self.workers}')
        try:
            if self.workers > 1:
                for _ in range(self.workers):
                    parent, child = self._context.Pipe()
                    process = self._context.Process(target=_worker_main,
                                                    args=(child, self._paused, self._stopped))
                    try:
                        process.start()
                    except BaseException:
                        parent.close()
                        child.close()
                        raise
                    child.close()
                    slot = dict(process=process, connection=parent, index=None, started=0, group=None)
                    self._slots.append(slot)
                    slot['group'] = WorkerGroup(process)
                self._submit()
            return self
        except BaseException:
            self.close()
            raise

    def pause(self):
        if self._pause_started is None:
            self._pause_started = time.monotonic()
        self._paused.set()

    def resume(self):
        if self._pause_started is not None:
            duration = time.monotonic() - self._pause_started
            for slot in self._slots:
                slot['started'] += duration
            self._pause_started = None
        self._paused.clear()

    def stop(self):
        self._stopped.set()
        self._paused.clear()

    def _submit(self):
        for slot in self._slots:
            if self._submitted >= len(self.parts) or self._submitted - self._consumed >= self.workers:
                break
            if slot['index'] is not None:
                continue
            # Reduce new admissions under memory pressure; let one task progress.
            available = available_memory()
            if (available is not None and available < self.estimate + MEMORY_RESERVE_BYTES
                    and any(item['index'] is not None for item in self._slots)):
                break
            index = self._submitted
            try:
                slot['connection'].send((index, self.parts[index], self.child_options))
            except (OSError, EOFError) as exc:
                raise AnalysisWorkerError('无法向分析子进程提交任务') from exc
            slot['index'], slot['started'] = index, time.monotonic()
            self._submitted += 1

    def get(self, index):
        if index != self._consumed or self._closed:
            raise RuntimeError('Part 必须按发现顺序消费，且调度器必须处于运行状态')
        if self.checkpoint:
            self.checkpoint()
        if self._stopped.is_set():
            raise AnalysisStopped()
        if self.workers == 1:
            self._consumed += 1
            return self.analyze(self.parts[index], self.options)
        while index not in self._results:
            if self.checkpoint:
                self.checkpoint()
            if self._stopped.is_set():
                raise AnalysisStopped()
            ready = wait([slot['connection'] for slot in self._slots], timeout=0.1)
            for slot in self._slots:
                process = slot['process']
                if slot['connection'] in ready:
                    try:
                        message = slot['connection'].recv()
                    except (EOFError, OSError, pickle.UnpicklingError) as exc:
                        raise AnalysisWorkerError(f'分析子进程通信中断（PID={process.pid}）') from exc
                    if not isinstance(message, tuple) or len(message) != 3:
                        raise AnalysisWorkerError('分析子进程返回的消息格式无效')
                    kind, received, value = message
                    if received != slot['index']:
                        raise AnalysisWorkerError('分析子进程返回的 Part 与提交任务不一致')
                    if kind == 'log':
                        self._write_log(f"[{self.parts[received]['name']}] {value}")
                    elif kind == 'result':
                        from ego_checker.domain.models import PartResult
                        if not isinstance(value, PartResult) or value.name != self.parts[received]['name']:
                            raise AnalysisWorkerError('分析子进程返回的结果身份无效')
                        self._results[received] = value
                        slot['index'] = None
                    elif kind == 'stopped':
                        raise AnalysisStopped()
                    elif kind == 'memory_error':
                        raise MemoryError('分析子进程可用内存不足')
                    else:
                        raise AnalysisWorkerError('无法识别分析子进程响应')
                if process.exitcode is not None:
                    raise AnalysisWorkerError(f'分析子进程异常退出（PID={process.pid}，退出码={process.exitcode}）')
                if (self.timeout and self._pause_started is None and slot['index'] is not None
                        and time.monotonic() - slot['started'] > self.timeout):
                    name = self.parts[slot['index']]['name']
                    raise TimeoutError(f'分析 {name} 超过 {self.timeout:g} 秒')
            self._submit()
        result = self._results.pop(index)
        self._consumed += 1
        self._submit()
        return result

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.stop()
        for slot in self._slots:
            try:
                slot['connection'].send(None)
            except (OSError, BrokenPipeError):
                pass
        deadline = time.monotonic() + 2
        for slot in self._slots:
            slot['process'].join(max(0, deadline - time.monotonic()))
        for slot in self._slots:
            if slot['group'] is not None:
                slot['group'].close()
        for slot in self._slots:
            if slot['process'].is_alive():
                slot['process'].terminate()
        for slot in self._slots:
            slot['process'].join(2)
            slot['connection'].close()
            if slot['process'].is_alive():
                slot['process'].kill()
                slot['process'].join(2)
            if slot['process'].is_alive():
                raise AnalysisWorkerError('分析子进程未能安全回收')
            slot['process'].close()
        self._slots.clear()
        self._results.clear()

    def __exit__(self, exc_type, exc_value, exc_traceback):
        self.close()
