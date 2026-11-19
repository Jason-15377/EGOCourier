"""analysis.timing 的实现。"""
from typing import Dict, List, Optional, Set, Tuple
import math
from ego_checker.analysis.sync_edges import remember_query, remember_statistics
from ego_checker.domain.constants import ROLE_LABELS
from ego_checker.domain.constants import CAM_SYNC_THRESHOLD_US, DEPTH_ROW_DIFF_US, FRAME_COUNT_ROW_DIFF_US, IMU_DROP_MULTIPLIER, IMU_SYNC_THRESHOLD_US, INTERVAL_WAVE_MS, RGB_DROP_MULTIPLIER, ROW_ALIGN_THRESHOLD_US, US_PER_MS
from ego_checker.domain.models import AbnormalFrame, CameraData, DropResult, FrameCountResult, IntervalResult, RowAlignResult, SourceRows, SyncResult
from ego_checker.domain.numeric import _median, _nearest_diffs_sorted, _nearest_idx, _percentile
from ego_checker.analysis.vectorized import gap_events, nearest_indexes, safe_int64


def check_frame_count(cams: Dict[str, CameraData]) -> FrameCountResult:
    """相机帧数对齐: 组内帧数差值=0 且 组内首/尾行时间戳差值 < 组阈值 → PASS.

    含 depth 的三目+depth 结构分组判定, 帧数差值与首尾对齐均在组内比较:
      - {depth, depth_rgb}: 首/尾帧对齐阈值 5ms
      - 鱼眼三目 (其余相机): 首/尾帧对齐阈值 1ms
    其它数据沿用原口径 (全部相机一组, 1ms).
    """
    r = FrameCountResult()
    if len(cams) < 2:
        r.detail = "相机数 < 2, 跳过帧数对齐"
        r.passed = True
        return r
    counts = {role: c.count for role, c in cams.items()}
    r.counts = counts

    # 分组: 含 depth 时 {depth, depth_rgb} (5ms) 与 鱼眼三目 (1ms) 分别判定
    if "depth" in cams and "depth_rgb" in cams:
        cut_groups: List[Tuple[List[str], int]] = [(["depth", "depth_rgb"], DEPTH_ROW_DIFF_US)]
        fisheye = [x for x in cams.keys() if x not in ("depth", "depth_rgb")]
        if fisheye:
            cut_groups.append((fisheye, FRAME_COUNT_ROW_DIFF_US))
    else:
        cut_groups = [(list(cams.keys()), FRAME_COUNT_ROW_DIFF_US)]

    # 帧数差值 / 首行 / 尾行差值: 均在组内 (max-min) 取各组最差
    count_diff = 0
    first_diff = 0
    last_diff = 0
    worst_count_group = ""
    worst_first_th = FRAME_COUNT_ROW_DIFF_US
    worst_last_th = FRAME_COUNT_ROW_DIFF_US
    for group, th_us in cut_groups:
        sub = [cams[role] for role in group if role in cams]
        if not sub:
            continue
        grp_counts = [c.count for c in sub]
        cd = (max(grp_counts) - min(grp_counts)) if len(grp_counts) >= 2 else 0
        if cd > count_diff:
            count_diff, worst_count_group = cd, "/".join(group)
        firsts = [c.timestamps[0] for c in sub if c.timestamps]
        lasts = [c.timestamps[-1] for c in sub if c.timestamps]
        fd = (max(firsts) - min(firsts)) if len(firsts) >= 2 else 0
        ld = (max(lasts) - min(lasts)) if len(lasts) >= 2 else 0
        if fd > first_diff:
            first_diff, worst_first_th = fd, th_us
        if ld > last_diff:
            last_diff, worst_last_th = ld, th_us
    r.count_diff = count_diff
    r.first_row_diff_us = first_diff
    r.last_row_diff_us = last_diff

    frame_ok = r.count_diff == 0
    first_ok = r.first_row_diff_us < worst_first_th
    last_ok = r.last_row_diff_us < worst_last_th
    r.passed = frame_ok and first_ok and last_ok

    # 异常帧详情
    if not first_ok:
        r.abnormal.append(AbnormalFrame(
            csv_line=1, row_index=0, value_us=r.first_row_diff_us,
            threshold_us=worst_first_th, detail="首行时间戳差值"))
    if not last_ok:
        r.abnormal.append(AbnormalFrame(
            csv_line=max(c.rows[-1] + 2 for c in cams.values() if c.rows),
            row_index=max(c.rows[-1] for c in cams.values() if c.rows),
            value_us=r.last_row_diff_us, threshold_us=worst_last_th,
            detail="尾行时间戳差值"))
    if not frame_ok:
        r.abnormal.append(AbnormalFrame(
            csv_line=0, row_index=-1, value_us=r.count_diff,
            threshold_us=0,
            detail=f"相机帧数差值={r.count_diff}" + (f" (组 {worst_count_group})" if worst_count_group else "")))
    parts = "  ".join(f"{ROLE_LABELS.get(k, k)}={v}" for k, v in counts.items())
    r.detail = (f"帧数差值={r.count_diff} 首行差值={r.first_row_diff_us}µs "
                f"尾行差值={r.last_row_diff_us}µs [{parts}]")
    return r


def check_frame_count_groups(cams: Dict[str, CameraData]) -> Dict[str, FrameCountResult]:
    """深度产品分别返回鱼眼相机组与 depth/depth_rgb 组的帧数对齐结果。"""
    depth_roles = [role for role in ("depth", "depth_rgb") if role in cams]
    fisheye_roles = [role for role in cams if role not in ("depth", "depth_rgb")]
    if not depth_roles or not fisheye_roles:
        return {}
    return {
        "fisheye": check_frame_count({role: cams[role] for role in fisheye_roles}),
        "depth": check_frame_count({role: cams[role] for role in depth_roles}),
    }


def compute_expected_interval(ts: List[int]) -> float:
    """数据平均帧间隔 (µs): (末帧-首帧)/(帧数-1). 作为无帧率检测时的兜底."""
    if len(ts) < 2:
        return 0.0
    dur = ts[-1] - ts[0]
    return dur / max(1, len(ts) - 1)


COMMON_RATES = [1, 10, 15, 24, 25, 30, 50, 60, 100, 120, 200, 250, 400, 500, 1000, 2000]


