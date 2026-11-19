"""会话记录登记簿（ego_relay.history）单元测试。

不启动 GUI：验证去重、排序、损坏归档、备注、判定与文件缺失标记等，
全部写入临时 EGO_DATA_ROOT，不碰真实 data/ 目录。
"""

import json
import os
import tempfile
import unittest
from pathlib import Path


class HistoryStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["EGO_DATA_ROOT"] = self._tmp.name
        # config 在导入时就把 DATA_ROOT 固化了，必须 reload 才让它看到临时目录。
        import importlib

        from ego_relay import config
        importlib.reload(config)
        from ego_relay import history
        importlib.reload(history)
        self.history = history

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop("EGO_DATA_ROOT", None)
        import importlib

        from ego_relay import config, history
        importlib.reload(config)
        importlib.reload(history)

    def test_record_and_load_newest_first(self):
        h = self.history
        first = h.record_analysis(kind="single", root="D:/a", out_root="D:/a",
                                  html_path="D:/a/r.html", status="finished")
        second = h.record_analysis(kind="flat", root="D:/b", out_root="D:/b",
                                   html_path="D:/b/r.html", status="partial")
        rows = h.load_sessions()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["id"], second["id"])
        self.assertEqual(rows[1]["id"], first["id"])
        self.assertTrue(h.SESSIONS_JSON.exists())

    def test_duplicate_callback_deduped(self):
        h = self.history
        a = h.record_analysis(kind="single", root="D:/a", out_root="D:/a",
                              html_path="D:/a/r.html", status="finished")
        b = h.record_analysis(kind="single", root="D:/a", out_root="D:/a",
                              html_path="D:/a/r.html", status="finished")
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(h.load_sessions()), 1)

    def test_record_note(self):
        h = self.history
        entry = h.record_analysis(kind="single", root="D:/a", out_root="D:/a",
                                  html_path="D:/a/r.html", status="finished",
                                  note="定时任务")
        self.assertEqual(entry["note"], "定时任务")
        self.assertEqual(h.load_sessions()[0]["note"], "定时任务")

    def test_prune_marks_missing_report_and_root(self):
        h = self.history
        h.record_analysis(kind="single", root="D:/nope", out_root="D:/nope",
                          html_path="D:/nope/r.html", status="failed")
        rows = h.load_sessions()
        self.assertEqual(h.prune_missing(rows), 1)
        self.assertTrue(rows[0]["_html_missing"])
        self.assertTrue(rows[0]["_root_missing"])

    def test_update_note_and_reject_unknown_field(self):
        h = self.history
        entry = h.record_analysis(kind="single", root="D:/a", out_root="D:/a",
                                  html_path="", status="finished")
        h.update_session(entry["id"], note="现场备注")
        self.assertEqual(h.load_sessions()[0]["note"], "现场备注")
        with self.assertRaises(ValueError):
            h.update_session(entry["id"], id="hacked")

    def test_delete_keeps_report_file(self):
        h = self.history
        report = Path(self._tmp.name) / "r.html"
        report.write_text("x", encoding="utf-8")
        entry = h.record_analysis(kind="single", root="D:/a", out_root="D:/a",
                                  html_path=str(report), status="finished")
        self.assertTrue(h.delete_session(entry["id"], delete_files=False))
        self.assertTrue(report.exists(), "默认删除记录不应动报告文件")
        self.assertEqual(h.load_sessions(), [])

    def test_delete_with_files_removes_report_only(self):
        h = self.history
        data_root = Path(self._tmp.name) / "capture"
        data_root.mkdir()
        report = Path(self._tmp.name) / "r.html"
        report.write_text("x", encoding="utf-8")
        entry = h.record_analysis(kind="single", root=str(data_root),
                                  out_root=str(data_root), html_path=str(report),
                                  status="finished")
        h.delete_session(entry["id"], delete_files=True)
        self.assertFalse(report.exists())
        self.assertTrue(data_root.is_dir(), "数据目录永远不能被删除")

    def test_corrupt_file_is_archived_not_raised(self):
        h = self.history
        h.SESSIONS_JSON.parent.mkdir(parents=True, exist_ok=True)
        h.SESSIONS_JSON.write_text("{not json", encoding="utf-8")
        self.assertEqual(h.load_sessions(), [])
        archived = list(h.SESSIONS_JSON.parent.glob("sessions.bad-*.json"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_text(encoding="utf-8"), "{not json")

    def test_find_sessions_filters(self):
        h = self.history
        h.record_analysis(kind="single", root="D:/a", out_root="D:/a",
                          html_path="", status="finished")
        h.record_analysis(kind="single", root="D:/b", out_root="D:/b",
                          html_path="", status="failed")
        self.assertEqual(len(h.find_sessions(status="failed")), 1)
        self.assertEqual(len(h.find_sessions(root="D:/a")), 1)


if __name__ == "__main__":
    unittest.main()
