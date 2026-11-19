# -*- coding: utf-8 -*-
"""定时分析调度的单元测试（不启动 GUI、不连设备）。

覆盖：下次触发计算、宽限期/补跑、重复触发抑制、单次任务过期、
设置持久化与损坏配置恢复、控制器 busy 跳过与完成回写。
"""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from ego_gui.analysis_schedule import (
    MODE_MANUAL,
    MODE_SCHEDULED,
    REPEAT_DAILY,
    REPEAT_ONCE,
    ScheduleController,
    ScheduleSettings,
    due_occurrence,
    load_settings,
    next_occurrence,
    save_settings,
    schedule_field_defaults,
)


def _daily(**kwargs):
    base = dict(mode=MODE_SCHEDULED, enabled=True, repeat=REPEAT_DAILY,
                time="02:00", grace_minutes=120, catch_up=True)
    base.update(kwargs)
    return ScheduleSettings(**base)


def _once(day="2026-11-17", **kwargs):
    base = dict(mode=MODE_SCHEDULED, enabled=True, repeat=REPEAT_ONCE,
                time="02:00", date=day, grace_minutes=120, catch_up=True)
    base.update(kwargs)
    return ScheduleSettings(**base)


class TestNextOccurrence(unittest.TestCase):
    def test_daily_before_and_after_time(self):
        settings = _daily()
        self.assertEqual(next_occurrence(datetime(2026, 11, 17, 1, 0), settings),
                         datetime(2026, 11, 17, 2, 0))
        self.assertEqual(next_occurrence(datetime(2026, 11, 17, 3, 0), settings),
                         datetime(2026, 11, 18, 2, 0))

    def test_daily_exact_time_rolls_to_tomorrow(self):
        settings = _daily()
        self.assertEqual(next_occurrence(datetime(2026, 11, 17, 2, 0), settings),
                         datetime(2026, 11, 18, 2, 0))

    def test_once_future_and_past(self):
        self.assertEqual(next_occurrence(datetime(2026, 11, 16, 23, 0), _once()),
                         datetime(2026, 11, 17, 2, 0))
        self.assertIsNone(next_occurrence(datetime(2026, 11, 17, 3, 0), _once()))

    def test_manual_mode_has_no_next_run(self):
        self.assertIsNone(next_occurrence(datetime(2026, 11, 17, 1, 0),
                                          _daily(mode=MODE_MANUAL)))


class TestDueOccurrence(unittest.TestCase):
    def test_daily_due_inside_grace(self):
        self.assertEqual(due_occurrence(datetime(2026, 11, 17, 2, 1), _daily()),
                         datetime(2026, 11, 17, 2, 0))

    def test_daily_before_time_not_due(self):
        self.assertIsNone(due_occurrence(datetime(2026, 11, 17, 1, 59), _daily()))

    def test_daily_after_grace_not_due(self):
        self.assertIsNone(due_occurrence(datetime(2026, 11, 17, 5, 0), _daily()))

    def test_last_trigger_for_suppresses_duplicate(self):
        settings = _daily(last_trigger_for="2026-11-17T02:00:00")
        self.assertIsNone(due_occurrence(datetime(2026, 11, 17, 2, 1), settings))

    def test_catch_up_disabled_requires_near_exact_time(self):
        settings = _daily(catch_up=False)
        self.assertIsNotNone(due_occurrence(datetime(2026, 11, 17, 2, 0, 30), settings))
        self.assertIsNone(due_occurrence(datetime(2026, 11, 17, 2, 1, 30), settings))

    def test_once_due_inside_grace_and_expired(self):
        self.assertEqual(due_occurrence(datetime(2026, 11, 17, 3, 0), _once()),
                         datetime(2026, 11, 17, 2, 0))
        self.assertIsNone(due_occurrence(datetime(2026, 11, 17, 5, 0), _once()))

    def test_disabled_or_manual_not_due(self):
        self.assertIsNone(due_occurrence(datetime(2026, 11, 17, 2, 1),
                                         _daily(enabled=False)))
        self.assertIsNone(due_occurrence(datetime(2026, 11, 17, 2, 1),
                                         _daily(mode=MODE_MANUAL)))


