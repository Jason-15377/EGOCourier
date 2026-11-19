"""分析与截帧参数解析，无GUI前置依赖。"""
import random


def parse_oos_group_limit(text):
    """Return None for ALL/blank, otherwise a positive group limit."""
    if text is None:
        return None
    value = str(text).strip()
    if not value or value.upper() == "ALL":
        return None
    if not value.isdecimal() or int(value) <= 0:
        raise ValueError("不同步截取组数必须为正整数")
    return int(value)


def parse_capture_part_limit(text):
    """Return None for ALL/blank, otherwise an integer part target."""
    if text is None:
        return None
    value = str(text).strip()
    if not value or value.upper() == "ALL":
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError("截取帧Part数必须为整数或 ALL") from exc


def select_capture_part_indexes(results, limit, rng=None):
    """选择要截帧的 Part 下标；首尾必选，中间优先抽取不同步 Part。"""
    count = len(results)
    if limit is None or count <= 2 or limit >= count:
        return list(range(count))

    target = max(2, limit)
    # 分析失败的中间 Part 没有可用相机源，不占随机截取名额；首尾仍按硬规则保留，
    # Worker 会明确记录无法截取的原因。
    middle = [index for index in range(1, count - 1)
              if not getattr(results[index], "error", "")]
    unsynced = [
        index for index in middle
        if any(getattr(sync, "over_count", 0) > 0 or getattr(sync, "abnormal", None)
               for sync in results[index].cam_sync.values())
    ]
    unsynced_set = set(unsynced)
    normal = [index for index in middle if index not in unsynced_set]
    picker = rng or random.Random()
    quota = target - 2
    chosen = picker.sample(unsynced, min(quota, len(unsynced)))
    quota -= len(chosen)
    if quota:
        chosen.extend(picker.sample(normal, min(quota, len(normal))))
    return sorted([0, *chosen, count - 1])
