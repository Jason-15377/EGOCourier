# -*- coding: utf-8 -*-
"""导出进度条回归测试（不依赖真机、不需要显示环境）。

覆盖：
  T1  阶段占比表：各阶段区间互不重叠、单调递增、覆盖 0..100；
      每段子进度都落在自己区间内，多段共用一个回调时按份额切段；
  T2  UI 节流器：丢弃回退与重复值、0/100 立即生效、重置后旧值不再顶住新任务。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QProgressBar  # noqa: E402

_app = QApplication.instance() or QApplication([])

from ego_gui.device_tab import (  # noqa: E402
    PROGRESS_STAGES, _ProgressMeter, _stage_map, _sub, _ThrottledProgress,
)


class TestStageTable(unittest.TestCase):
    def test_ranges_are_contiguous_and_ordered(self):
        prev_hi = None
        for name in ("firmware", "stage", "mobile", "pack", "import"):
            lo, hi = PROGRESS_STAGES[name]
            self.assertLess(lo, hi, name)
            if prev_hi is not None:
                self.assertGreaterEqual(lo, prev_hi, f"{name} 与前一段重叠")
            prev_hi = hi
        self.assertEqual(PROGRESS_STAGES["firmware"][0], 0)
        self.assertEqual(PROGRESS_STAGES["import"][1], 100)

    def test_sub_progress_stays_inside_range(self):
        for name, (lo, hi) in PROGRESS_STAGES.items():
            values = [_stage_map(name, p) for p in range(0, 101, 5)]
            self.assertEqual(values[0], lo)
            self.assertLess(values[-1], hi)      # 满额也留 1 格给下一段
            self.assertEqual(values, sorted(values), name)

    def test_clamps_out_of_range_input(self):
        lo, hi = PROGRESS_STAGES["firmware"]
        self.assertEqual(_stage_map("firmware", -5), lo)
        self.assertLess(_stage_map("firmware", 500), hi)
        self.assertEqual(_stage_map("firmware", None), lo)
        self.assertEqual(_stage_map("firmware", "x"), lo)

    def test_reserve_caps_early_finish(self):
        """只跑手机阶段时要停在 import 之前，不能提前顶到 100。"""
        got = _stage_map("mobile", 100, reserve=PROGRESS_STAGES["import"][0])
        self.assertLess(got, PROGRESS_STAGES["import"][0])
        self.assertGreater(got, PROGRESS_STAGES["mobile"][0])

    def test_shared_meter_switches_stage(self):
        """pack_staged：前 60 属于 stage，后 40 属于 pack，不得回退。"""
        meter = _ProgressMeter([("stage", 60), ("pack", 40)])
        seq = [meter(p) for p in (0, 30, 60, 61, 80, 100)]
        self.assertEqual(seq, sorted(seq), seq)
        stage_lo, stage_hi = PROGRESS_STAGES["stage"]
        pack_lo, pack_hi = PROGRESS_STAGES["pack"]
        self.assertEqual(seq[0], stage_lo)
        self.assertLess(seq[2], stage_hi)
        self.assertGreaterEqual(seq[3], pack_lo)
        self.assertLess(seq[-1], pack_hi)

    def test_zero_share_is_ignored(self):
        """只勾固件时 stage 份额为 0，压缩进度应直接落到 pack 段。"""
        meter = _ProgressMeter([("stage", 0), ("pack", 40)])
        self.assertEqual(meter(0), PROGRESS_STAGES["pack"][0])
        self.assertGreater(meter(100), PROGRESS_STAGES["pack"][0])

    def test_sub_matches_meter(self):
        self.assertEqual(_sub([("stage", 60), ("pack", 40)])(62),
                         _ProgressMeter([("stage", 60), ("pack", 40)])(62))


class TestThrottle(unittest.TestCase):
    def setUp(self):
        _app.processEvents()
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.ctl = _ThrottledProgress(self.bar)

    def test_drops_regression(self):
        self.bar.setValue(40)
        self.ctl.set_value(30)
        self.assertEqual(self.bar.value(), 40)

    def test_drops_duplicate(self):
        self.bar.setValue(40)
        self.ctl.set_value(40)
        self.assertEqual(self.bar.value(), 40)

    def test_advances_and_ends_at_100(self):
        for v in (10, 20, 30, 100):
            self.ctl.set_value(v)
            self.ctl._flush()
        self.assertEqual(self.bar.value(), 100)

    def test_reset_accepts_lower_value_next_task(self):
        self.bar.setValue(100)
        self.ctl.reset()
        self.bar.setValue(0)
        self.ctl.set_value(15)
        self.ctl._flush()
        self.assertEqual(self.bar.value(), 15)

    def test_value_is_clamped(self):
        self.ctl.set_value(1000)
        self.ctl._flush()
        self.assertEqual(self.bar.value(), 100)
        self.ctl.reset()
        self.bar.setValue(0)
        self.ctl.set_value(-5)
        self.assertEqual(self.bar.value(), 0)


if __name__ == "__main__":
    unittest.main()
