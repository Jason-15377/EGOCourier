"""domain.numeric 的实现。"""
import bisect
import math
from typing import List, Optional


def _nearest_idx(sorted_ts: List[int], t: int) -> Optional[int]:
    if not sorted_ts:
        return None
    i = bisect.bisect_left(sorted_ts, t)
    best: Optional[int] = None
    if i < len(sorted_ts):
        best = i
    if i > 0:
        cand = i - 1
        if best is None or abs(sorted_ts[cand] - t) < abs(sorted_ts[best] - t):
            best = cand
    return best


def _nearest_diffs_sorted(a: List[int], b: List[int]) -> List[int]:
    """两个已升序排序列表, 对 a 中每个元素在 b 中找最近邻的差值.

    双指针 O(len(a)+len(b)), 比逐元素 bisect O(n log n) 快得多.
    """
    diffs: List[int] = []
    nb = len(b)
    j = 0
    for x in a:
        while j + 1 < nb and b[j + 1] <= x:
            j += 1
        best = abs(b[j] - x)
        if j + 1 < nb:
            d2 = abs(b[j + 1] - x)
            if d2 < best:
                best = d2
        diffs.append(best)
    return diffs


def _percentile(sorted_vals: List[int], p: float) -> int:
    if not sorted_vals:
        return 0
    k = (len(sorted_vals) - 1) * p
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + int((sorted_vals[hi] - sorted_vals[lo]) * (k - lo))


def _median(vals: List[int]) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    n = len(s)
    if n % 2 == 1:
        return float(s[n // 2])
    return (s[n // 2 - 1] + s[n // 2]) / 2.0
