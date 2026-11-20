"""问题 Part 汇总与 HTML 快速定位锚点测试（不启动 GUI）。"""

import tempfile
import unittest
from pathlib import Path

from ego_checker.domain.models import (
    FrameCountResult,
    PartResult,
    RowAlignResult,
)
from ego_checker.reporting import outputs as rep


def _pass_part(name):
    part = PartResult(name=name, path=Path(name))
    part.checks = {"frame_count": True}
    part.frame_count = FrameCountResult(
        counts={"left": 10, "right": 10}, passed=True)
    part.audio_enabled = False
    return part


def _fail_part(name):
    part = PartResult(name=name, path=Path(name))
    part.checks = {"frame_count": False}
    part.frame_count = FrameCountResult(
        counts={"left": 10, "right": 8}, count_diff=2,
        first_row_diff_us=1200, passed=False)
    part.audio_enabled = False
    return part


def _unverified_part(name):
    part = PartResult(name=name, path=Path(name))
    part.frame_count = FrameCountResult(
        counts={"left": 10, "right": 10}, passed=True)
    part.row_align = RowAlignResult(passed=True)
    part.audio_enabled = False
    return part


class ProblemPartsTest(unittest.TestCase):
    def test_lists_only_unpassed_parts(self):
        ok = _pass_part("ok_part")
        fail = _fail_part("fail_part")
        waived = _fail_part("waived_part")
        waived.whitelisted = True
        unverified = _unverified_part("unverified_part")
        error = PartResult(name="error_part", path=Path("error_part"))
        error.error = "源文件读取失败"
        error.audio_enabled = False

        problems = rep.problem_parts([ok, fail, waived, unverified, error])

        self.assertEqual([item["index"] for item in problems], [1, 3, 4])
        self.assertEqual([item["name"] for item in problems],
                         ["fail_part", "unverified_part", "error_part"])
        self.assertEqual([item["status"] for item in problems],
                         ["FAIL", "UNVERIFIED", "ERROR"])
        self.assertTrue(problems[0]["reasons"])
        self.assertTrue(any("相机帧数对齐" in header
                            for header, _text in problems[0]["reasons"]))
        self.assertEqual(problems[2]["reasons"][0][0], "执行异常")

    def test_empty_results(self):
        self.assertEqual(rep.problem_parts([]), [])

    def test_html_contains_row_anchors_and_quick_links(self):
        results = [_pass_part("ok_part"), _fail_part("fail_part")]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.html"
            rep.generate_html(results, out, Path(tmp))
            html = out.read_text(encoding="utf-8")

        self.assertIn("问题快速定位", html)
        self.assertIn("href='#part-2'", html)
        self.assertIn("id='part-1'", html)
        self.assertIn("id='part-2'", html)
        self.assertIn("fail_part", html)
        # 正常 Part 不应出现在快速定位卡片里。
        nav = html.split("问题快速定位", 1)[1].split("<h2>", 1)[0]
        self.assertNotIn("ok_part", nav)


if __name__ == "__main__":
    unittest.main()