def auto_detect_rate(timestamps_us: List[int]) -> float:
    """从时间戳中位数间隔自动检测帧率 (Hz), 吸附到常见帧率."""
    n = len(timestamps_us)
    if n < 10:
        return 30.0
    sample = sorted(timestamps_us[:200])
    deltas = []
    for i in range(1, len(sample)):
        if sample[i] > sample[i - 1]:
            deltas.append(sample[i] - sample[i - 1])
    if not deltas:
        return 30.0
    deltas.sort()
    median_interval = deltas[len(deltas) // 2]
    if median_interval <= 0:
        return 30.0
    rate = 1_000_000.0 / median_interval
    return min(COMMON_RATES, key=lambda r: abs(r - rate))


def expected_interval_us(ts: List[int]) -> float:
    """帧间隔基准 expected = 1/帧率 (µs), 帧率由时间戳中位数间隔检测并吸附.

    例如 30fps => expected = 1/30 ≈ 33333µs = 33.333ms.
    """
    rate = auto_detect_rate(ts)
    if rate <= 0:
        return compute_expected_interval(ts)
    return 1_000_000.0 / rate


def check_interval(cam: CameraData, threshold_us: int = INTERVAL_WAVE_MS * US_PER_MS,
                   expected_us: Optional[float] = None) -> IntervalResult:
    """interval 帧间隔: |interval - expected| >= 10ms 记异常.

    expected_us 由调用方按录制配置的帧率传入；配置缺失时才按时间戳推定。
    """
    r = IntervalResult(role=cam.role, label=cam.label)
    ts = cam.timestamps
    if len(ts) < 2:
        r.detail = "帧数不足, 跳过"
        r.passed = True
        return r
    if expected_us is not None and expected_us > 0:
        r.expected_us = float(expected_us)
    else:
        r.expected_us = expected_interval_us(ts)   # 无配置时的兜底：1/帧率
    intervals = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
    r.max_interval_us = max(intervals)
    r.min_interval_us = min(intervals)
    r.mean_interval_us = sum(intervals) / len(intervals)
    devs = [abs(iv - r.expected_us) for iv in intervals]
    r.max_dev_us = max(devs)
    r.mean_dev_us = sum(devs) / len(devs)
    over = []
    for i, iv in enumerate(intervals):
        dev = abs(iv - r.expected_us)
        if dev >= threshold_us:
            note = "，时间戳回退" if iv < 0 else ""
            over.append(AbnormalFrame(
                csv_line=cam.rows[i] + 2, row_index=cam.rows[i],
                value_us=iv, threshold_us=threshold_us,
                ts_a=ts[i], ts_b=ts[i + 1],
                detail=f"波动偏差{dev/1000:.3f}ms vs 阈值{threshold_us/1000:.3f}ms"
                       f"（原始间隔{iv/1000:.3f}ms）{note}"))
    r.over_count = len(over)
    r.abnormal = over
    r.passed = r.over_count == 0
    r.detail = (f"max={r.max_interval_us}µs mean={r.mean_interval_us:.0f}µs "
                f"expected={r.expected_us:.0f}µs 超阈={r.over_count}")
    return r


def check_row_align(cams: Dict[str, CameraData],
                    threshold_us: int = ROW_ALIGN_THRESHOLD_US) -> RowAlignResult:
    """同行时间戳对齐: 每行相机时间戳最大差值 >= 阈值 记异常.

    含 depth 的三目+depth 结构: 只测鱼眼三目 (其余非 depth 相机) 的同行对齐,
    depth/rgb 组不参与同行对齐 (其对齐由帧数对齐的 5ms 口径覆盖)。
    """
    r = RowAlignResult()

    # 分组: 含 depth 时只保留鱼眼三目组; 否则全部一组
    roles = list(cams.keys())
    if "depth" in cams and "depth_rgb" in cams:
        groups = [[x for x in roles if x not in ("depth", "depth_rgb")]]
    else:
        groups = [[x for x in roles if x != "depth"]]
    groups = [g for g in groups if len(g) >= 2]
    if not groups:
        r.detail = "可对齐相机组 < 2, 跳过同行对齐"
        r.passed = True
        return r

    diffs: List[int] = []
    over: List[AbnormalFrame] = []
    for g in groups:
        sub = [cams[role] for role in g]
        n = min(c.count for c in sub)
        for i in range(n):
            vals = [c.timestamps[i] for c in sub]
            d = max(vals) - min(vals)
            diffs.append(d)
            if d >= threshold_us:
                over.append(AbnormalFrame(
                    csv_line=next(c.rows[i] + 2 for c in sub if c.rows),
                    row_index=i, value_us=d, threshold_us=threshold_us,
                    cam_ts={cams[role].role: cams[role].timestamps[i] for role in g},
                    detail=f"同行组[{'/'.join(g)}] 时间戳差值"))
    if not diffs:
        r.detail = "无有效行数据"
        r.passed = True
        return r
    r.max_diff_us = max(diffs)
    r.mean_diff_us = sum(diffs) / len(diffs)
    r.over_count = len(over)
    r.abnormal = over
    r.passed = r.over_count == 0
    r.detail = f"组{len(groups)} max={r.max_diff_us}µs mean={r.mean_diff_us:.0f}µs 超阈={r.over_count}"
    return r


def _nearest_diff_pool(cam: CameraData, pool: List[int]) -> List[int]:
    """cam 每帧在 pool 中找最近邻, 返回各帧最小差值 (µs)."""
    if not cam.timestamps or not pool:
        return []
    spool = sorted(pool)
    out = []
    for t in cam.timestamps:
        idx = _nearest_idx(spool, t)
        if idx is None:
            continue
        out.append(abs(spool[idx] - t))
    return out


def _cam_pool_index(cams: Dict[str, CameraData], roles: List[str],
                    ctx_pool: Optional[Dict[str, List[int]]] = None):
    """合并多路相机时间戳为一个全局有序池, 并记录每条来自哪路相机/哪一行.

    返回 (sorted_ts, meta), meta[i] = (role, data_row_index) 供最近邻回溯来源.
    ctx_pool: 可选的「相邻分段边界参考」{role: [ts,...]}; 其条目以行号 -1 加入
    (表示来自相邻切片, 非本段数据行), 仅供最近邻匹配使用。
    """
    items: List[Tuple[int, str, int]] = []
    for role in roles:
        c = cams.get(role)
        if c is None:
            continue
        for t, r in zip(c.timestamps, c.rows):
            items.append((t, role, r))
        # 相邻分段提供的边界参考 (行号 -1)
        if ctx_pool and role in ctx_pool:
            for t in ctx_pool[role]:
                items.append((t, role, -1))
    items.sort(key=lambda x: x[0])
    ts = [x[0] for x in items]
    meta = [(x[1], x[2]) for x in items]
    return ts, meta


def _cam_sync_core(base_role: str, base: CameraData, cams: Dict[str, CameraData],
                   other_roles: List[str], threshold_us: int,
                   mode_detail: str,
                   ctx_pool: Optional[Dict[str, List[int]]] = None,
                   ctx_base: Optional[List[int]] = None) -> SyncResult:
    """以 base 为基准做「双向帧序对齐」, 生成 SyncResult.

    双向: 既检查 base 的每帧能否在对端找到近邻 (base 端缺口), 也检查对端每帧能否在
    base 找到近邻 (对端缺口 = base 自身缺帧)。两方向缺口都记入本路异常, 因此
    「多路同时丢帧」也能在每路的 cam_sync 中体现, 使同步异常数与丢帧数对应。
    ctx_pool: 相邻分段的「对端相机」边界参考 {role: [ts]}, 用于方向1 (本段帧跨段找对端).
    ctx_base: 相邻分段的「本路相机」边界参考 [ts], 用于方向2 (对端帧跨段找本路).
    """
    sr = SyncResult(base_role=base_role, base_label=base.label,
                    threshold_us=threshold_us, mode="camera")
    sr.total_frames = base.count
    # 防御性排除基准角色：即使调用方误把它传入，也绝不允许本相机进入对端池。
    other_roles = [role for role in other_roles if role != base_role]
    spool, meta = _cam_pool_index(cams, other_roles, ctx_pool)
    if not spool:
        sr.detail = "组内无其余相机时间戳, 跳过"
        return sr
    base_pool = sorted(base.timestamps)
    # 方向2 匹配用池: 本段 base + 相邻切片 base 边界参考
    base_pool_ext = sorted(list(base_pool) + list(ctx_base or []))
    base_set = set(base_pool)
    ctx_base_set = set(ctx_base or [])
    # 排序后的 base 时间戳 -> 原始下标 (方向2 回溯行号用)
    base_order = sorted(range(len(base.timestamps)), key=lambda k: base.timestamps[k])
    seen_keys = {}

    def _add_context(query: CameraData, query_role: str, query_i: int,
                     query_ts: int, ref_ts: int, ref_role: str, d: int):
        """记录算法实际选中的跨段最近邻；候选帧不进入该列表。"""
        ref = cams.get(ref_role)
        sr.context_matches.append(AbnormalFrame(
            csv_line=query.rows[query_i] + 2,
            row_index=query.rows[query_i],
            value_us=d, threshold_us=threshold_us,
            ts_a=query_ts, ts_b=ref_ts,
            cam=query_role, cam_label=query.label or query_role,
            b_role=ref_role,
            b_label=(ref.label if ref else ref_role),
            b_row_index=None, b_csv_line=None,
            detail=(f"{query.label or query_role} → "
                    f"{ref.label if ref else ref_role} 跨段最近邻")))

    def _add(a_row: int, a_ts: int, b_ts: int, b_role: str, b_row: int, d: int, tag: str):
        key = (a_row, b_role, b_row)
        if key in seen_keys:
            return seen_keys[key]
        b_lbl = cams[b_role].label if b_role in cams else b_role
        sr.abnormal.append(AbnormalFrame(
            csv_line=base.rows[a_row] + 2, row_index=base.rows[a_row],
            value_us=d, threshold_us=threshold_us,
            ts_a=a_ts, ts_b=b_ts,
            b_role=b_role, b_label=b_lbl,
            b_row_index=b_row, b_csv_line=(b_row + 2 if b_row >= 0 else -1),
            detail=tag))
        seen_keys[key] = sr.abnormal[-1]
        return sr.abnormal[-1]

    diffs = []
    other_diffs = []   # 方向2(对端帧→base)偏差, 用于 max/mean
    # 方向1: base 每帧在对端池找最近邻 (base 有、对端缺)
    for i, t in enumerate(base.timestamps):
        idx = _nearest_idx(spool, t)
        if idx is None:
            continue
        d = abs(spool[idx] - t)
        diffs.append(d)
        b_role, b_row = meta[idx]
        if b_row < 0:
            # 命中的是相邻切片参考: 本帧在相邻分段对齐, 不计异常
            if d < threshold_us:
                _add_context(base, base_role, i, t, spool[idx], b_role, d)
            continue
        if d >= threshold_us:
            event = _add(i, t, spool[idx], b_role, b_row, d,
                 f"{base.label} vs {cams[b_role].label if b_role in cams else b_role} 最近邻")
            remember_query(sr, event, base_role, base.label, base.rows[i], t,
                           other_roles, spool)
    # 方向2: 对端每帧在 base 找最近邻 (对端有、base 缺 => base 自身缺帧)
    for ot, (br, brow) in zip(spool, meta):
        if brow < 0:            # 相邻切片参考不是本段对端数据, 跳过
            continue
        other = cams.get(br)
        if other is None:
            continue
        j = _nearest_idx(base_pool_ext, ot)
        if j is None:
            continue
        d2 = abs(base_pool_ext[j] - ot)
        other_diffs.append(d2)
        if (d2 < threshold_us and base_pool_ext[j] in ctx_base_set
                and base_pool_ext[j] not in base_set):
            # brow is a physical source row, not a timestamp-list index.
            sr.context_matches.append(AbnormalFrame(
                csv_line=brow + 2, row_index=brow, value_us=d2,
                threshold_us=threshold_us, ts_a=ot, ts_b=base_pool_ext[j],
                cam=br, cam_label=other.label, b_role=base_role,
                b_label=base.label, detail=f"{other.label} → {base.label} 跨段最近邻"))
        if d2 >= threshold_us:
            # 对端帧在 base(含相邻切片参考) 仍找不到近邻: 记到 base (取本段最近的 base 帧)
            k = _nearest_idx(base_pool, ot)
            if k is None:
                continue
            orig_i = base_order[k]
            event = _add(orig_i, base_pool[k], ot, br, brow, d2,
                 f"{base.label} 缺帧 (对端 {cams[br].label if br in cams else br} 有)")
            remember_query(sr, event, br, other.label, brow, ot, (base_role,), base_pool)
    sr.matched_frames = len(diffs)
    # max/mean 合并方向1(本路→对端)与方向2(对端→本路)偏差, 反映真实同步偏差峰值
    all_dev = diffs + other_diffs
    remember_statistics(sr, all_dev)
    if all_dev:
        sr.max_diff_us = max(all_dev)
        sr.mean_diff_us = sum(all_dev) / len(all_dev)
    if diffs:
        sd = sorted(all_dev)
        sr.p95_diff_us = _percentile(sd, 0.95)
        sr.p50_diff_us = _percentile(sd, 0.50)
    sr.over_count = len(sr.abnormal)
    sr.passed = sr.over_count == 0
    sr.context_hits = len(sr.context_matches)
    ctx_note = (f" 相邻切片参考命中{sr.context_hits}帧"
                if sr.context_hits else "")
    sr.detail = (f"{mode_detail}max={sr.max_diff_us}µs mean={sr.mean_diff_us:.0f}µs "
                 f"超阈={sr.over_count}/{sr.matched_frames}{ctx_note}")
    return sr


def check_cam_sync(cams: Dict[str, CameraData],
                   threshold_us: int = CAM_SYNC_THRESHOLD_US,
                   ctx_pool: Optional[Dict[str, List[int]]] = None,
                   ctx_base: Optional[Dict[str, List[int]]] = None) -> Dict[str, SyncResult]:
    """相机时间戳同步: 每目相机为基准, 在其余相机时间戳池中找最近邻.

    判定 (release0820_align_v2): 0 帧超阈 → PASS, 任何超阈 → FAIL.
    ctx_pool: 相邻分段的「对端相机」边界参考 {role: [ts]} (方向1 跨段找对端)。
    ctx_base: 相邻分段的「本路相机」边界参考 {role: [ts]} (方向2 跨段找本路)。
    """
    results: Dict[str, SyncResult] = {}
    if len(cams) < 2:
        return results
    for base_role, base in cams.items():
        other_roles = [r for r in cams if r != base_role]
        results[base_role] = _cam_sync_core(
            base_role, base, cams, other_roles, threshold_us, mode_detail="",
            ctx_pool=ctx_pool,
            ctx_base=(ctx_base.get(base_role) if ctx_base else None))
    return results


def _depth_sync_threshold_us(adaptive=False, depth_sync_ms=5.0):
    if adaptive:
        return 5000
    value = float(depth_sync_ms)
    if not math.isfinite(value) or not 0.001 <= value <= 1_000_000:
        raise ValueError('深度同步阈值须为0.001～1000000 ms')
    return round(value * US_PER_MS)


def _cam_sync_pair_groups(
        cams: Dict[str, CameraData], adaptive: bool = False,
        depth_sync: bool = True,
        depth_sync_ms: float = 5.0,
) -> Optional[Dict[str, List[Tuple[str, str, int]]]]:
    """返回 D3B 的独立相机同步域；关闭时 Depth 不作基准或比较目标。

    ``depth``、``depth_rgb`` 中的元组是有方向的“基准相机 ->
    对比相机”，开启时两个基准各自与另外四目比较并形成独立报告结论；
    关闭时仅保留 Depth RGB 指向鱼眼三目的比较。
    ``fisheye`` 仍是无方向的鱼眼三目两两同步对。
    """
    roles = set(cams.keys())
    depth_roles = [role for role in ('depth', 'depth_rgb') if role in roles]
    if not depth_roles:
        return None
    depth_threshold_us = _depth_sync_threshold_us(adaptive, depth_sync_ms)

    def th(a: str, b: str) -> int:
        if a in ('depth', 'depth_rgb') or b in ('depth', 'depth_rgb'):
            return depth_threshold_us
        return int(_adaptive_cam_sync_ms(a, b) * US_PER_MS)

    rgb3 = [r for r in ("left_rgb", "right_rgb", "up_rgb") if r in roles]
    fisheye_pairs: List[Tuple[str, str, int]] = []
    for i in range(len(rgb3)):
        for j in range(i + 1, len(rgb3)):
            fisheye_pairs.append((rgb3[i], rgb3[j], th(rgb3[i], rgb3[j])))
    d3b_roles = [*depth_roles, *rgb3]
    return {
        base: [
            (base, other, th(base, other))
            for other in d3b_roles
            if other != base and (depth_sync or other != "depth")
        ]
        for base in depth_roles if depth_sync or base != 'depth'
        if len(d3b_roles) > 1
    } | {
        name: fisheye_pairs for name in ('fisheye',) if fisheye_pairs
    }


def _cam_sync_pairs(cams: Dict[str, CameraData],
                    adaptive: bool = False, depth_sync_ms: float = 5.0) -> Optional[List[Tuple[str, str, int]]]:
    """兼容接口：将 D3B 三个同步域展平为同步对列表。"""
    groups = _cam_sync_pair_groups(cams, adaptive=adaptive, depth_sync_ms=depth_sync_ms)
    if groups is None:
        return None
    return [pair for pairs in groups.values() for pair in pairs]


def _cam_sync_by_pairs(cams: Dict[str, CameraData],
                       pairs: List[Tuple[str, str, int]],
                       ctx_pool: Optional[Dict[str, List[int]]] = None,
                       directed: bool = False) -> Dict[str, SyncResult]:
    """按「同步对」计算每路 cam_sync: 每对独立阈值, 双向帧序对齐.

    对每一对 (A,B,thr): A 的帧在 B 中缺近邻 → 记 A 异常; B 的帧在 A 中缺近邻 → 记 B 异常.
    每路结果聚合其参与的所有对. 0 帧超阈 → PASS.
    """
    results: Dict[str, SyncResult] = {}
    # 初始化每路结果 + 该路参与的对 (用于 detail 说明)
    participating = ({pair[0] for pair in pairs} if directed else
                     {role for pair in pairs for role in pair[:2]})
    for role, c in cams.items():
        if role not in participating:
            continue
        thresholds = {threshold for a, b, threshold in pairs
                      if role == a or (not directed and role == b)}
        sr = SyncResult(base_role=role, base_label=c.label,
                        threshold_us=next(iter(thresholds)) if len(thresholds) == 1 else 0,
                        mode="camera")
        sr.total_frames = c.count
        results[role] = sr
    rel: Dict[str, List[str]] = {r: [] for r in participating}
    seen: Dict[str, set] = {r: set() for r in participating}
    all_diffs: Dict[str, List[int]] = {r: [] for r in participating}

    for a_role, b_role, thr in pairs:
        # 同角色配对没有同步意义；防止未来配置错误导致本相机与自身比较。
        if a_role == b_role:
            continue
        a = cams.get(a_role)
        b = cams.get(b_role)
        if a is None or b is None:
            continue
        rel[a_role].append(f"{b.label}({thr / US_PER_MS:g}ms)")
        if not directed:
            rel[b_role].append(f"{a.label}({thr / US_PER_MS:g}ms)")
        b_items = [(t, i) for i, t in enumerate(b.timestamps)]
        b_items.extend((t, None) for t in (ctx_pool or {}).get(b_role, []))
        b_items.sort(key=lambda item: item[0])
        bpool = [item[0] for item in b_items]
        b_meta = [item[1] for item in b_items]
        a_items = [(t, i) for i, t in enumerate(a.timestamps)]
        a_items.extend((t, None) for t in (ctx_pool or {}).get(a_role, []))
        a_items.sort(key=lambda item: item[0])
        apool = [item[0] for item in a_items]
        a_meta = [item[1] for item in a_items]

        # A 的帧在 B 缺近邻 -> 记 A
        for i, t in enumerate(a.timestamps):
            idx = _nearest_idx(bpool, t)
            if idx is None:
                continue
            d = abs(bpool[idx] - t)
            all_diffs[a_role].append(d)
            if b_meta[idx] is None and d < thr:
                results[a_role].context_matches.append(AbnormalFrame(
                    csv_line=a.rows[i] + 2, row_index=a.rows[i],
                    value_us=d, threshold_us=thr, ts_a=t, ts_b=bpool[idx],
                    cam=a_role, cam_label=a.label or a_role,
                    b_role=b_role, b_label=b.label,
                    b_row_index=None, b_csv_line=None,
                    detail=f"{a.label or a_role} → {b.label} 跨段最近邻"))
            if d >= thr:
                key = (b_role, i, b_meta[idx])
                if key in seen[a_role]:
                    continue
                seen[a_role].add(key)
                results[a_role].abnormal.append(AbnormalFrame(
                    csv_line=a.rows[i] + 2, row_index=a.rows[i],
                    value_us=d, threshold_us=thr, ts_a=t, ts_b=bpool[idx],
                    b_role=b_role, b_label=b.label,
                    b_row_index=b.rows[b_meta[idx]] if b_meta[idx] is not None else None,
                    b_csv_line=(b.rows[b_meta[idx]] + 2 if b_meta[idx] is not None else None),
                    detail=(f"基准[{a.label}]→[{b.label}] 最近邻"
                            if directed else f"{a.label} vs {b.label} 最近邻")))
                remember_query(results[a_role], results[a_role].abnormal[-1],
                               a_role, a.label, a.rows[i], t, (b_role,), bpool)
        if directed:
            continue
        # B 的帧在 A 缺近邻 -> 记 B
        for k, t in enumerate(b.timestamps):
            idx = _nearest_idx(apool, t)
            if idx is None:
                continue
            d = abs(apool[idx] - t)
            all_diffs[b_role].append(d)
            if a_meta[idx] is None and d < thr:
                results[b_role].context_matches.append(AbnormalFrame(
                    csv_line=b.rows[k] + 2, row_index=b.rows[k],
                    value_us=d, threshold_us=thr, ts_a=t, ts_b=apool[idx],
                    cam=b_role, cam_label=b.label or b_role,
                    b_role=a_role, b_label=a.label,
                    b_row_index=None, b_csv_line=None,
                    detail=f"{b.label or b_role} → {a.label} 跨段最近邻"))
            if d >= thr:
                key = (a_role, a_meta[idx], k)
                if key in seen[b_role]:
                    continue
                seen[b_role].add(key)
                results[b_role].abnormal.append(AbnormalFrame(
                    csv_line=b.rows[k] + 2, row_index=b.rows[k],
                    value_us=d, threshold_us=thr, ts_a=t, ts_b=apool[idx],
                    b_role=a_role, b_label=a.label,
                    b_row_index=a.rows[a_meta[idx]] if a_meta[idx] is not None else None,
                    b_csv_line=(a.rows[a_meta[idx]] + 2 if a_meta[idx] is not None else None),
                    detail=f"{b.label} 缺帧 (对端 {a.label} 有)"))
                remember_query(results[b_role], results[b_role].abnormal[-1],
                               b_role, b.label, b.rows[k], t, (a_role,), apool)

    for role, sr in results.items():
        sr.context_hits = len(sr.context_matches)
        sr.over_count = len(sr.abnormal)
        sr.passed = sr.over_count == 0
        # max/mean 取「所有匹配帧」偏差 (含未超阈的), 反映真实同步偏差最大值
        ds = all_diffs[role]
        remember_statistics(sr, ds)
        sr.matched_frames = len(ds)
        if ds:
            sorted_diffs = sorted(ds)
            sr.max_diff_us = max(ds)
            sr.mean_diff_us = sum(ds) / len(ds)
            sr.p95_diff_us = _percentile(sorted_diffs, 0.95)
            sr.p50_diff_us = _percentile(sorted_diffs, 0.50)
        relation = (f"基准[{sr.base_label}]→[{'/'.join(rel[role])}] " if directed
                    else f"对[{'+'.join(rel[role])}] ")
        ctx_note = (f" 相邻切片参考命中{sr.context_hits}帧"
                    if sr.context_hits else "")
        sr.detail = (f"{relation}max={sr.max_diff_us}µs "
                     f"mean={sr.mean_diff_us:.0f}µs "
                     f"超阈={sr.over_count}/{sr.matched_frames}{ctx_note}")
    return results


def check_cam_sync_groups(cams: Dict[str, CameraData], adaptive: bool = False,
                          ctx_pool: Optional[Dict[str, List[int]]] = None,
                          depth_sync: bool = True,
                          depth_sync_ms: float = 5.0,
                          ) -> Dict[str, Dict[str, SyncResult]]:
    """分别计算 D3B 三个同步域，保留每组独立 PASS/FAIL 与异常详情。"""
    groups = _cam_sync_pair_groups(cams, adaptive=adaptive, depth_sync=depth_sync,
                                   depth_sync_ms=depth_sync_ms)
    if groups is None:
        return {}
    return {
        name: _cam_sync_by_pairs(
            cams, pairs, ctx_pool=ctx_pool, directed=(name != "fisheye"))
        for name, pairs in groups.items()
    }


def check_cam_sync_grouped(cams: Dict[str, CameraData], adaptive: bool = False,
                           ctx_pool: Optional[Dict[str, List[int]]] = None,
                           depth_sync: bool = True,
                           depth_sync_ms: float = 5.0,
                           ) -> Dict[str, SyncResult]:
    """按「同步对」计算相机基准同步 (深度产品专用; 每对独立阈值, 双向帧序对齐)."""
    groups = _cam_sync_pair_groups(cams, adaptive=adaptive)
    if groups is None:
        return check_cam_sync({role: camera for role, camera in cams.items()
                               if depth_sync
                               or role.split('__', 1)[0] != 'depth'})
    grouped = check_cam_sync_groups(
        cams, adaptive=adaptive, ctx_pool=ctx_pool, depth_sync=depth_sync,
        depth_sync_ms=depth_sync_ms)
    # 三个同步域的基准角色不重叠：两个深度域分别返回 depth、
    # depth_rgb，鱼眼域返回 left/right/up，合并后兼容原 cam_sync 接口。
    return {
        role: result
        for name in ("depth", "depth_rgb", "fisheye")
        for role, result in grouped.get(name, {}).items()
    }


def check_imu_sync(cams: Dict[str, CameraData], imu: List[int],
                   threshold_us: int = IMU_SYNC_THRESHOLD_US,
                   context_timestamps: Optional[Set[int]] = None,
                   source_rows: Optional[SourceRows] = None) -> Dict[str, SyncResult]:
    """IMU 时间戳同步: 每目相机为基准匹配 IMU. 阈值 1ms; 按 max 峰值分档."""
    results: Dict[str, SyncResult] = {}
    if not imu:
        return results
    # Membership marker only; the matching sequence below retains every record.
    context_timestamps = set(context_timestamps or ())
    spool = sorted(imu)   # IMU 池对所有相机相同, 只排一次
    lookup = safe_int64(spool)
    for base_role, base in cams.items():
        sr = SyncResult(base_role=base_role, base_label=base.label,
                        threshold_us=threshold_us, mode="imu")
        sr.total_frames = base.count
        diffs = []
        indexes = nearest_indexes(lookup, base.timestamps)
        for i, t in enumerate(base.timestamps):
            idx = indexes[i] if indexes is not None else _nearest_idx(spool, t)
            if idx is None:
                continue
            d = abs(spool[idx] - t)
            diffs.append(d)
            if context_timestamps and spool[idx] in context_timestamps:
                sr.context_matches.append(AbnormalFrame(
                    csv_line=base.rows[i] + 2, row_index=base.rows[i],
                    value_us=d, threshold_us=threshold_us,
                    ts_a=t, ts_b=spool[idx],
                    b_role="imu", b_label="IMU",
                    # idx 属于“本段 + 相邻段”的排序池，不是源 Part 行号；
                    # 源样本序号由 ImuBoundarySample 在报告阶段精确回溯。
                    b_row_index=None, b_csv_line=None,
                    detail=f"{base.label} vs 跨段 IMU 最近邻"))
            if d >= threshold_us:
                # 本段 CSV/MCAP 使用真实源数据行；跨段样本由上下文详情单独
                # 回溯。没有来源映射时显示未知，不用中间索引冒充源文件行号。
                if context_timestamps and spool[idx] in context_timestamps:
                    source_row = None
                elif source_rows is None:
                    source_row = None
                else:
                    source_row = source_rows.get(spool[idx])
                sr.abnormal.append(AbnormalFrame(
                    csv_line=base.rows[i] + 2, row_index=base.rows[i],
                    value_us=d, threshold_us=threshold_us,
                    ts_a=t, ts_b=spool[idx],
                    b_role="imu", b_label="IMU",
                    b_row_index=source_row,
                    b_csv_line=(source_row + 2 if source_row is not None else None),
                    detail=f"{base.label} vs IMU 最近邻"))
                remember_query(sr, sr.abnormal[-1], base_role, base.label,
                               base.rows[i], t, ('imu',), spool)
        sr.matched_frames = len(diffs)
        remember_statistics(sr, diffs)
        if diffs:
            sd = sorted(diffs)
            sr.max_diff_us = max(diffs)
            sr.mean_diff_us = sum(diffs) / len(diffs)
            sr.p95_diff_us = _percentile(sd, 0.95)
            sr.p50_diff_us = _percentile(sd, 0.50)
        sr.over_count = len(sr.abnormal)
        sr.context_hits = len(sr.context_matches)
        sr.passed = sr.max_diff_us < threshold_us   # max 峰值分档
        sr.detail = f"max={sr.max_diff_us}µs mean={sr.mean_diff_us:.0f}µs 超阈={sr.over_count}"
        results[base_role] = sr
    return results


def grip_theoretical_interval_us(frequency_hz: float) -> float:
    """Use a configured frequency without timestamp inference or rate snapping."""
    if isinstance(frequency_hz, bool):
        raise ValueError('夹爪JSON配置频率必须为正数')
    try:
        rate = float(frequency_hz)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('夹爪JSON配置频率必须为正数') from exc
    if not math.isfinite(rate) or rate <= 0 or not math.isfinite(1_000_000.0 / rate):
        raise ValueError('夹爪JSON配置频率必须为正数')
    return 1_000_000.0 / rate


def check_grip_sync(cams: Dict[str, CameraData], grip: List[int],
                    threshold_us: Optional[float] = None, *,
                    frequency_hz: Optional[float] = None,
                    frequency_source: str = '') -> Dict[str, SyncResult]:
    """Match same-device samples using an explicitly supplied JSON frequency/limit."""
    results: Dict[str, SyncResult] = {}
    spool = sorted(grip)
    if not spool:
        return results
    if frequency_hz is not None:
        effective_threshold = grip_theoretical_interval_us(frequency_hz)
    elif threshold_us is not None:
        effective_threshold = float(threshold_us)
        if not math.isfinite(effective_threshold) or effective_threshold <= 0:
            raise ValueError('夹爪同步阈值必须为正数')
    else:
        raise ValueError('夹爪同步缺少JSON配置频率，不使用时间戳推算阈值')
    for base_role, base in cams.items():
        sr = SyncResult(base_role=base_role, base_label=base.label,
                        threshold_us=effective_threshold, mode="grip",
                        frequency_hz=frequency_hz, frequency_source=frequency_source)
        sr.total_frames = base.count
        diffs = []
        for i, timestamp in enumerate(base.timestamps):
            idx = _nearest_idx(spool, timestamp)
            if idx is None:
                continue
            diff = abs(spool[idx] - timestamp)
            diffs.append(diff)
            if diff >= effective_threshold:
                sr.abnormal.append(AbnormalFrame(
                    csv_line=base.rows[i] + 2, row_index=base.rows[i],
                    value_us=diff, threshold_us=effective_threshold,
                    ts_a=timestamp, ts_b=spool[idx],
                    b_role="grip", b_label="夹爪",
                    b_row_index=idx, b_csv_line=idx + 2,
                    detail=f"{base.label} vs 同设备夹爪最近邻"))
        sr.matched_frames = len(diffs)
        if diffs:
            sorted_diffs = sorted(diffs)
            sr.max_diff_us = max(diffs)
            sr.mean_diff_us = sum(diffs) / len(diffs)
            sr.p95_diff_us = _percentile(sorted_diffs, 0.95)
            sr.p50_diff_us = _percentile(sorted_diffs, 0.50)
        sr.over_count = len(sr.abnormal)
        sr.passed = bool(diffs) and sr.max_diff_us < effective_threshold
        sr.detail = (f"max={sr.max_diff_us}µs mean={sr.mean_diff_us:.0f}µs "
                     f"阈值<{effective_threshold}µs 超阈={sr.over_count}")
        results[base_role] = sr
    return results


def check_rgb_drop(cam: CameraData, expected_us: float,
                   multiplier: float = RGB_DROP_MULTIPLIER) -> DropResult:
    """RGB 连续性: 检查丢帧、时间戳倒退与重复.

    丢帧数 = Σ round(间隔 / 理论间隔) - 1 (间隔 > 理论间隔才计), 即按理论间隔
    (expected = 1/帧率) 推算每段间隔实际缺失的帧数, 不用 1.5× 阈值筛选.
    判定 PASS/FAIL 按 max 峰值 vs 1.5×理论间隔，并把倒退、重复作为独立异常。
    """
    r = DropResult(role=cam.role, label=cam.label)
    ts = cam.timestamps
    if len(ts) < 2:
        r.detail = "帧数不足, 跳过"
        r.passed = True
        return r
    if expected_us <= 0:
        expected_us = compute_expected_interval(ts)
    r.threshold_us = expected_us * multiplier
    gaps = []
    backsteps = []
    duplicates = []
    for i in range(len(ts) - 1):
        iv = ts[i + 1] - ts[i]
        if iv < 0:
            backsteps.append((i, iv))
            continue
        if iv == 0:
            duplicates.append((i, iv))
            continue
        # 按理论间隔统计: 缺失帧数 = round(间隔/理论) - 1; 仅当 >=1 (即间隔≥1.5×理论附近) 才计
        lost = round(iv / max(1.0, expected_us)) - 1
        if iv >= r.threshold_us and lost >= 1:
            gaps.append((i, iv, lost))
    r.total_gaps = len(gaps)
    r.total_lost_frames = sum(g[2] for g in gaps)
    r.backstep_count = len(backsteps)
    r.duplicate_count = len(duplicates)
    r.max_gap_us = max((g[1] for g in gaps), default=0)
    r.mean_gap_us = (sum(g[1] for g in gaps) / len(gaps)) if gaps else 0.0
    drop_abnormal = [AbnormalFrame(
        csv_line=cam.rows[i] + 2, row_index=cam.rows[i], value_us=iv,
        threshold_us=int(expected_us),
        ts_a=ts[i], ts_b=ts[i + 1], lost=lost, cam=cam.label,
        detail=f"帧间隔{iv/1000:.3f}ms>理论{expected_us/1000:.3f}ms, 丢失≈{lost}帧",
        anomaly_type="丢帧")
        for i, iv, lost in gaps]
    backstep_abnormal = [AbnormalFrame(
        csv_line=cam.rows[i] + 2, row_index=cam.rows[i], value_us=iv,
        threshold_us=0, ts_a=ts[i], ts_b=ts[i + 1], lost=0, cam=cam.label,
        detail=f"相机时间戳从{ts[i]}倒退到{ts[i + 1]}，倒退{-iv/1000:.3f}ms",
        anomaly_type="倒退")
        for i, iv in backsteps]
    duplicate_abnormal = [AbnormalFrame(
        csv_line=cam.rows[i] + 2, row_index=cam.rows[i], value_us=iv,
        threshold_us=0, ts_a=ts[i], ts_b=ts[i + 1], lost=0, cam=cam.label,
        detail=f"相邻相机时间戳重复：{ts[i]}", anomaly_type="重复")
        for i, iv in duplicates]
    r.abnormal = sorted(drop_abnormal + backstep_abnormal + duplicate_abnormal,
                        key=lambda item: item.row_index)
    r.passed = not r.abnormal
    r.detail = (f"丢帧数={r.total_lost_frames} 事件={r.total_gaps} "
                f"倒退={r.backstep_count} 重复={r.duplicate_count} "
                f"max间隔={r.max_gap_us}µs")
    return r


def check_imu_drop(imu: List[int],
                   multiplier: float = IMU_DROP_MULTIPLIER,
                   *, detect_duplicates: bool = False,
                   stream_label: str = "IMU",
                   frequency_hz: Optional[float] = None,
                   infer_frequency: bool = False) -> DropResult:
    """按原始记录顺序检查 IMU 丢帧与时间戳倒退。

    丢帧数 = Σ round(间隔 / 理论间隔) - 1 (间隔 > 理论间隔才计), 即按理论间隔
    (expected = 1/IMU频率) 推算缺失帧数。负间隔记为倒退；相同时间戳允许
    accel/gyro 共用，不记异常。IMU理论频率由上游按“当前录制JSON优先、无匹配JSON时
    按时间戳计算并在±15Hz内吸附标称频率”解析后传入；夹爪调用可显式启用节拍估计。
    原始记录顺序与数量保持不变。
    """
    r = DropResult(role="imu", label=stream_label)
    ts = list(imu)
    rate_basis = ts
    r.total_frames = len(ts)
    if len(ts) < 2:
        r.detail = f"{stream_label}数据不足, 跳过"
        r.passed = True
        return r
    # IMU 使用上游已经解析的标称频率；只有夹爪等调用方显式要求时，
    # 本函数才直接从时间戳估计固定节拍。
    rate = frequency_hz
    if rate is None and infer_frequency:
        rate = auto_detect_rate(rate_basis)
    if rate is None or rate <= 0:
        r.detail = f"{stream_label}缺少有效标称频率，未执行丢帧检查"
        r.passed = False
        return r
    expected_us = (1_000_000.0 / rate if rate > 0
                   else compute_expected_interval(rate_basis))
    r.threshold_us = expected_us * multiplier
    events = gap_events(ts, expected_us, r.threshold_us, detect_duplicates)
    if events is not None:
        gaps, backsteps, duplicates = events
    else:
        # Preserve unlimited Python integers and unusual numeric inputs.
        gaps, backsteps, duplicates = [], [], []
        for i in range(len(ts) - 1):
            iv = ts[i + 1] - ts[i]
            if iv < 0:
                backsteps.append((i, iv))
                continue
            if iv == 0 and detect_duplicates:
                duplicates.append((i, iv))
                continue
            lost = round(iv / max(1.0, expected_us)) - 1
            if iv >= r.threshold_us and lost >= 1:
                gaps.append((i, iv, lost))
    r.total_gaps = len(gaps)
    r.total_lost_frames = sum(g[2] for g in gaps)
    r.backstep_count = len(backsteps)
    r.duplicate_count = len(duplicates)
    r.max_gap_us = max((g[1] for g in gaps), default=0)
    r.mean_gap_us = (sum(g[1] for g in gaps) / len(gaps)) if gaps else 0.0
    drop_abnormal = [AbnormalFrame(
        csv_line=i + 2, row_index=i, value_us=iv,
        threshold_us=int(expected_us),
        ts_a=ts[i], ts_b=ts[i + 1], lost=lost, cam=stream_label,
        detail=f"{stream_label}间隔{iv/1000:.3f}ms>理论{expected_us/1000:.3f}ms, 丢失≈{lost}帧",
        anomaly_type="丢帧")
        for i, iv, lost in gaps]
    backstep_abnormal = [AbnormalFrame(
        csv_line=i + 2, row_index=i, value_us=iv,
        threshold_us=0, ts_a=ts[i], ts_b=ts[i + 1], lost=0, cam=stream_label,
        detail=f"{stream_label}时间戳从{ts[i]}倒退到{ts[i + 1]}，倒退{-iv/1000:.3f}ms",
        anomaly_type="倒退")
        for i, iv in backsteps]
    duplicate_abnormal = [AbnormalFrame(
        csv_line=i + 2, row_index=i, value_us=iv,
        threshold_us=0, ts_a=ts[i], ts_b=ts[i + 1], lost=0, cam=stream_label,
        detail=f"{stream_label}相邻时间戳重复：{ts[i]}",
        anomaly_type="重复")
        for i, iv in duplicates]
    r.abnormal = sorted(drop_abnormal + backstep_abnormal + duplicate_abnormal,
                        key=lambda item: item.row_index)
    r.passed = not r.abnormal
    r.detail = (f"{stream_label}丢帧数={r.total_lost_frames} 丢帧事件={r.total_gaps} "
                f"倒退={r.backstep_count} 重复={r.duplicate_count} "
                f"max间隔={r.max_gap_us}µs")
    return r


def check_imu_drop_sources(sources: List[List[int]],
                           multiplier: float = IMU_DROP_MULTIPLIER,
                           *, detect_duplicates: bool = True,
                           stream_label: str = "IMU",
                           deduplicate_sources: bool = False,
                           frequency_hz: Optional[float] = None,
                           infer_frequency: bool = False) -> DropResult:
    """分别检查独立 IMU 文件/MCAP 通道，避免跨来源边界产生伪异常。

    部分采集包会分别保存 accel/gyro CSV，但两份文件使用完全相同的逻辑帧时间轴。
    每个来源独立统计，即使时间戳完全相同也保留所有记录。
    """
    # Compatibility argument retained; raw sources are always counted independently.
    distinct_sources = [source for source in sources if source]
    checked = [check_imu_drop(source, multiplier=multiplier,
                              detect_duplicates=detect_duplicates,
                              stream_label=stream_label,
                              frequency_hz=frequency_hz,
                              infer_frequency=infer_frequency)
               for source in distinct_sources]
    for source, item in zip(distinct_sources, checked):
        rows = getattr(source, 'source_rows', ())
        label = '/'.join(filter(None, (getattr(source, 'source', ''),
                                      getattr(source, 'sensor_type', ''))))
        for abnormal in item.abnormal:
            index = abnormal.row_index
            if index < len(rows):
                abnormal.row_index = rows[index]
                abnormal.csv_line = rows[index] + 2
                if index + 1 < len(rows):
                    abnormal.b_row_index = rows[index + 1]
                    abnormal.b_csv_line = rows[index + 1] + 2
            if label:
                abnormal.detail += f" [来源: {label}]"
    if not checked:
        return check_imu_drop([], multiplier=multiplier,
                              detect_duplicates=detect_duplicates,
                              stream_label=stream_label,
                              frequency_hz=frequency_hz,
                              infer_frequency=infer_frequency)
    if len(checked) == 1:
        return checked[0]
    result = DropResult(role="imu", label=stream_label)
    result.total_frames = sum(item.total_frames for item in checked)
    result.total_gaps = sum(item.total_gaps for item in checked)
    result.total_lost_frames = sum(item.total_lost_frames for item in checked)
    result.backstep_count = sum(item.backstep_count for item in checked)
    result.duplicate_count = sum(item.duplicate_count for item in checked)
    result.max_gap_us = max((item.max_gap_us for item in checked), default=0)
    gap_count = sum(item.total_gaps for item in checked)
    result.mean_gap_us = (sum(item.mean_gap_us * item.total_gaps for item in checked)
                          / gap_count if gap_count else 0.0)
    result.threshold_us = max((item.threshold_us for item in checked), default=0.0)
    result.abnormal = [abnormal for item in checked for abnormal in item.abnormal]
    result.passed = all(item.passed for item in checked)
    result.detail = (f"{stream_label}丢帧数={result.total_lost_frames} "
                     f"丢帧事件={result.total_gaps} 倒退={result.backstep_count} "
                     f"重复={result.duplicate_count} max间隔={result.max_gap_us}µs")
    return result


def check_stream_drop(ts: List[int], role: str, label: str,
                      tolerance_us: int = 1000) -> DropResult:
    """按「实测中位间隔」为理论周期做丢帧统计 (音频等固定节拍流).

    理论间隔 expected = 相邻间隔中位数 (如音频 ~42.6ms);
    阈值 = expected + tolerance_us (上下浮动容差, 默认 1ms);
    丢帧数 = round(间隔/expected) - 1 (倍数, ≥1 才计);
    PASS 当所有间隔 ≤ 阈值 (即无超容差的断流)。
    """
    r = DropResult(role=role, label=label)
    ts = list(ts)
    if len(ts) < 2:
        r.detail = "数据不足, 跳过"
        r.passed = True
        return r
    ivs = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
    ivs_pos = [x for x in ivs if x > 0] or [1.0]
    expected_us = _median(ivs_pos)          # 理论间隔 = 中位间隔
    r.threshold_us = expected_us + tolerance_us
    gaps = []
    for i, iv in enumerate(ivs):
        lost = round(iv / max(1.0, expected_us)) - 1
        if iv > r.threshold_us and lost >= 1:
            gaps.append((i, iv, lost))
    r.total_gaps = len(gaps)
    r.total_lost_frames = sum(g[2] for g in gaps)
    r.max_gap_us = max((g[1] for g in gaps), default=0)
    r.mean_gap_us = (sum(g[1] for g in gaps) / len(gaps)) if gaps else 0.0
    r.abnormal = [AbnormalFrame(
        csv_line=i + 2, row_index=i, value_us=iv,
        threshold_us=int(r.threshold_us),
        ts_a=ts[i], ts_b=ts[i + 1], lost=lost, cam=label,
        detail=f"{label}间隔{iv/1000:.3f}ms>理论{expected_us/1000:.3f}ms"
               f"+容差{tolerance_us/1000:.3f}ms, 丢失≈{lost}帧")
        for i, iv, lost in gaps]
    r.passed = len(gaps) == 0
    r.detail = (f"{label}丢帧数={r.total_lost_frames} 事件={r.total_gaps} "
                f"理论间隔={expected_us/1000:.3f}ms max间隔={r.max_gap_us/1000:.3f}ms")
    return r


def check_imu_groups_diff(device_imus: List[List[int]]) -> SyncResult:
    """多组 IMU 差值: 各设备 IMU 两两最近邻对齐, 汇总 max/mean (µs).

    device_imus: 每设备一条 IMU 时间戳列表 (已升序). 返回 SyncResult (mode='imu').
    用双指针最近邻 O(n), 避免逐元素 bisect.
    """
    # Sorting is an auxiliary search index; retain duplicate records and leave
    # each device's raw arrival sequence untouched.
    device_imus = [sorted(timestamps) for timestamps in device_imus]
    sr = SyncResult(mode="imu", threshold_us=0)
    all_diffs: List[int] = []
    n = len(device_imus)
    for i in range(n):
        base = device_imus[i]
        if not base:
            continue
        for j in range(i + 1, n):
            other = device_imus[j]
            if not other:
                continue
            all_diffs.extend(_nearest_diffs_sorted(base, other))
    if all_diffs:
        sd = sorted(all_diffs)
        sr.matched_frames = len(all_diffs)
        sr.max_diff_us = max(all_diffs)
        sr.mean_diff_us = sum(all_diffs) / len(all_diffs)
        sr.p95_diff_us = _percentile(sd, 0.95)
        sr.p50_diff_us = _percentile(sd, 0.50)
    sr.detail = f"max={sr.max_diff_us}µs mean={sr.mean_diff_us:.0f}µs 组间对={sr.matched_frames}"
    return sr


def _adaptive_rate_threshold_ms(fps: float) -> float:
    """帧间隔波动 / 同行对齐 自适应阈值 (ms): 30fps→10ms, 60fps→5ms; 其余按比例 300/fps."""
    if fps and fps > 0:
        return 300.0 / fps
    return 10.0


def _adaptive_cam_sync_ms(role_a: str, role_b: str) -> float:
    """相机同步自适应阈值 (ms): RGB&IR 之间 1ms; depth 与 depth_rgb / RGB 之间 5ms."""
    ra, rb = role_a.lower(), role_b.lower()
    depth_like = ("depth",)
    a_depth = any(t in ra for t in depth_like)
    b_depth = any(t in rb for t in depth_like)
    if a_depth or b_depth:
        return 5.0
    return 1.0


def _adaptive_imu_sync_ms(frequency_hz: Optional[float]) -> float:
    """Use the declared IMU rate; timestamps never define the synchronization threshold."""
    if frequency_hz is None or frequency_hz <= 0:
        return 1.0
    return 1000.0 / frequency_hz


def _adaptive_cam_sync_ms_single(cams: Dict[str, "CameraData"]) -> float:
    """非同步对(无 depth)数据的相机同步自适应阈值 (ms): 取所有相机两两阈值的最大值."""
    roles = list(cams.keys())
    if not roles:
        return 1.0
    best = 1.0
    for i in range(len(roles)):
        for j in range(i + 1, len(roles)):
            best = max(best, _adaptive_cam_sync_ms(roles[i], roles[j]))
    return best
