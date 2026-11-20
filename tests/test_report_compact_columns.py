# -*- coding: utf-8 -*-
"""HTML 报告明细表的紧凑列布局。

现场反馈：明细表每个相机一列，长文本又把列撑得很宽，必须左右横移。
HTML 报告因此改用紧凑列：同一检查项的每相机结果在单元格内纵向分行，
CSV 与窗口结果表仍保留完整列。
"""
import tempfile
import unittest
from pathlib import Path

from ego_checker.domain.models import (
    CameraData,
    DropResult,
    FrameCountResult,
    IntervalResult,
    PartResult,
    RowAlignResult,
    SyncResult,
)
from ego_checker.reporting import outputs as rep
from ego_checker.reporting.columns import ReportColumns


ROLES = {"left_rgb": "RGB", "side_left": "左目", "side_right": "右目"}


def _camera_part():
    part = PartResult(name="part0001", path=Path("part0001"))
    part.data_folder = "UMI_Finger-R_BF8LA60006M_20261118_144122"
    part.cameras = {
        role: CameraData(role=role, label=label, file=Path(f"{role}.csv"),
                         timestamps=[index * 33333 for index in range(10)],
                         n_rows=10)
        for role, label in ROLES.items()
    }
    part.fps = 30.0
    part.firmware = "v1.0.13"
    part.video_duration_by_role = {role: 180.63 for role in ROLES}
    part.audio_enabled = True
    part.frame_count = FrameCountResult(
        counts={role: 10 for role in ROLES}, passed=True,
        first_row_diff_us=142, last_row_diff_us=134)
    part.intervals = {
        role: IntervalResult(role=role, max_dev_us=52, mean_dev_us=10, passed=True)
        for role in ROLES
    }
    part.cam_sync = {
        role: SyncResult(matched_frames=10, passed=True, max_diff_us=178)
        for role in ROLES
    }
    part.imu_sync = {
        role: SyncResult(mode="imu", matched_frames=10, passed=True, max_diff_us=496)
        for role in ROLES
    }
    part.grip_sync = {
        role: SyncResult(mode="grip", matched_frames=10, passed=True, max_diff_us=8973)
        for role in ROLES
    }
    part.rgb_drop = {
        role: DropResult(role=role, total_lost_frames=0, passed=True)
        for role in ROLES
    }
    part.imu_drop = DropResult(total_lost_frames=0, total_frames=365712, passed=True)
    part.grip_drop = DropResult(total_lost_frames=0, passed=True)
    part.imu_groups_diff = SyncResult(matched_frames=10, max_diff_us=100, mean_diff_us=50)
    part.row_align = RowAlignResult(passed=True, max_diff_us=178)
    part.checks = {"frame_count": True}
    part.checks_completed = True
    return part


class CompactReportColumnsTest(unittest.TestCase):
    def test_default_columns_keep_per_camera_layout(self):
        headers = ReportColumns([_camera_part()]).headers()
        self.assertIn("左目帧间隔波动(ms)", headers)
        self.assertIn("IMU与左目同步", headers)
        self.assertIn("夹爪与左目同步", headers)

    def test_compact_columns_stack_values_vertically(self):
        part = _camera_part()
        cols = ReportColumns([part], compact=True)
        headers = cols.headers()
        cells = dict(zip(headers, cols.row_cells(part)))

        self.assertIn("帧间隔波动(ms)", headers)
        self.assertIn("相机 / 帧率 / 固件", headers)
        self.assertIn("丢帧", headers)
        self.assertNotIn("左目帧间隔波动(ms)", headers)

        interval = cells["帧间隔波动(ms)"]
        self.assertEqual(interval.count("\n"), 2)
        self.assertIn("左目: PASS", interval)
        self.assertIn("右侧目: PASS", interval)

        camera = cells["相机 / 帧率 / 固件"]
        self.assertEqual(camera.count("\n"), 2)
        self.assertIn("帧率: 30.0", camera)
        self.assertIn("固件: v1.0.13", camera)

    def test_html_uses_compact_columns_and_wraps_cells(self):
        part = _camera_part()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.html"
            rep.generate_html([part], out, Path(tmp))
            html = out.read_text(encoding="utf-8")

        self.assertIn("white-space:normal", html)
        self.assertNotIn("white-space:nowrap", html)
        self.assertIn("contain:inline-size", html)
        self.assertIn("帧间隔波动(ms)", html)
        self.assertNotIn("左目帧间隔波动(ms)", html)

    def test_absolute_paths_are_shortened_with_full_path_in_title(self):
        part = _camera_part()
        path = r"E:\very\long\session\part0001\device_camera_color_part0001_pts.csv"
        part.error = "相机输入未完整读取：color_rgb: " + path
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.html"
            rep.generate_html([part], out, Path(tmp))
            html = out.read_text(encoding="utf-8")

        self.assertIn(r"…\device_camera_color_part0001_pts.csv", html)
        self.assertIn(path, html, "完整路径必须保留在 title 提示中")


if __name__ == "__main__":
    unittest.main(verbosity=2)
