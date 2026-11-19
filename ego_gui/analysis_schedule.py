"""日志分析定时调度：设置持久化、触发计算与 GUI 控制器。

定时分析只在 EGO Courier 运行期间生效（窗口可以最小化，但不要退出）。
设置保存在 ``data/analysis_schedule.json``；同一时间只允许一个实例执行
定时任务，避免两个窗口各自跑一遍。

时间计算全部使用本机本地时间（naive datetime），并按 wall clock 判断，
不依赖 QTimer 的累计间隔，因此睡眠唤醒、系统卡顿后仍能按真实时间补跑。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import QLockFile, QObject, QTimer, Signal

from ego_checker.runtime.paths import data_directory


SCHEDULE_FILE_NAME = "analysis_schedule.json"
SCHEDULE_LOCK_NAME = "analysis_schedule.lock"
DEFAULT_TIME = "02:00"
DEFAULT_GRACE_MINUTES = 120

MODE_MANUAL = "manual"
MODE_SCHEDULED = "scheduled"
REPEAT_DAILY = "daily"
REPEAT_ONCE = "once"


def _data_dir() -> Path:
    path = Path(data_directory())
    path.mkdir(parents=True, exist_ok=True)
    return path


def schedule_path() -> Path:
    return _data_dir() / SCHEDULE_FILE_NAME


def _valid_clock(text: Any) -> bool:
    try:
        datetime.strptime(str(text), "%H:%M")
        return True
    except (TypeError, ValueError):
        return False


def _valid_day(text: Any) -> bool:
    try:
        datetime.strptime(str(text), "%Y-%m-%d")
        return True
    except (TypeError, ValueError):
        return False


def _safe_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


@dataclass
class ScheduleSettings:
    """一次定时配置的完整快照（含分析参数）。"""

    mode: str = MODE_MANUAL
    enabled: bool = False
    repeat: str = REPEAT_DAILY
    time: str = DEFAULT_TIME
    date: Optional[str] = None
    out_root: str = ""
    independent_output: bool = True
    catch_up: bool = True
    grace_minutes: int = DEFAULT_GRACE_MINUTES
    params: dict = field(default_factory=dict)
    options: dict = field(default_factory=dict)
    data_roots: list[str] = field(default_factory=list)
    data_root: str = ""            # 兼容旧配置 / 单目录快捷字段
    last_trigger_for: Optional[str] = None
    last_run: dict = field(default_factory=dict)
    next_run: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Any) -> "ScheduleSettings":
        if not isinstance(raw, dict):
            return cls()
        mode = raw.get("mode")
        if mode not in (MODE_MANUAL, MODE_SCHEDULED):
            mode = MODE_MANUAL
        repeat = raw.get("repeat")
        if repeat not in (REPEAT_DAILY, REPEAT_ONCE):
            repeat = REPEAT_DAILY
        clock = str(raw.get("time") or DEFAULT_TIME)
        if not _valid_clock(clock):
            clock = DEFAULT_TIME
        one_day = raw.get("date")
        if repeat == REPEAT_ONCE:
            one_day = str(one_day) if one_day and _valid_day(one_day) else None
        else:
            one_day = None
        params = raw.get("params")
        options = raw.get("options")
        last_run = raw.get("last_run")
        raw_roots = raw.get("data_roots")
        roots: list[str] = []
        if isinstance(raw_roots, list):
            roots = [str(item).strip() for item in raw_roots
                     if isinstance(item, str) and str(item).strip()]
        legacy_root = str(raw.get("data_root") or "").strip()
        if not roots and legacy_root:
            roots = [legacy_root]
        seen: set[str] = set()
        roots = [item for item in roots if not (item in seen or seen.add(item))]
        data_root = legacy_root or (roots[0] if roots else "")
        return cls(
            mode=mode,
            enabled=bool(raw.get("enabled", False)),
            repeat=repeat,
            time=clock,
            date=one_day,
            data_roots=roots,
            data_root=data_root,
            out_root=str(raw.get("out_root") or ""),
            independent_output=bool(raw.get("independent_output", True)),
            catch_up=bool(raw.get("catch_up", True)),
            grace_minutes=_safe_int(raw.get("grace_minutes"),
                                    DEFAULT_GRACE_MINUTES, 0, 1440),
            params=params if isinstance(params, dict) else {},
            options=options if isinstance(options, dict) else {},
            last_trigger_for=(str(raw["last_trigger_for"])
                              if raw.get("last_trigger_for") else None),
            last_run=last_run if isinstance(last_run, dict) else {},
            next_run=str(raw["next_run"]) if raw.get("next_run") else None,
        )


def load_settings(path: Optional[Path] = None) -> ScheduleSettings:
    """读取定时配置；文件不存在或损坏时返回默认配置。

    损坏文件会改名归档（``analysis_schedule.bad-YYYYMMDD_HHMMSS.json``），
    避免下一次保存把现场配置直接覆盖掉。
    """
    target = Path(path) if path else schedule_path()
    if not target.is_file():
        return ScheduleSettings()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        try:
            bad = target.with_name(
                f"{target.stem}.bad-{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                f"{target.suffix}")
            os.replace(target, bad)
        except OSError:
            pass
        return ScheduleSettings()
    return ScheduleSettings.from_dict(raw)


def save_settings(settings: ScheduleSettings, path: Optional[Path] = None) -> None:
    """原子写入定时配置。"""
    target = Path(path) if path else schedule_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    payload = json.dumps(settings.to_dict(), ensure_ascii=False, indent=1)
    with open(tmp, "w", encoding="utf-8") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, target)


def _parse_clock(text: str) -> Optional[dt_time]:
    try:
        return datetime.strptime(str(text), "%H:%M").time()
    except (TypeError, ValueError):
        return None


def next_minute(now: Optional[datetime] = None) -> datetime:
    """返回下一个整分钟（秒/微秒归零）；23:59 跨到次日 00:00。"""
    current = now or datetime.now()
    return current.replace(second=0, microsecond=0) + timedelta(minutes=1)


def schedule_field_defaults(settings: ScheduleSettings, *,
                            from_disk: bool,
                            now: Optional[datetime] = None) -> tuple[str, str]:
    """给定时设置表单准备 ``(时间 HH:MM, 日期 YYYY-MM-DD)`` 初始值。

    全新配置（磁盘上没有 ``analysis_schedule.json``）的「仅一次」任务改用
    电脑当前时间（顺延到下一分钟，避免一保存就提示“时间已过”）。

    已保存的「仅一次」日期早于今天（例如旧版本遗留的 2016 年日期）时自动
    重置为今天/当前时刻；日期为空或损坏时同样按当前时间处理。每天模式
    保留配置里的触发时刻（没有配置时是 ``02:00``），日期字段不使用。
    """
    current = now or datetime.now()
    if settings.repeat != REPEAT_ONCE:
        return settings.time, ""
    saved_date_present = bool(from_disk and settings.date)
    date_text = settings.date if saved_date_present else current.strftime("%Y-%m-%d")
    try:
        saved_day = datetime.strptime(date_text, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        saved_day = None
    if saved_day is None or saved_day < current.date() or not saved_date_present:
        upcoming = next_minute(current)
        return upcoming.strftime("%H:%M"), upcoming.strftime("%Y-%m-%d")
    return settings.time, date_text


def occurrence_datetime(settings: ScheduleSettings,
                        day: Optional[date] = None) -> Optional[datetime]:
    """返回指定日期（默认今天）的计划触发时刻；仅一次任务用配置日期。"""
    clock = _parse_clock(settings.time)
    if clock is None:
        return None
    if settings.repeat == REPEAT_ONCE:
        if not settings.date:
            return None
        try:
            one_day = datetime.strptime(settings.date, "%Y-%m-%d").date()
        except ValueError:
            return None
        return datetime.combine(one_day, clock)
    return datetime.combine(day or datetime.now().date(), clock)


def next_occurrence(now: datetime,
                    settings: ScheduleSettings) -> Optional[datetime]:
    """严格晚于 ``now`` 的下一次计划时间；仅一次且已过时返回 None。"""
    if settings.mode != MODE_SCHEDULED:
        return None
    clock = _parse_clock(settings.time)
    if clock is None:
        return None
    if settings.repeat == REPEAT_ONCE:
        occurrence = occurrence_datetime(settings)
        return occurrence if occurrence and occurrence > now else None
    today = datetime.combine(now.date(), clock)
    return today if today > now else today + timedelta(days=1)


def due_occurrence(now: datetime,
                   settings: ScheduleSettings) -> Optional[datetime]:
    """返回当前应当触发的计划时刻，没有则 None。

    - 每天：只看今天的计划时刻；已过时是否补跑由 ``catch_up`` 与宽限期决定。
    - 仅一次：计划时刻已过，未超过宽限期仍可补跑。
    - ``last_trigger_for`` 命中时表示该时刻已经处理过，绝不重复触发。
    """
    if settings.mode != MODE_SCHEDULED or not settings.enabled:
        return None
    clock = _parse_clock(settings.time)
    if clock is None:
        return None
    if settings.repeat == REPEAT_ONCE:
        occurrence = occurrence_datetime(settings)
        if occurrence is None or now < occurrence:
            return None
    else:
        occurrence = datetime.combine(now.date(), clock)
        if now < occurrence:
            return None
    if settings.last_trigger_for == occurrence.isoformat(timespec="seconds"):
        return None
    grace = timedelta(minutes=max(0, int(settings.grace_minutes)))
    if settings.catch_up:
        if now - occurrence > grace:
            return None
    elif now - occurrence > timedelta(seconds=60):
        return None
    return occurrence


def format_dt(value: Any) -> str:
    if not value:
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    try:
        return datetime.fromisoformat(str(value)).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(value)


def format_duration(seconds: Any) -> str:
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "—"
    if total < 60:
        return f"{total} 秒"
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} 分 {sec} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分"


STATUS_LABELS = {
    "started": "进行中",
    "finished": "成功",
    "partial_success": "部分成功",
    "partial": "已停止（部分报告）",
    "failed": "失败",
    "skipped_busy": "已跳过（已有任务）",
    "missed": "已错过",
    "failed_to_start": "启动失败",
}


class ScheduleController(QObject):
    """GUI 内的定时分析控制器（30 秒心跳，只在主线程使用）。"""

    due = Signal(object)            # {"occurrence": str, "settings": dict}
    status_changed = Signal(str)
    log = Signal(str)

    TICK_MS = 30_000

    def __init__(self, parent=None, *,
                 now_fn: Optional[Callable[[], datetime]] = None,
                 path: Optional[Path] = None,
                 use_lock: bool = True,
                 start_timer: bool = True):
        super().__init__(parent)
        self._path = Path(path) if path else None
        self._now = now_fn or datetime.now
        self.settings = load_settings(self._path)
        self._busy = False
        self._pending_occurrence: Optional[str] = None
        self._lock: Optional[QLockFile] = None
        self._use_lock = use_lock
        self._lock_ok = not use_lock
        self._start_timer = start_timer
        self._timer = QTimer(self)
        self._timer.setInterval(self.TICK_MS)
        self._timer.timeout.connect(self.tick)
        self._refresh_next_run()

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """获取单实例锁并启动心跳；拿不到锁时本实例的定时自动停用。"""
        if self._use_lock and self._lock is None:
            lock = QLockFile(str(_data_dir() / SCHEDULE_LOCK_NAME))
            lock.setStaleLockTime(30_000)
            if lock.tryLock(0):
                self._lock = lock
                self._lock_ok = True
            else:
                self._lock_ok = False
                self.log.emit(
                    "另一个 EGO Courier 实例正在负责定时分析，本实例的定时已停用")
        if self._start_timer:
            self._timer.start()
        # 上次进程在分析中途退出时，last_run 会停在 started；本次拿到锁后
        # 明确标为失败，避免界面永远显示“进行中”。
        if (self._lock_ok and isinstance(self.settings.last_run, dict)
                and self.settings.last_run.get("status") == "started"):
            self.settings.last_run = {
                **self.settings.last_run,
                "status": "failed",
                "error": "上次运行未正常结束（程序退出或崩溃）",
            }
            self.persist()
            self.log.emit("定时分析：上次运行未正常结束，已标记为失败")
        self.tick()

    def stop(self) -> None:
        self._timer.stop()
        if self._lock is not None:
            if self._lock_ok:
                self._lock.unlock()
            self._lock = None
        self._lock_ok = not self._use_lock

    def set_busy(self, busy: bool) -> None:
        """由分析页告知当前是否有分析在跑，避免定时再起一份。"""
        self._busy = bool(busy)

    @property
    def available(self) -> bool:
        """本实例是否拿到了定时执行权（单实例锁）。"""
        return self._lock_ok

    # ------------------------------------------------------------ settings
    def persist(self) -> None:
        try:
            save_settings(self.settings, self._path)
        except OSError as exc:
            self.log.emit(f"保存定时设置失败：{exc}")

    def update_settings(self, settings: ScheduleSettings) -> None:
        self.settings = settings
        self._refresh_next_run()
        self.persist()
        self.status_changed.emit(self.status_text())

    def set_mode(self, mode: str) -> None:
        if mode not in (MODE_MANUAL, MODE_SCHEDULED):
            return
        self.settings.mode = mode
        self._refresh_next_run()
        self.persist()
        self.status_changed.emit(self.status_text())

    def set_enabled(self, enabled: bool) -> None:
        self.settings.enabled = bool(enabled)
        self._refresh_next_run()
        self.persist()
        self.status_changed.emit(self.status_text())

    # ------------------------------------------------------------ scheduling
    def _refresh_next_run(self) -> None:
        if self.settings.mode != MODE_SCHEDULED or not self.settings.enabled:
            self.settings.next_run = None
            return
        occurrence = next_occurrence(self._now(), self.settings)
        self.settings.next_run = (occurrence.isoformat(timespec="seconds")
                                  if occurrence else None)

    def status_text(self) -> str:
        if self.settings.mode != MODE_SCHEDULED:
            return "定时分析已暂停（目录分析模式）"
        if not self._lock_ok:
            return "定时不可用：另一个实例正在负责定时"
        if not self.settings.enabled:
            return "定时分析未启用"
        if self.settings.next_run:
            return f"定时分析已启用，下次运行 {format_dt(self.settings.next_run)}"
        return "定时分析已启用，等待下一次触发"

    def tick(self, now: Optional[datetime] = None) -> None:
        """心跳入口；``now`` 仅测试时传入。"""
        if not self._lock_ok:
            return
        current = now or self._now()
        if self.settings.mode != MODE_SCHEDULED or not self.settings.enabled:
            self._refresh_next_run()
            return
        # 仅一次任务：过了宽限期仍未执行，标记错过并停用，避免长期空转。
        if self.settings.repeat == REPEAT_ONCE:
            occurrence = occurrence_datetime(self.settings)
            if (occurrence is not None and current >= occurrence
                    and self.settings.last_trigger_for
                    != occurrence.isoformat(timespec="seconds")):
                grace = timedelta(minutes=max(0, int(self.settings.grace_minutes)))
                expired = (not self.settings.catch_up
                           or current - occurrence > grace)
                if expired:
                    self.settings.enabled = False
                    self.settings.last_run = {
                        "scheduled_for": occurrence.isoformat(timespec="seconds"),
                        "at": current.isoformat(timespec="seconds"),
                        "status": "missed",
                    }
                    self._refresh_next_run()
                    self.persist()
                    self.status_changed.emit(self.status_text())
                    self.log.emit("定时分析：仅一次的时间已过，已标记错过并停用")
                    return
        occurrence = due_occurrence(current, self.settings)
        if occurrence is None:
            self._refresh_next_run()
            return
        iso = occurrence.isoformat(timespec="seconds")
        self.settings.last_trigger_for = iso
        self._refresh_next_run()
        if self._busy:
            self.settings.last_run = {
                "scheduled_for": iso,
                "at": current.isoformat(timespec="seconds"),
                "status": "skipped_busy",
            }
            self.persist()
            self.status_changed.emit(self.status_text())
            self.log.emit(
                f"定时分析：{format_dt(iso)} 触发时已有分析任务，本次跳过")
            return
        self._pending_occurrence = iso
        self.settings.last_run = {
            "scheduled_for": iso,
            "at": current.isoformat(timespec="seconds"),
            "status": "started",
        }
        self.persist()
        self.status_changed.emit(self.status_text())
        self.log.emit(f"定时分析：到达 {format_dt(iso)}，开始执行")
        self.due.emit({"occurrence": iso, "settings": self.settings.to_dict()})

    def mark_finished(self, status: str, *,
                      occurrence: Optional[str] = None,
                      duration_s: Optional[float] = None,
                      report: str = "",
                      error: str = "",
                      runs: Optional[list] = None) -> None:
        """分析结束回调；只有带 occurrence 的定时任务才推进计划。"""
        self._busy = False
        if occurrence:
            entry = {
                "scheduled_for": occurrence,
                "finished_at": self._now().isoformat(timespec="seconds"),
                "status": status,
            }
            if duration_s is not None:
                entry["duration_s"] = round(float(duration_s), 1)
            if report:
                entry["report"] = report
            if error:
                entry["error"] = error
            if runs:
                entry["runs"] = runs
            self.settings.last_run = entry
            if self.settings.repeat == REPEAT_ONCE:
                self.settings.enabled = False
            self._pending_occurrence = None
            self._refresh_next_run()
            self.persist()
        self.status_changed.emit(self.status_text())

    def last_run_text(self) -> str:
        run = self.settings.last_run if isinstance(self.settings.last_run, dict) else {}
        if not run:
            return "上次运行：—"
        status = STATUS_LABELS.get(str(run.get("status")),
                                   str(run.get("status") or "—"))
        when = format_dt(run.get("scheduled_for") or run.get("at"))
        text = f"上次运行：{when} {status}"
        runs = run.get("runs") if isinstance(run.get("runs"), list) else []
        if len(runs) > 1:
            ok = sum(1 for item in runs
                     if isinstance(item, dict) and item.get("status") == "finished")
            text += f"（{ok}/{len(runs)} 个目录成功）"
        elif run.get("duration_s") is not None:
            text += f"（{format_duration(run['duration_s'])}）"
        return text