class TestSettingsPersistence(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "schedule.json"
            original = ScheduleSettings(
                mode=MODE_SCHEDULED, enabled=True, repeat=REPEAT_ONCE,
                time="03:30", date="2026-12-01",
                data_roots=[r"D:\data", r"D:\data2"],
                data_root=r"D:\data", out_root=r"D:\out",
                independent_output=False, catch_up=False, grace_minutes=30,
                params={"input_mode": "multi"}, options={"sei_align": True},
                last_trigger_for="2026-12-01T03:30:00",
                last_run={"status": "finished", "duration_s": 12.3},
                next_run="2026-12-01T03:30:00",
            )
            save_settings(original, path)
            loaded = load_settings(path)
            self.assertEqual(loaded.to_dict(), original.to_dict())

    def test_corrupt_json_falls_back_and_archives(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "schedule.json"
            path.write_text("{bad json", encoding="utf-8")
            loaded = load_settings(path)
            self.assertEqual(loaded.to_dict(), ScheduleSettings().to_dict())
            self.assertFalse(path.exists())
            self.assertTrue(list(Path(td).glob("schedule.bad-*.json")))

    def test_from_dict_sanitizes_invalid_values(self):
        loaded = ScheduleSettings.from_dict({
            "mode": "nonsense",
            "repeat": "weekly",
            "time": "99:99",
            "grace_minutes": 9999,
            "params": "not-a-dict",
        })
        self.assertEqual(loaded.mode, MODE_MANUAL)
        self.assertEqual(loaded.repeat, REPEAT_DAILY)
        self.assertEqual(loaded.time, "02:00")
        self.assertEqual(loaded.grace_minutes, 1440)
        self.assertEqual(loaded.params, {})

    def test_legacy_single_root_migrates_to_list(self):
        loaded = ScheduleSettings.from_dict({"data_root": r"D:\legacy"})
        self.assertEqual(loaded.data_roots, [r"D:\legacy"])
        self.assertEqual(loaded.data_root, r"D:\legacy")


class TestScheduleFieldDefaults(unittest.TestCase):
    """定时表单的“仅一次”时间默认值必须跟随电脑系统时间。"""

    def setUp(self):
        self.now = datetime(2026, 11, 19, 10, 30)

    def test_fresh_once_uses_next_minute(self):
        clock, day = schedule_field_defaults(
            ScheduleSettings(repeat=REPEAT_ONCE), from_disk=False, now=self.now)
        self.assertEqual(clock, "10:31")
        self.assertEqual(day, "2026-11-19")

    def test_stale_saved_once_date_refreshes(self):
        settings = ScheduleSettings(repeat=REPEAT_ONCE, date="2016-01-01",
                                    time="02:00")
        clock, day = schedule_field_defaults(
            settings, from_disk=True, now=self.now)
        self.assertEqual(clock, "10:31")
        self.assertEqual(day, "2026-11-19")

    def test_future_saved_once_values_are_preserved(self):
        settings = ScheduleSettings(repeat=REPEAT_ONCE, date="2026-12-01",
                                    time="08:15")
        clock, day = schedule_field_defaults(
            settings, from_disk=True, now=self.now)
        self.assertEqual(clock, "08:15")
        self.assertEqual(day, "2026-12-01")

    def test_once_without_date_uses_next_minute(self):
        settings = ScheduleSettings(repeat=REPEAT_ONCE, date=None, time="02:00")
        clock, day = schedule_field_defaults(
            settings, from_disk=True, now=self.now)
        self.assertEqual(clock, "10:31")
        self.assertEqual(day, "2026-11-19")

    def test_once_at_end_of_day_moves_to_tomorrow(self):
        clock, day = schedule_field_defaults(
            ScheduleSettings(repeat=REPEAT_ONCE), from_disk=False,
            now=datetime(2026, 11, 19, 23, 59, 30))
        self.assertEqual(clock, "00:00")
        self.assertEqual(day, "2026-11-20")

    def test_daily_keeps_configured_clock_and_no_date(self):
        clock, day = schedule_field_defaults(
            _daily(), from_disk=False, now=self.now)
        self.assertEqual(clock, "02:00")
        self.assertEqual(day, "")


class TestScheduleController(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "schedule.json"
        self.now = datetime(2026, 11, 17, 2, 1)
        self.ctl = ScheduleController(
            path=self.path, use_lock=False, start_timer=False,
            now_fn=lambda: self.now)
        self.hits = []
        self.ctl.due.connect(self.hits.append)

    def tearDown(self):
        self.tmp.cleanup()

    def _set_daily(self, **kwargs):
        self.ctl.settings = _daily(data_root=self.tmp.name, **kwargs)

    def test_due_emits_once_and_persists(self):
        self._set_daily()
        self.ctl.tick(self.now)
        self.assertEqual(len(self.hits), 1)
        self.assertEqual(self.hits[0]["occurrence"], "2026-11-17T02:00:00")
        self.assertEqual(self.ctl.settings.last_run["status"], "started")
        self.ctl.tick(self.now)
        self.assertEqual(len(self.hits), 1, "同一计划时刻不得重复触发")

    def test_busy_skips_and_records(self):
        self._set_daily()
        self.ctl.set_busy(True)
        self.ctl.tick(self.now)
        self.assertEqual(self.hits, [])
        self.assertEqual(self.ctl.settings.last_run["status"], "skipped_busy")

    def test_mark_finished_updates_last_and_next_run(self):
        self._set_daily()
        self.ctl.tick(self.now)
        occurrence = self.hits[0]["occurrence"]
        self.ctl.mark_finished("finished", occurrence=occurrence,
                               duration_s=12.3, report="report.html")
        self.assertEqual(self.ctl.settings.last_run["status"], "finished")
        self.assertEqual(self.ctl.settings.last_run["duration_s"], 12.3)
        self.assertEqual(self.ctl.settings.next_run, "2026-11-18T02:00:00")
        loaded = load_settings(self.path)
        self.assertEqual(loaded.last_run["status"], "finished")
        self.assertEqual(loaded.next_run, "2026-11-18T02:00:00")

    def test_once_finished_disables(self):
        self.ctl.settings = _once(data_root=self.tmp.name)
        self.ctl.tick(self.now)
        self.ctl.mark_finished("finished", occurrence=self.hits[0]["occurrence"])
        self.assertFalse(self.ctl.settings.enabled)
        self.assertIsNone(self.ctl.settings.next_run)

    def test_once_expired_marks_missed(self):
        self.ctl.settings = _once(day="2026-11-16", data_root=self.tmp.name)
        self.ctl.tick(self.now)
        self.assertEqual(self.hits, [])
        self.assertFalse(self.ctl.settings.enabled)
        self.assertEqual(self.ctl.settings.last_run["status"], "missed")

    def test_manual_or_disabled_does_not_emit(self):
        self._set_daily(mode=MODE_MANUAL)
        self.ctl.tick(self.now)
        self._set_daily(enabled=False)
        self.ctl.tick(self.now)
        self.assertEqual(self.hits, [])

    def test_stale_started_marked_failed_on_start(self):
        self.ctl.settings.last_run = {
            "scheduled_for": "2026-11-16T02:00:00", "status": "started"}
        self.ctl.start()
        self.assertEqual(self.ctl.settings.last_run["status"], "failed")
        self.assertIn("未正常结束", self.ctl.settings.last_run["error"])

    def test_multi_directory_last_run_text(self):
        self.ctl.settings.last_run = {
            "scheduled_for": "2026-11-17T02:00:00",
            "status": "partial_success",
            "runs": [
                {"root": r"D:\a", "status": "finished"},
                {"root": r"D:\b", "status": "failed"},
            ],
        }
        text = self.ctl.last_run_text()
        self.assertIn("部分成功", text)
        self.assertIn("1/2 个目录成功", text)

    def test_mark_finished_keeps_runs_detail(self):
        self._set_daily()
        self.ctl.tick(self.now)
        runs = [{"root": r"D:\a", "status": "finished"}]
        self.ctl.mark_finished("partial_success",
                               occurrence=self.hits[0]["occurrence"],
                               runs=runs)
        self.assertEqual(self.ctl.settings.last_run["runs"], runs)


if __name__ == "__main__":
    unittest.main()
