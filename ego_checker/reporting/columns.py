"""reporting.columns 的实现。"""
import ego_checker.reporting.audio as audio_validation
from pathlib import Path
from typing import List
from ego_checker.domain.constants import ROLE_LABELS
from ego_checker.domain.models import AbnormalFrame, PartResult


REPORT_NAME = "Ego同步帧测试报告.html"


SHORT_LABEL = {
    "left_rgb": "左目", "right_rgb": "右目",
    "side_left": "左侧目", "side_right": "右侧目",
    "front_rgb": "前目", "back_rgb": "后目", "top_rgb": "顶目",
    "wrist_left": "左腕带", "wrist_right": "右腕带",
}


CAM_SYNC_GROUPS = [
    ("depth", "Depth与另外四目同步"),
    ("depth_rgb", "Depth RGB与另外四目同步"),
    ("fisheye", "鱼眼三目同步"),
]


def _imu_total(result, device) -> int:
    """IMU 记录数；原始序列已释放时取释放前固化的边界样本计数."""
    source = result.imu_by_device if device is not None else None
    if source is not None and source.get(device):
        return len(source[device])
    if device is None and result.imu:
        return len(result.imu)
    samples = result.boundary_samples or {}
    if device is None:
        return int((samples.get('imu') or {}).get('count') or 0)
    return int(((samples.get('imu_by_device') or {}).get(device) or {}).get('count') or 0)


def _dev_short(dev: str) -> str:
    """设备名缩写 (用于 IMU 丢帧/差值显示)."""
    d = dev.upper()
    if "[" in dev:
        return dev
    if d == "EGO" or d.startswith("EGO"):
        return "EGO"
    if "WRISTCAM-L" in d or d.startswith("WRISTCAM_L"):
        return "左腕带"
    if "WRISTCAM-R" in d or d.startswith("WRISTCAM_R"):
        return "右腕带"
    return dev


def _group_camera_label(device: str, label: str) -> str:
    """Outer report groups already identify the device; avoid repeating it per camera."""
    prefix = f"{device} "
    return label[len(prefix):] if device and label.startswith(prefix) else label


def _video_duration_summary(result: PartResult) -> str:
    """Format every device/camera duration in one report cell."""
    entries = []
    for role, camera in result.cameras.items():
        label = camera.label or ROLE_LABELS.get(role.split("__", 1)[0], role)
        if role in result.video_duration_by_role:
            value = audio_validation.format_duration(result.video_duration_by_role[role])
        else:
            reason = result.video_duration_error_by_role.get(role, "视频时长未验证")
            value = f"UNVERIFIED {reason}"
        entries.append((result.camera_device_by_role.get(role, ""), label, value))
    if not entries:
        return "-"
    devices = list(dict.fromkeys(device for device, _label, _value in entries if device))
    if len(devices) > 1:
        grouped = []
        for device in devices:
            values = [f"{_group_camera_label(device, label)}:{value}"
                      for owner, label, value in entries
                      if owner == device]
            grouped.append(f"{_dev_short(device)}[{'/'.join(values)}]")
        unknown = [f"{label}:{value}" for owner, label, value in entries if not owner]
        if unknown:
            grouped.append(f"未知设备[{'/'.join(unknown)}]")
        return "；".join(grouped)
    if len(entries) == 1:
        return entries[0][2]
    prefix = f"{_dev_short(devices[0])}[" if devices else ""
    suffix = "]" if devices else ""
    device = devices[0] if devices else ""
    return prefix + "/".join(
        f"{_group_camera_label(device, label)}:{value}"
        for _owner, label, value in entries
    ) + suffix


def unique_path(path: Path) -> Path:
    """若 path 已存在则返回带 (1)/(2)... 序号的新路径, 不覆盖旧文件."""
    if not path.exists():
        return path
    parent = path.parent
    stem = path.stem
    suffix = path.suffix
    i = 1
    while True:
        cand = parent / f"{stem}({i}){suffix}"
        if not cand.exists():
            return cand
        i += 1


