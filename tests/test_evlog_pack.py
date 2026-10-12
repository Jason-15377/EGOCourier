# -*- coding: utf-8 -*-
"""EGOViewer 日志收集/筛选的回归测试（不依赖真机与 EGOViewer 安装）。

重点回归：日志文件名里的时间戳是"会话开始时刻"，而 EGOViewer/OrbbecSDK 的会话可
跨天持续写入。修复前只看文件名时间，会把"开始于窗口之前、但仍在窗口内写入"的长会话
整份丢弃（现象：勾选了 app/sdk，一键导出却只有 firmware）。这里用临时文件人为构造
"开始于昨天、mtime 在今天"的会话，断言其被正确保留。
"""
import os
import tempfile
import time
import unittest
from datetime import datetime, time as dtime

from ego_relay import evlog_pack as ep


def _ts(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s).timestamp()


def _touch(path, mtime):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("x")
    os.utime(path, (mtime, mtime))


class TestCrossDaySession(unittest.TestCase):
    """跨天长会话：文件名时间在窗口外，但 mtime 在窗口内，应被保留。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.logs = os.path.join(self.root, "data", "logs")
        self.sdk = os.path.join(self.root, "Log")
        os.makedirs(self.logs, exist_ok=True)
        os.makedirs(self.sdk, exist_ok=True)

        # 窗口 = 今天 00:00 ~ 今天 12:00
        self.win_start = _ts(2026, 10, 10, 0, 0, 0)
        self.win_end = _ts(2026, 10, 10, 12, 0, 0)

    def tearDown(self):
        self._tmp.cleanup()

    def test_cross_day_session_kept(self):
        # app：会话始于昨天 17:50，仍在写（mtime 今天 10:41）→ 应保留
        cross = os.path.join(self.logs, "EGOViewer_20261009175055.log")
        _touch(cross, _ts(2026, 10, 10, 10, 41, 0))

        # sdk：会话始于昨天 17:50，仍在写（mtime 今天 10:47）→ 应保留
        cross_sdk = os.path.join(self.sdk, "OrbbecSDK.20261009175056.log.txt")
        _touch(cross_sdk, _ts(2026, 10, 10, 10, 47, 0))

        c = ep.collect(self.root, ["app", "sdk"],
                       start=self.win_start, end=self.win_end)
        self.assertEqual(len(c["app"]), 1, "跨天 app 会话应保留")
        self.assertEqual(os.path.basename(c["app"][0]["rel"]), "EGOViewer_20261009175055.log")
        self.assertEqual(len(c["sdk"]), 1, "跨天 sdk 会话应保留")
        self.assertEqual(os.path.basename(c["sdk"][0]["rel"]),
                         "OrbbecSDK.20261009175056.log.txt")

    def test_finished_old_session_excluded(self):
        # 会话始于昨天 17:06，昨天 17:49 已结束（mtime 也在昨天）→ 应排除
        old = os.path.join(self.logs, "EGOViewer_20261009170634.log")
        _touch(old, _ts(2026, 10, 9, 17, 49, 0))

        c = ep.collect(self.root, ["app"], start=self.win_start, end=self.win_end)
        self.assertEqual(len(c["app"]), 0, "已结束的旧会话不应保留")

    def test_session_starting_inside_window_kept(self):
        # 会话始于今天 09:13，仍在写 → 保留（常规情形，不能被回归破坏）
        cur = os.path.join(self.logs, "EGOViewer_20261010091339.log")
        _touch(cur, _ts(2026, 10, 10, 10, 41, 0))

        c = ep.collect(self.root, ["app"], start=self.win_start, end=self.win_end)
        self.assertEqual(len(c["app"]), 1)

    def test_session_starting_after_window_excluded(self):
        # 会话始于今天 13:00，晚于窗口止 → 排除
        fut = os.path.join(self.logs, "EGOViewer_20261010130000.log")
        _touch(fut, _ts(2026, 10, 10, 13, 30, 0))

        c = ep.collect(self.root, ["app"], start=self.win_start, end=self.win_end)
        self.assertEqual(len(c["app"]), 0)

    def test_open_ended_window(self):
        # 只给 start、不给 end：跨越 start 的长会话（mtime 更晚）应保留
        cross = os.path.join(self.logs, "EGOViewer_20261009175055.log")
        _touch(cross, _ts(2026, 10, 10, 10, 41, 0))
        c = ep.collect(self.root, ["app"], start=self.win_start, end=None)
        self.assertEqual(len(c["app"]), 1)


class TestFileStamp(unittest.TestCase):
    def test_interval_from_name_and_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "EGOViewer_20261009175055.log")
            _touch(p, _ts(2026, 10, 10, 10, 41, 0))
            start, end, from_name = ep._file_stamp(p, "EGOViewer_20261009175055.log")
            self.assertTrue(from_name)
            self.assertEqual(start, _ts(2026, 10, 9, 17, 50, 55))
            self.assertEqual(end, _ts(2026, 10, 10, 10, 41, 0))
            self.assertGreaterEqual(end, start)

    def test_no_name_stamp_uses_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "orbbec.log")
            _touch(p, _ts(2026, 10, 10, 10, 41, 0))
            start, end, from_name = ep._file_stamp(p, "orbbec.log")
            self.assertFalse(from_name)
            self.assertEqual(start, end)


class TestOverlap(unittest.TestCase):
    def test_overlap_semantics(self):
        s, e = _ts(2026, 10, 9, 17, 0, 0), _ts(2026, 10, 10, 10, 0, 0)
        w_start, w_end = _ts(2026, 10, 10, 0, 0, 0), _ts(2026, 10, 10, 12, 0, 0)
        self.assertTrue(ep._overlaps(s, e, w_start, w_end))
        # 刚好在窗口起点处结束 → 命中（闭区间）
        self.assertTrue(ep._overlaps(s, w_start, w_start, w_end))
        # 早于窗口起点结束 → 不命中
        self.assertFalse(ep._overlaps(s, w_start - 1, w_start, w_end))
        # 晚于窗口止开始 → 不命中
        self.assertFalse(ep._overlaps(w_end + 1, w_end + 2, w_start, w_end))


class TestFilterFirmwarePull(unittest.TestCase):
    def test_pull_within_window_kept(self):
        pulled = _ts(2026, 10, 10, 10, 0, 0)
        kept, stamp, within = ep.filter_firmware_pull(
            ["a.log"], pulled,
            start=_ts(2026, 10, 10, 0, 0, 0), end=_ts(2026, 10, 10, 12, 0, 0))
        self.assertTrue(within)
        self.assertEqual(kept, ["a.log"])

    def test_pull_outside_window_dropped(self):
        pulled = _ts(2026, 10, 10, 13, 0, 0)
        kept, stamp, within = ep.filter_firmware_pull(
            ["a.log"], pulled,
            start=_ts(2026, 10, 10, 0, 0, 0), end=_ts(2026, 10, 10, 12, 0, 0))
        self.assertFalse(within)
        self.assertEqual(kept, [])

    def test_end_tolerance(self):
        # 拉取时刻比 end 晚 1 秒，容差 2 秒内应命中
        end = _ts(2026, 10, 10, 12, 0, 0)
        kept, stamp, within = ep.filter_firmware_pull(
            ["a.log"], end + 1,
            start=_ts(2026, 10, 10, 0, 0, 0), end=end)
        self.assertTrue(within)


class TestStageCrossDay(unittest.TestCase):
    """stage 落盘结果应包含跨天会话文件，即"一键导出"最终产物。"""

    def test_stage_copies_cross_day_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = tmp
            logs = os.path.join(root, "data", "logs")
            sdk = os.path.join(root, "Log")
            cross = os.path.join(logs, "EGOViewer_20261009175055.log")
            cross_sdk = os.path.join(sdk, "OrbbecSDK.20261009175056.log.txt")
            _touch(cross, _ts(2026, 10, 10, 10, 41, 0))
            _touch(cross_sdk, _ts(2026, 10, 10, 10, 47, 0))

            out = os.path.join(root, "EGO_export_test")
            _dest, collected = ep.stage(
                root, out, ["app", "sdk"],
                start=_ts(2026, 10, 10, 0, 0, 0), end=_ts(2026, 10, 10, 12, 0, 0))

            self.assertEqual(len(collected["app"]), 1)
            self.assertEqual(len(collected["sdk"]), 1)
            self.assertTrue(os.path.isfile(os.path.join(out, "app",
                                                        "EGOViewer_20261009175055.log")))
            self.assertTrue(os.path.isfile(os.path.join(out, "sdk",
                                                        "OrbbecSDK.20261009175056.log.txt")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
