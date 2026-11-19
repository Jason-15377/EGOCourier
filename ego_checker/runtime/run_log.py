"""Persistent, line-buffered analysis run logs."""

from datetime import datetime
from contextlib import contextmanager
import faulthandler
import json
import os
from pathlib import Path
import platform
import sys
import threading
import traceback
from time import perf_counter

from ego_checker.runtime.paths import data_directory


def tool_directory() -> Path:
    """分析运行日志的持久化根目录（EGO Courier 的 data/，或冻结程序的工具目录）。"""
    return data_directory()


class RunLogWriter:
    """Append analysis messages to one UTF-8 log and flush every message."""

    _fault_owner = None

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._stream = self.path.open("x", encoding="utf-8-sig", buffering=1)
        self._fault_handler_enabled = False

    @classmethod
    def create(cls, now=None):
        """Create a collision-safe log below the tool's ``log`` directory."""
        created_at = now or datetime.now()
        stem = f"analysis_{created_at:%Y%m%d_%H%M%S_%f}"
        # 用户可见日志落在 EGO Courier 的 data/log，进程退出后仍然保留
        # （PyInstaller 的 _MEIPASS 临时目录退出即删，不能放那里）。
        log_dir = tool_directory() / "log"
        for suffix in range(1000):
            name = f"{stem}.log" if suffix == 0 else f"{stem}_{suffix}.log"
            try:
                return cls(log_dir / name)
            except FileExistsError:
                continue
        raise FileExistsError(f"无法创建唯一运行日志: {log_dir / (stem + '.log')}")

    def write(self, message) -> None:
        """Write all lines with a local timestamp and make them durable to Python."""
        text = str(message)
        lines = text.splitlines() or [""]
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        with self._lock:
            if self._stream.closed:
                raise ValueError(f"运行日志已经关闭: {self.path}")
            for line in lines:
                self._stream.write(f"[{timestamp}] {line}\n")
            self._stream.flush()

    def write_runtime_header(self) -> None:
        """Record process identity before importing or constructing the GUI."""
        from ego_checker.runtime.version import APP_VERSION, BUILD_DATE

        mode = "打包程序" if getattr(sys, "frozen", False) else "源码运行"
        self.write("=" * 72)
        self.write("[工具启动] 数采分析工具")
        self.write(f"应用版本: {APP_VERSION}，构建日期: {BUILD_DATE}，运行模式: {mode}")
        self.write(f"进程: PID={os.getpid()}，主线程={threading.current_thread().name}")
        self.write(f"操作系统: {platform.platform()}，架构: {platform.machine()}")
        self.write(f"Python: {platform.python_version()} ({sys.executable})")
        self.write(f"工具目录: {tool_directory()}")
        self.write(f"工作目录: {Path.cwd()}")
        self.write(f"启动参数: {sys.argv!r}")
        self.write(f"日志文件: {self.path}")

    def enable_fault_handler(self) -> None:
        """Ask Python to dump fatal native/Python thread stacks into this log."""
        with self._lock:
            faulthandler.enable(file=self._stream, all_threads=True)
            self._fault_handler_enabled = True
            RunLogWriter._fault_owner = self
            self._stream.flush()

    def close(self) -> None:
        with self._lock:
            if self._fault_handler_enabled and RunLogWriter._fault_owner is self:
                faulthandler.disable()
                RunLogWriter._fault_owner = None
                self._fault_handler_enabled = False
            if not self._stream.closed:
                try:
                    self._stream.flush()
                finally:
                    self._stream.close()


class AnalysisRunLog:
    """One analysis owns one file; no process-wide stdout redirection or hooks."""

    def __init__(self, *, emit=None):
        self.emit = emit
        self.writer = None
        self.path = None
        self.outcome = 'FAILED'

    def __enter__(self):
        self.started = perf_counter()
        self.writer = RunLogWriter.create()
        self.path = self.writer.path
        try:
            self.writer.write_runtime_header()
            self.write(f'[分析开始] 本次分析日志: {self.path}')
        except BaseException:
            self.writer.close()
            raise
        return self

    def write(self, message):
        self.writer.write(message)
        if self.emit is not None:
            self.emit(message)

    def display(self, message):
        """Persist CLI output even when the GUI-subsystem EXE has no stdout."""
        self.write(message)
        if sys.stdout is not None:
            print(message)

    def configuration(self, root, out, params, options=None):
        self.write(f'数据根目录: {Path(root).absolute()}')
        self.write(f'输出目录: {Path(out).absolute() if out is not None else "未指定（仅分析）"}')
        visible = lambda values: {k:v for k,v in values.items() if not k.startswith('_')}
        self.write('[分析配置] ' + json.dumps(dict(params=visible(params), options=visible(options or {})),
            ensure_ascii=False, sort_keys=True, default=str))

    @contextmanager
    def stage(self, name):
        started = perf_counter()
        self.write(f'[阶段开始] {name}')
        try:
            yield
        except BaseException:
            self.write(f'[阶段异常] {name}，耗时 {perf_counter()-started:.3f} 秒')
            raise
        else:
            self.write(f'[阶段完成] {name}，耗时 {perf_counter()-started:.3f} 秒')

    def __exit__(self, exc_type, exc, tb):
        from ego_checker.runtime.control import AnalysisStopped
        try:
            if exc_type is not None:
                if isinstance(exc, (AnalysisStopped, KeyboardInterrupt)):
                    self.outcome = 'STOPPED'
                elif isinstance(exc, TimeoutError):
                    self.outcome = 'TIMEOUT'
                self.write('[分析异常]\n' + ''.join(traceback.format_exception(exc_type, exc, tb)))
            self.write(f'[分析结束] 状态={self.outcome}，总耗时 {perf_counter()-self.started:.3f} 秒')
        finally:
            self.writer.close()


def install_python_exception_logging(writer: RunLogWriter):
    """Install hooks for uncaught main-thread, worker-thread and unraisable errors."""
    original_sys_hook = sys.excepthook
    original_thread_hook = threading.excepthook
    original_unraisable_hook = sys.unraisablehook

    def persist(label, detail):
        try:
            writer.write(f"[{label}]\n{detail}")
        except Exception:
            pass

    def sys_hook(exc_type, exc_value, exc_traceback):
        persist("未捕获Python异常", "".join(traceback.format_exception(
            exc_type, exc_value, exc_traceback)))
        original_sys_hook(exc_type, exc_value, exc_traceback)

    def thread_hook(args):
        detail = (f"线程: {args.thread.name if args.thread else '-'}\n"
                  + "".join(traceback.format_exception(
                      args.exc_type, args.exc_value, args.exc_traceback)))
        persist("未捕获线程异常", detail)
        original_thread_hook(args)

    def unraisable_hook(args):
        detail = (f"对象: {args.object!r}\n"
                  + (f"说明: {args.err_msg}\n" if args.err_msg else "")
                  + "".join(traceback.format_exception(
                      args.exc_type, args.exc_value, args.exc_traceback)))
        persist("无法传播的Python异常", detail)
        original_unraisable_hook(args)

    sys.excepthook = sys_hook
    threading.excepthook = thread_hook
    sys.unraisablehook = unraisable_hook
    return original_sys_hook, original_thread_hook, original_unraisable_hook


def restore_python_exception_logging(original_hooks) -> None:
    """Restore the process-wide hooks replaced for one GUI lifetime."""
    if original_hooks is None:
        return
    sys.excepthook, threading.excepthook, sys.unraisablehook = original_hooks


__all__ = [
    "RunLogWriter", "AnalysisRunLog", "install_python_exception_logging",
    "restore_python_exception_logging", "tool_directory",
]