def _imu_context_entries(payload):
    """展开 IMU 跨段来源；携带匹配记录时只返回实际用于对齐的样本。"""
    context, matches = (payload if isinstance(payload, tuple)
                        else (payload, None))
    sources = []
    for key, value in context.items():
        if isinstance(value, dict):
            sources.extend((key, direction, sample)
                           for direction, sample in value.items())
        else:
            sources.append(("", key, value))
    if matches is None:
        for device, direction, sample in sources:
            yield device, direction, sample, None
        return
    source_by_timestamp = {
        timestamp: (device, direction, sample)
        for device, direction, sample in sources
        for timestamp in sample.timestamps_us
    }
    for match in matches:
        source = source_by_timestamp.get(match.ts_b)
        if source is not None:
            yield *source, match


class ReportColumns:
    """动态构建明细表列: 每个测试项合并为一个单元格 (跨相机), 时间单位 ms 保留 3 位."""

    def __init__(self, results: List[PartResult]):
        roles: List[str] = []
        for r in results:
            for role in r.cameras:
                if role not in roles:
                    roles.append(role)
        self.roles = roles
        self.cam_label = {}
        for role in roles:
            label = next((r.cameras[role].label for r in results if role in r.cameras), "")
            self.cam_label[role] = label or ROLE_LABELS.get(role, role)
        self.split_frame_count = any(
            r.frame_count_groups
            or 'frame_count_depth' in r.unavailable_checks
            or any(role in ("depth", "depth_rgb") for role in r.required_streams_missing)
            for r in results)
        self.split_cam_sync = any(r.cam_sync_grouped or r.cam_sync_groups for r in results)
        depth_settings = {r.depth_sync_enabled for r in results if 'depth_rgb' in r.cameras}
        self.depth_rgb_sync_label = (
            'Depth RGB与鱼眼三目同步' if depth_settings == {False} else
            'Depth RGB与其它相机同步' if len(depth_settings) > 1 else CAM_SYNC_GROUPS[1][1])
        # 一份汇总报告可同时包含 D3B 与普通/多设备相机。此时普通相机结果
        # 复用分组报告的第一帧数列和 fisheye 同步列，但列名不能把普通相机
        # 误称为“鱼眼三目”。纯 D3B 报告继续保留原有产品术语。
        def is_depth_product(result: PartResult) -> bool:
            return bool(
                result.frame_count_groups
                or result.cam_sync_groups
                or any(role in ("depth", "depth_rgb") for role in result.cameras)
                or any(role in ("depth", "depth_rgb")
                       for role in result.required_streams_missing)
            )

        self.has_generic_cameras = any(
            result.cameras and not is_depth_product(result)
            for result in results
        )
        self.primary_frame_count_label = (
            "相机/鱼眼三目帧数对齐"
            if self.split_frame_count and self.has_generic_cameras
            else "鱼眼三目帧数对齐"
        )
        self.fisheye_sync_label = (
            "相机/鱼眼三目同步"
            if self.split_cam_sync and self.has_generic_cameras
            else "鱼眼三目同步"
        )
        # 用户关闭音频测试时整组移除音频列（时长/包完整性/丢帧结果）。
        self.audio_enabled = any(r.audio_enabled for r in results)
        self.grip_roles = [role for role in roles
                           if any(role in r.grip_sync or
                                  (f'grip_sync_{role}' in r.unavailable_checks
                                   and (r.grip or any(r.grip_by_device.values())))
                                  for r in results)]
        self.has_imu_frequency_audit = any(r.imu_frequency_profiles for r in results)

    @staticmethod
    def _ms(us) -> str:
        """µs -> ms, 保留 3 位小数."""
        return f"{us / 1000.0:.3f}"

    def headers(self) -> List[str]:
        h = ["数据文件夹名称", "Part数据名称", "相机", "帧率(fps)",
             "固件版本", "视频时长"]
        if self.audio_enabled:
            h.append("音频时长")
        if self.split_frame_count:
            h += [self.primary_frame_count_label, "Depth/Depth RGB帧数对齐"]
        else:
            h.append("相机帧数对齐")
        for role in self.roles:
            s = SHORT_LABEL.get(role, self.cam_label[role])
            h.append(f"{s}帧间隔波动(ms)")
        if self.split_cam_sync:
            h += [CAM_SYNC_GROUPS[0][1], self.depth_rgb_sync_label,
                  self.fisheye_sync_label]
        else:
            for role in self.roles:
                s = SHORT_LABEL.get(role, self.cam_label[role])
                h.append(f"{s}基准同步对齐")
        for role in self.roles:
            s = SHORT_LABEL.get(role, self.cam_label[role])
            h.append(f"IMU与{s}同步")
        for role in self.grip_roles:
            s = SHORT_LABEL.get(role, self.cam_label[role])
            h.append(f"夹爪与{s}同步")
        h += ["相机丢帧", "IMU丢帧", "夹爪丢帧"]
        if self.audio_enabled:
            h += ["音频包完整性", "音频丢帧结果"]
        h += ["多组IMU差值(ms)", "同行同步对齐(ms)", "SEI对齐"]
        if self.has_imu_frequency_audit:
            h.append('IMU频率核对')
        h.append('业务结论')
        return h

    def _sync_cell(self, sr) -> str:
        """同步类单元格: PASS/FAIL + max 偏差 + 超阈占比(若有)."""
        status = "PASS" if sr.passed else "FAIL"
        base = f"{status} max{self._ms(sr.max_diff_us)}ms"
        if sr.over_count:
            base += f" 超{sr.over_pct:.2f}%"
        return base

    def _sync_group_cell(self, results) -> str:
        """聚合一个同步域内各参与相机的双向匹配结果；超阈数按相机标明。"""
        items = list(results.values()) if results else []
        if not items:
            return "-"
        passed = all(item.passed for item in items)
        max_diff = max((item.max_diff_us for item in items), default=0)
        cell = f"{'PASS' if passed else 'FAIL'} max{self._ms(max_diff)}ms"
        over_parts = [f"{item.base_label or ROLE_LABELS.get(role, role)}超{item.over_count}"
                      for role, item in results.items() if item.over_count]
        if over_parts:
            cell += " " + "/".join(over_parts)
        return cell

    def _frame_count_cell(self, result, missing_roles=()) -> str:
        """生成单个相机组的帧数/首尾时间戳对齐结论。"""
        if result is None:
            cell = "-"
        else:
            extra = ""
            if len(result.counts) >= 2:
                extra = (f" 首{self._ms(result.first_row_diff_us)}"
                         f"尾{self._ms(result.last_row_diff_us)}ms")
            cell = f"{'PASS' if result.passed else 'FAIL'} 帧差{result.count_diff}{extra}"
        if missing_roles:
            names = "/".join(ROLE_LABELS.get(role, role) for role in missing_roles)
            cell = f"FAIL 缺少必需流:{names}；" + cell
        return cell

    def _frame_count_by_device_cell(self, r: PartResult) -> str:
        """多设备帧数对齐：逐设备展示各自相机组内的结论，单目设备标注跳过。"""
        parts = []
        all_pass = True
        for device, result in r.frame_count_by_device.items():
            extra = ""
            if len(result.counts) >= 2:
                extra = (f" 首{self._ms(result.first_row_diff_us)}"
                         f"尾{self._ms(result.last_row_diff_us)}ms")
            all_pass = all_pass and result.passed
            parts.append(f"{_dev_short(device)}:{'PASS' if result.passed else 'FAIL'} "
                         f"帧差{result.count_diff}{extra}")
        for device, reason in r.frame_count_skipped_devices.items():
            parts.append(f"{_dev_short(device)}:跳过（单目相机）")
        if not parts:
            return "-"
        return f"{'PASS' if all_pass else 'FAIL'} " + "；".join(parts)

    def _check_keys(self, r: PartResult) -> List[str]:
        """与显示列一一对应；未执行原因同时控制摘要与详情。"""
        return ([''] * (7 if self.audio_enabled else 6)
                + ([('frame_count_fisheye' if any(role in r.cameras for role in ('depth', 'depth_rgb'))
                     else 'frame_count'), 'frame_count_depth'] if self.split_frame_count
                   else ['frame_count'])
                + [f'interval_{role}' for role in self.roles]
                + ([('cam_sync' if group == 'fisheye' and not r.cam_sync_grouped
                      else f'cam_sync_group_{group}') for group, _ in CAM_SYNC_GROUPS]
                   if self.split_cam_sync else [f'cam_sync_{role}' for role in self.roles])
                + [f'imu_sync_{role}' for role in self.roles]
                + [f'grip_sync_{role}' for role in self.grip_roles]
                + ['rgb_drop', 'imu_drop', 'grip_drop']
                + (['', ''] if self.audio_enabled else [])
                + ['', 'row_align', 'sei_align']
                + (['imu_frequency'] if self.has_imu_frequency_audit else []) + [''])

    def row_cells(self, r: PartResult) -> List[str]:
        ra = r.row_align
        ms = self._ms
        audio_duration = audio_validation.device_summary(
            r.audio_evidence, audio_validation.duration_summary)

        missing_cameras = [role for role in r.required_streams_missing if role != "imu"]
        if r.frame_count_by_device:
            # 多设备：各设备在自己的相机组内判定；单目设备无第二路可比，标注跳过。
            fc_cells = [self._frame_count_by_device_cell(r)]
        elif self.split_frame_count:
            fisheye_missing = [role for role in missing_cameras
                               if role not in ("depth", "depth_rgb")]
            depth_missing = [role for role in missing_cameras
                             if role in ("depth", "depth_rgb")]
            fisheye_result = r.frame_count_groups.get("fisheye")
            depth_result = r.frame_count_groups.get("depth")
            if not r.frame_count_groups and not any("depth" in role for role in r.cameras):
                fisheye_result = r.frame_count
            fc_cells = [self._frame_count_cell(fisheye_result, fisheye_missing),
                        self._frame_count_cell(depth_result, depth_missing)]
        else:
            fc_cells = [self._frame_count_cell(r.frame_count, missing_cameras)]

        cells = [
            r.data_folder,        # 数据文件夹名称 (part 文件上层文件夹 / 多设备会话根目录名)
            r.name,               # Part数据名称
            "/".join(
                r.cameras[role].label or self.cam_label.get(role, role)
                for role in self.roles if role in r.cameras
            ) or "-",
            f"{r.fps:.1f}" if r.fps else "-",
            r.firmware or "-",
            _video_duration_summary(r),
        ]
        if self.audio_enabled:
            cells.append(audio_duration)
        cells += fc_cells
        # 帧间隔波动 (每目一列, 偏差 dev 的 max/mean 合并显示, 超阈计数)
        for role in self.roles:
            iv = r.intervals.get(role)
            if iv:
                s = (f"{'PASS' if iv.passed else 'FAIL'} "
                     f"{ms(iv.max_dev_us)}/{ms(iv.mean_dev_us)}")
                if iv.over_count:
                    s += f" 超{iv.over_count}"
                cells.append(s)
            else:
                cells.append("-")
        # 相机基准同步对齐：D3B 按两个深度基准域和鱼眼域独立显示，
        # 其它产品仍按每目显示。
        if self.split_cam_sync:
            groups = r.cam_sync_groups or {"fisheye": r.cam_sync}
            for group, _label in CAM_SYNC_GROUPS:
                cell = ("未测试（用户关闭）" if not r.depth_sync_enabled
                        and group == 'depth' and group in r.cameras
                        else self._sync_group_cell(groups.get(group)))
                if group == 'depth_rgb' and not r.depth_sync_enabled and group in r.cameras:
                    cell += '（不含Depth）'
                cells.append(cell)
        else:
            for role in self.roles:
                cs = r.cam_sync.get(role)
                cell = ("未测试（用户关闭）" if not r.depth_sync_enabled
                        and role in r.cameras and role.split('__', 1)[0] == 'depth'
                        else self._sync_cell(cs) if cs and cs.matched_frames else "-")
                cells.append(cell)
        # IMU 同步 (每目一列)
        for role in self.roles:
            isy = r.imu_sync.get(role)
            cell = self._sync_cell(isy) if isy and isy.matched_frames else "-"
            if r.imu_error and role in r.cameras:
                cell = "ERROR IMU读取失败，未执行同步；" + r.imu_error
            cells.append(cell)
        # 夹爪同步只为存在所属夹爪时间轴的相机生成动态列。
        for role in self.grip_roles:
            grip_sync = r.grip_sync.get(role)
            cell = (self._sync_cell(grip_sync)
                    if grip_sync and grip_sync.matched_frames else "-")
            cells.append(cell)
        # RGB 连续性 (合并各相机丢帧、倒退和重复证据)
        rd_entries = []
        rd_all_pass = True
        for role in self.roles:
            rd = r.rgb_drop.get(role)
            if rd:
                rd_all_pass = rd_all_pass and rd.passed
                label = SHORT_LABEL.get(role, rd.label or self.cam_label.get(role, role))
                detail = f"{rd.total_lost_frames}"
                if rd.backstep_count:
                    detail += f" 倒退{rd.backstep_count}"
                if rd.duplicate_count:
                    detail += f" 重复{rd.duplicate_count}"
                rd_entries.append((r.camera_device_by_role.get(role, ""), label, detail))
        if r.multi_device and rd_entries:
            devices = list(dict.fromkeys(device for device, _label, _detail in rd_entries
                                         if device))
            rd_parts = []
            for device in devices:
                values = [f"{_group_camera_label(device, label)}{detail}"
                          for owner, label, detail in rd_entries if owner == device]
                rd_parts.append(f"{_dev_short(device)}[{'/'.join(values)}]")
            unknown = [f"{label}{detail}" for owner, label, detail in rd_entries
                       if not owner]
            if unknown:
                rd_parts.append(f"未知设备[{'/'.join(unknown)}]")
            rd_text = "；".join(rd_parts)
        else:
            rd_text = " ".join(f"{label}{detail}"
                               for _device, label, detail in rd_entries)
        rd_status = "PASS" if rd_all_pass else "FAIL"
        rd_cell = f"{rd_status} " + (rd_text or "0")
        # IMU 丢帧 (多设备按设备拆分; 显示「丢帧/总帧数」; 丢帧数=0 → PASS, ≥1 → FAIL)
        if r.imu_drop_by_device and len(r.imu_drop_by_device) > 1:
            parts = []
            all_pass = True
            for dev, dr in r.imu_drop_by_device.items():
                dev_total = _imu_total(r, dev)
                suffix = f" 倒退{dr.backstep_count}" if dr.backstep_count else ""
                parts.append(f"{_dev_short(dev)}:{dr.total_lost_frames}/{dev_total}{suffix}")
                all_pass = all_pass and dr.passed
            imu_cell = f"{'PASS' if all_pass else 'FAIL'} " + " ".join(parts)
        else:
            if r.imu_drop:
                imu_lost = r.imu_drop.total_lost_frames
                imu_total = _imu_total(r, None)
                suffix = f" 倒退{r.imu_drop.backstep_count}" if r.imu_drop.backstep_count else ""
                imu_cell = (f"{'PASS' if r.imu_drop.passed else 'FAIL'} {imu_lost} "
                            f"(总{imu_total}){suffix}")
            else:
                imu_cell = "-"
        if "imu" in r.required_streams_missing:
            imu_cell = "FAIL 元数据要求 IMU，但 MCAP 无消息"
        if not r.imu_order_valid:
            detected_backsteps = ((r.imu_drop.backstep_count if r.imu_drop else 0)
                                  + sum(item.backstep_count
                                        for item in r.imu_drop_by_device.values()))
            if not detected_backsteps:
                imu_cell = "FAIL 源 IMU 时间倒退；" + imu_cell
        if r.imu_error:
            imu_cell = "ERROR " + r.imu_error + "；未执行丢帧检查"
        elif r.imu_frequency_error or r.imu_frequency_error_by_device:
            frequency_errors = [r.imu_frequency_error] if r.imu_frequency_error else []
            frequency_errors.extend(r.imu_frequency_error_by_device.values())
            measured = ('；已分析设备：' + imu_cell) if r.imu_drop_by_device else ''
            imu_cell = "ERROR " + "；".join(frequency_errors) + "；对应设备未执行丢帧检查" + measured
        # 多组 IMU 差值 (max/mean)
        gd = r.imu_groups_diff
        if r.imu_error:
            groups_cell = "ERROR IMU读取失败，未执行多组差值检查"
        elif gd and gd.matched_frames:
            groups_cell = f"max:{ms(gd.max_diff_us)}ms mean:{ms(gd.mean_diff_us)}ms"
        else:
            groups_cell = "-"
        # 同行同步对齐（多设备按设备各自相机组判定）
        if r.row_align_by_device:
            ra_parts = []
            ra_all_pass = True
            for device, ra_dev in r.row_align_by_device.items():
                ra_all_pass = ra_all_pass and ra_dev.passed
                part = f"{_dev_short(device)}:{'PASS' if ra_dev.passed else 'FAIL'} " \
                       f"max{ms(ra_dev.max_diff_us)}ms"
                if ra_dev.over_count:
                    part += f" 超{ra_dev.over_count}"
                ra_parts.append(part)
            for device in r.frame_count_skipped_devices:
                ra_parts.append(f"{_dev_short(device)}:跳过（单目相机）")
            ra_cell = f"{'PASS' if ra_all_pass else 'FAIL'} " + "；".join(ra_parts)
        else:
            ra_cell = f"{'PASS' if ra.passed else 'FAIL'} max{ms(ra.max_diff_us)}ms"
            if ra.over_count:
                ra_cell += f" 超{ra.over_count}"
        # SEI 对齐
        sei = r.checks.get("sei_align", "-")
        if isinstance(sei, bool):
            sei = "PASS" if sei else "FAIL"
        # 夹爪 (grip) 丢帧 (仅当存在该流)
        def grip_stat(item):
            text = str(item.total_lost_frames)
            if item.backstep_count:
                text += f" 倒退{item.backstep_count}"
            if item.duplicate_count:
                text += f" 重复{item.duplicate_count}"
            return text

        grip_cell = "-"
        if r.grip_drop_by_device:
            grip_cell = (f"{'PASS' if all(item.passed for item in r.grip_drop_by_device.values()) else 'FAIL'} "
                         + " ".join(f"{device}:{grip_stat(item)}"
                                    for device, item in r.grip_drop_by_device.items()))
        elif r.grip_drop is not None:
            grip_cell = (f"{'PASS' if r.grip_drop.passed else 'FAIL'} "
                         f"{grip_stat(r.grip_drop)}")
        audio_packets = audio_validation.device_summary(
            r.audio_evidence, audio_validation.packet_integrity_summary)
        audio_drop = audio_validation.device_summary(
            r.audio_evidence, audio_validation.drop_summary)
        cells += [rd_cell, imu_cell, grip_cell]
        if self.audio_enabled:
            cells += [audio_packets, audio_drop]
        cells += [groups_cell, ra_cell, sei]
        if self.has_imu_frequency_audit:
            entries = []
            for row in r.imu_frequency_audit:
                configured = row['configured_hz']
                measured = row['measured_hz']
                reference = row['reference_hz']
                entries.append(f'{row["device"] or "当前设备"}/{row["sensor"]} '
                    f'JSON={configured:g}Hz' if configured is not None else
                    f'{row["device"] or "当前设备"}/{row["sensor"]} JSON=未取得')
                entries[-1] += (f' 实测={measured:.6f}Hz（整数{row["rounded_hz"]}Hz）'
                                 if measured is not None else ' 实测=未验证')
                entries[-1] += f' 基准={reference:g}Hz' if reference is not None else ' 基准=未取得'
                if row['reason']:
                    entries[-1] += ' ' + row['reason']
            status = 'ERROR' if r.imu_frequency_validation_errors else 'PASS'
            cells.append(status + ' ' + '；'.join(entries) if entries else '-')
        cells.append(r.business_status)
        for index, key in enumerate(self._check_keys(r)):
            reason = r.unavailable_checks.get(key)
            if reason:
                # RGB 汇总同时保留完整流的实测结果，不把部分覆盖称为整体 PASS。
                measured = f'；已分析流：{cells[index]}' if key == 'rgb_drop' and r.rgb_drop else ''
                cells[index] = 'ERROR ' + r.unavailable_check_message(key) + measured
        if r.error:
            if not r.checks_completed:
                # 来源冲突或执行中断，不展示默认或未完成结果。
                start = 7 if self.audio_enabled else 6
                cells[start:-1] = ["-"] * (len(cells) - start - 1)
            cells[-1] = "ERROR " + r.error
        return cells

    def row_details(self, r: PartResult) -> List:
        """返回与 row_cells 对齐的每列详情描述符.

        描述符为 (kind, payload) 元组或 None:
          ("frame_count", r)                相机帧数对齐
          ("interval", (iv, role))          帧间隔波动
          ("cam_sync"/"imu_sync"/"grip_sync", (sr, role))  相机/IMU/夹爪同步
          ("rgb_drop", [(role, DropResult)])   相机丢帧(合并)
          ("imu_drop", DropResult)             IMU 丢帧
          或 List[AbnormalFrame] (row_align / sei 通用异常帧)
        """
        # 数据文件夹名称/Part数据名称/相机/帧率/固件/视频时长[/音频时长]
        det: List = [None] * (7 if self.audio_enabled else 6)
        if self.audio_enabled and r.audio_evidence:
            det[6] = ('audio_diagnostics', r.audio_evidence)
        if r.frame_count_by_device:
            blocks = []
            for device in r.frame_count_by_device:
                roles = [role for role, owner in r.camera_device_by_role.items()
                         if owner == device and role in r.cameras]
                if roles:
                    blocks.append(("frame_count_group",
                                   (r, roles, f"{_dev_short(device)}对齐详情")))
            det.append(blocks or None)                       # 多设备帧数对齐
        elif self.split_frame_count:
            fisheye_roles = [role for role in r.cameras if role not in ("depth", "depth_rgb")]
            depth_roles = [role for role in ("depth", "depth_rgb") if role in r.cameras]
            fisheye_result = r.frame_count_groups.get("fisheye")
            if not r.frame_count_groups and fisheye_roles:
                fisheye_result = r.frame_count
            detail_label = self.primary_frame_count_label.removesuffix("帧数对齐") + "对齐详情"
            det.append(("frame_count_group", (r, fisheye_roles, detail_label))
                       if fisheye_result else None)
            det.append(("frame_count_group", (r, depth_roles, "Depth对齐详情"))
                       if r.frame_count_groups.get("depth") else None)
        else:
            det.append(("frame_count", r))                   # 相机帧数对齐
        for role in self.roles:                              # 帧间隔波动
            iv = r.intervals.get(role)
            det.append(("interval", (iv, role)) if iv and iv.abnormal else None)
        if self.split_cam_sync:                              # D3B 三个同步域
            groups = r.cam_sync_groups or {"fisheye": r.cam_sync}
            for group, _label in CAM_SYNC_GROUPS:
                blk: List = []
                context_matches: List[AbnormalFrame] = []
                for role, cs in groups.get(group, {}).items():
                    if cs.abnormal:
                        blk.append(("cam_sync", (cs, role)))
                    context_matches.extend(cs.context_matches)
                if r.cam_sync_context and context_matches:
                    blk.append(("cam_context", (r.cam_sync_context, context_matches)))
                det.append(blk if blk else None)
        else:
            for role in self.roles:                          # 相机基准同步对齐
                cs = r.cam_sync.get(role)
                blk: List = []
                if cs and cs.abnormal:
                    blk.append(("cam_sync", (cs, role)))
                if r.cam_sync_context and cs and cs.context_matches:
                    blk.append(("cam_context", (r.cam_sync_context,
                                                cs.context_matches)))
                det.append(blk if blk else None)
        for role in self.roles:                              # IMU同步
            if r.imu_error:
                det.append(None)
                continue
            isy = r.imu_sync.get(role)
            # 异常详情 + 跨段样本来源, 与相机同步一致 (并列独立显示)
            blk = []
            if isy and isy.abnormal:
                blk.append(("imu_sync", (isy, role)))
            if isy and isy.context_matches and r.imu_sync_context:
                context = r.imu_sync_context
                device = r.camera_device_by_role.get(role, "")
                if device:
                    context = ({device: context[device]}
                               if device in context else {})
                if context:
                    blk.append(("imu_context", (context, isy.context_matches)))
            det.append(blk if blk else None)
        for role in self.grip_roles:                         # 夹爪与所属设备相机同步
            grip_sync = r.grip_sync.get(role)
            # 报告中不再展示夹爪 JSON 频率与同步阈值，只保留异常帧详情。
            if grip_sync and grip_sync.abnormal:
                det.append([('grip_sync', (grip_sync, role))])
            elif f'grip_sync_{role}' in r.unavailable_checks:
                det.append([('grip_sync_unavailable', (r, role))])
            else:
                det.append(None)
        rgb_entries: List = []                               # 相机丢帧(合并)
        for role in self.roles:
            rd = r.rgb_drop.get(role)
            if rd and rd.abnormal:
                rgb_entries.append((role, rd))
        det.append(("rgb_drop", rgb_entries) if rgb_entries else None)
        if r.imu_error:
            det.append(None)
        elif len(r.imu_drop_by_device) > 1:                  # IMU丢帧(按设备)
            entries = [(dev, dr) for dev, dr in r.imu_drop_by_device.items() if dr.abnormal]
            det.append(("imu_drop_devices", entries) if entries else None)
        else:
            det.append(("imu_drop", r.imu_drop) if r.imu_drop and r.imu_drop.abnormal else None)
        # 夹爪 (grip) 丢帧；多设备详情保留设备身份，避免双夹爪异常无法定位。
        if len(r.grip_drop_by_device) > 1:
            entries = [(dev, dr) for dev, dr in r.grip_drop_by_device.items()
                       if dr.abnormal]
            det.append(("grip_drop_devices", entries) if entries else None)
        else:
            det.append(("grip_drop", r.grip_drop)
                       if r.grip_drop and r.grip_drop.abnormal else None)
        # 音频时长已位于视频时长后；此处仅保留包完整性和丢帧结果详情。
        if self.audio_enabled:
            det.append(('audio_diagnostics', r.audio_evidence) if r.audio_evidence else None)
            audio_events = [event for evidence in r.audio_evidence
                            for event in evidence.continuity_events]
            det.append(("audio_continuity", r.audio_evidence) if audio_events else None)
        det.append(None)                                   # 多组IMU差值 (仅显示 max/mean)
        ra = r.row_align
        if r.row_align_by_device:
            # 多设备：合并且按设备挑选各自组内相机，避免跨设备混列。
            ra_blocks = []
            for device, ra_dev in r.row_align_by_device.items():
                if not ra_dev.abnormal:
                    continue
                roles = [ro for ro, owner in r.camera_device_by_role.items()
                         if owner == device and ro != "depth" and ro in r.cameras]
                ra_blocks.append(("row_align", (ra_dev, roles)))
            det.append(ra_blocks or None)                    # 同行同步对齐
        else:
            # 同行对齐不含 depth; 详情列仅用非 depth 相机
            ra_roles = [ro for ro in self.roles if ro != "depth"]
            det.append(("row_align", (ra, ra_roles)) if ra.abnormal else None)  # 同行同步对齐
        sei_af = r.over_frames_by_check.get("sei_align")
        det.append(("sei", sei_af) if sei_af else None)                        # SEI对齐
        det.append(None)  # business status
        if self.has_imu_frequency_audit:
            det.insert(len(det) - 1, ('imu_frequency', r) if r.imu_frequency_audit else None)
        for index, key in enumerate(self._check_keys(r)):
            if key in r.unavailable_checks and key != 'rgb_drop' and not key.startswith('grip_sync_'):
                det[index] = None
        if r.error and not r.checks_completed:
            start = 7 if self.audio_enabled else 6
            det[start:] = [None] * (len(det) - start)
        return det
