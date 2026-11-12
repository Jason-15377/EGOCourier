# -*- coding: utf-8 -*-
"""导出失败后「取消任务」必须能结束任务并清空进度条（v1.0.9 缺陷修复）。

现场：手机日志拉取失败（adb: device not found）后进度条停在失败那一刻的百分比，
点「取消任务」只写一行"没有运行中的任务"就没了，进度条永远卡着。

根因是 _on_export_fail / _on_pack_fail 只写日志、不碰进度条，而 _stop() 在
worker 已结束（isRunning() 为假）时不走任何复位分支。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ego_gui import device_tab as dt  # noqa: E402


class _StubBar:
    def __init__(self):
        self._v = 0

    def setValue(self, v):
        self._v = int(v)

    def value(self):
        return self._v


class _StubThrottle:
    def reset(self):
        pass


class _StubButton:
    def __init__(self):
        self.enabled = True

    def setEnabled(self, v):
        self.enabled = bool(v)


def _bare_tab():
    """不构造整个界面（会去扫全盘 EGOViewer），只装配本组测试要用的属性。"""
    t = dt.DeviceTab.__new__(dt.DeviceTab)
    t.progress = _StubBar()
    t._progress_ui = _StubThrottle()
    t._append = lambda *_a, **_k: None
    t.log = lambda *_a, **_k: None
    t.btn_export = _StubButton()
    t.btn_pack = _StubButton()
    t.btn_stop = _StubButton()
    return t


class TestProgressClearedAfterTaskEnds(unittest.TestCase):
    def test_export_fail_clears_progress(self):
        t = _bare_tab()
        t.progress.setValue(87)
        t._on_export_fail("adb: error: failed to get feature set: device not found")
        self.assertEqual(t.progress.value(), 0)

    def test_export_fail_does_not_restart_on_unrelated_error(self):
        """只有"时间段内无日志"才弹放宽提示；设备掉线不该再起一轮导出。"""
        t = _bare_tab()
        started = []
        t._widen_range_to_all = lambda: started.append("widen")
        t._start_export = lambda: started.append("start")
        t._on_export_fail("adb: device not found")
        self.assertEqual(started, [])

    def test_pack_fail_clears_progress(self):
        t = _bare_tab()
        t.progress.setValue(64)
        t._on_pack_fail("boom")
        self.assertEqual(t.progress.value(), 0)

    def test_stop_after_failure_resets_ui(self):
        """worker 已结束时点「取消任务」也要完成复位，而不是只写一行日志。"""
        t = _bare_tab()
        t._export_worker = None
        t._pack_worker = None
        t._dp_worker = None
        t.progress.setValue(55)
        t.btn_stop.setEnabled(True)
        t.btn_export.setEnabled(False)
        t._stop()
        self.assertEqual(t.progress.value(), 0)
        self.assertFalse(t.btn_stop.enabled)
        self.assertTrue(t.btn_export.enabled)

    def test_cancelled_clears_progress(self):
        t = _bare_tab()
        t.progress.setValue(92)
        t._on_export_cancelled()
        self.assertEqual(t.progress.value(), 0)


class TestPartialExportCleanup(unittest.TestCase):
    """取消/失败要删掉本次的半成品目录，但绝不能碰输出根目录下别的东西。"""

    def _worker(self):
        w = dt.EgoOneClickExportWorker.__new__(dt.EgoOneClickExportWorker)
        w.status = type("S", (), {"emit": staticmethod(lambda *_a: None)})()
        return w

    def test_discard_removes_only_target_dir(self):
        w = self._worker()
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "EGO_export_20261110_100000")
            os.makedirs(os.path.join(target, "Pixel 7"))
            keep = os.path.join(d, "EGO_export_20261109_090000")
            os.makedirs(keep)
            w._discard_partial(target)
            self.assertFalse(os.path.exists(target))
            self.assertTrue(os.path.isdir(keep))

    def test_discard_missing_dir_is_harmless(self):
        w = self._worker()
        w._discard_partial(os.path.join(tempfile.gettempdir(), "no-such-dir-xyz"))


if __name__ == "__main__":
    unittest.main()
