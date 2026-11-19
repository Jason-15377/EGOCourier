# -*- coding: utf-8 -*-
"""增量导出（只导新增日志）的单元测试。

覆盖：按 EGOViewer 根目录隔离的记录读写、任一分类缺记录时退回全量、
记录取最早起点、损坏状态文件回落，以及 DeviceTab 的筛选起点/留空保护。
"""
import os
import tempfile
import unittest
from pathlib import Path

from ego_relay import export_state


class TestExportState(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "export_incremental.json"
        self.root = str(Path(self._tmp.name) / "EGOViewer")

    def tearDown(self):
        self._tmp.cleanup()

    def test_round_trip_per_root_and_clear(self):
        stamp = 1_760_000_000.0
        export_state.set_watermarks(self.root, ["app", "sdk"], stamp,
                                    export_dir="D:/out/x", path=self.path)
        marks = export_state.get_watermarks(self.root, ["app", "sdk"],
                                            path=self.path)
        self.assertEqual(marks, {"app": stamp, "sdk": stamp})
        self.assertEqual(
            export_state.effective_watermark(self.root, ["app"], self.path), stamp)
        self.assertTrue(export_state.clear_root(self.root, path=self.path))
        self.assertEqual(
            export_state.get_watermarks(self.root, ["app"], self.path), {})
        self.assertFalse(export_state.clear_root(self.root, path=self.path))

    def test_any_missing_category_forces_full(self):
        export_state.set_watermarks(self.root, ["app"], 100.0, path=self.path)
        self.assertIsNone(
            export_state.effective_watermark(self.root, ["app", "sdk"], self.path))
        self.assertEqual(
            export_state.effective_watermark(self.root, ["app"], self.path), 100.0)

    def test_oldest_of_all_categories(self):
        export_state.set_watermarks(self.root, ["app"], 200.0, path=self.path)
        export_state.set_watermarks(self.root, ["sdk"], 100.0, path=self.path)
        self.assertEqual(
            export_state.effective_watermark(self.root, ["app", "sdk"], self.path),
            100.0)

    def test_corrupt_state_falls_back_empty(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(
            export_state.get_watermarks(self.root, ["app"], self.path), {})


class TestDeviceTabIncrementalFilter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PySide6.QtCore import QDateTime
        from ego_gui import device_tab as dt

        self.QDateTime = QDateTime
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._tmp.name, "EGOViewer")
        os.makedirs(self.root, exist_ok=True)
        self._orig_state_path = export_state.state_path
        export_state.state_path = lambda: Path(self._tmp.name) / "state.json"
        self._orig_rescan = dt.DeviceTab._rescan_ev_roots
        dt.DeviceTab._rescan_ev_roots = lambda self, interactive=False: None
        self.tab = dt.DeviceTab(lambda m: None, None)
        self.tab._current_ev_root = lambda: self.root
        self.tab.edit_range_start.setDateTime(
            QDateTime.fromSecsSinceEpoch(1_600_000_000))
        self.tab.edit_range_end.setDateTime(QDateTime.currentDateTime())
        self.tab.chk_incremental.setChecked(True)

    def tearDown(self):
        from ego_gui import device_tab as dt

        dt.DeviceTab._rescan_ev_roots = self._orig_rescan
        export_state.state_path = self._orig_state_path
        self.tab.deleteLater()
        self._tmp.cleanup()

    def test_first_run_full_then_next_uses_watermark(self):
        filters = self.tab._current_filters()
        effective, ctx = self.tab._apply_incremental(dict(filters), ["app", "sdk"])
        self.assertIsNotNone(ctx)
        self.assertEqual(effective["start"], filters["start"])

        self.tab._incremental_ctx = ctx
        self.tab._commit_incremental("D:/out/EGO_export_x")
        marks = export_state.get_watermarks(self.root, ["app", "sdk"])
        self.assertEqual(marks["app"], ctx["stamp"])
        self.assertEqual(marks["sdk"], ctx["stamp"])

        filters2 = self.tab._current_filters()
        effective2, ctx2 = self.tab._apply_incremental(
            dict(filters2), ["app", "sdk"])
        self.assertEqual(effective2["start"], ctx["stamp"])
        self.assertIsNotNone(ctx2)

    def test_gap_keeps_old_watermark(self):
        export_state.set_watermarks(self.root, ["app", "sdk"], 1_700_000_000.0)
        self.tab.edit_range_start.setDateTime(
            self.QDateTime.fromSecsSinceEpoch(1_700_000_000 + 3600))
        filters = self.tab._current_filters()
        effective, ctx = self.tab._apply_incremental(dict(filters), ["app", "sdk"])
        self.assertIsNone(ctx)
        self.assertEqual(effective["start"], filters["start"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
