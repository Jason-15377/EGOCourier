"""Integer-only batch operations; unusual values retain Python semantics."""
import math

import numpy as np


def safe_int64(values):
    """Keep every record and reject conversions that could lose precision."""
    try:
        array = np.asarray(values)
    except (OverflowError, TypeError, ValueError):
        return None
    if not array.size:
        return np.empty(0, dtype=np.int64)
    if array.ndim != 1 or array.dtype.kind not in 'iu':
        return None
    # Every subtraction and absolute difference then fits signed int64.
    if int(array.min()) < -(2**62) or int(array.max()) >= 2**62:
        return None
    return array.astype(np.int64, copy=False)


def nearest_indexes(values, queries):
    """Return bisect_left-compatible indexes, or None for the scalar path."""
    query = safe_int64(queries)
    if values is None or query is None:
        return None
    if not values.size:
        return [None] * len(query)
    position = np.searchsorted(values, query, side='left')
    right = np.minimum(position, values.size - 1)
    left = np.maximum(position - 1, 0)
    choose_left = ((position == values.size) | ((position > 0)
                   & (np.abs(values[left] - query) < np.abs(values[right] - query))))
    # Strict < preserves the original right-side choice on equal-distance ties.
    return np.where(choose_left, left, right).tolist()


def gap_events(timestamps, expected_us, threshold_us, detect_duplicates):
    """Select candidates in batches, retaining Python rounding and event order."""
    values = safe_int64(timestamps)
    if (values is None or not math.isfinite(expected_us)
            or not math.isfinite(threshold_us)):
        return None
    cutoff = math.ceil(threshold_us)
    if not -(2**63) <= cutoff < 2**63:
        return None
    delta = np.diff(values)
    backsteps = [(int(i), int(delta[i])) for i in np.flatnonzero(delta < 0)]
    duplicates = ([(int(i), 0) for i in np.flatnonzero(delta == 0)]
                  if detect_duplicates else [])
    gaps = []
    for index in np.flatnonzero((delta > 0) & (delta >= cutoff)):
        interval = int(delta[index])
        lost = round(interval / max(1.0, expected_us)) - 1
        if lost >= 1:
            gaps.append((int(index), interval, lost))
    return gaps, backsteps, duplicates
