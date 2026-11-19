"""Incremental edge matching; successful local pairs are never rematched."""
from collections import Counter
from bisect import bisect_left
from dataclasses import dataclass, replace

from ego_checker.domain.models import AbnormalFrame


@dataclass
class EdgeQuery:
    event: AbnormalFrame
    role: str
    label: str
    row: int
    timestamp: int
    targets: tuple
    direction: str


def remember_query(result, event, role, label, row, timestamp, targets, pool):
    direction = ("previous" if timestamp < pool[0] else
                 "next" if timestamp > pool[-1] else "")
    result.edge_queries.append(EdgeQuery(
        event, role, label, row, timestamp, tuple(targets), direction))


def remember_statistics(result, diffs):
    if any(query.direction for query in result.edge_queries):
        result.diff_counts = Counter(diffs)
    else:
        result.edge_queries.clear()


def local_sync(result):
    return result.local_result or result


def pending_directions(sync):
    return {query.direction for item in sync.values()
            for query in local_sync(item).edge_queries if query.direction}


def retry_edges(result, candidates, checkpoint=None, labels=None):
    """Candidates: direction -> target role -> [(timestamp, physical row)].

    Keep failed local evidence unless an adjacent reference actually passes its
    threshold. Every query (including both camera directions) contributes its
    own deviation to the statistics; shared anomaly display entries stay until
    all associated queries pass. No timestamp/record deduplication is performed.
    """
    local = local_sync(result)
    if not local.diff_counts or not candidates:
        return local
    counts = Counter(local.diff_counts)
    indexed = {direction: {role: sorted(records, key=lambda item: item[0])
                          for role, records in roles.items()}
               for direction, roles in candidates.items()}
    resolved, unresolved, matches = set(), set(), []
    for index, query in enumerate(local.edge_queries):
        if checkpoint and index % 256 == 0:
            checkpoint()
        best = None
        for role in query.targets:
            records = indexed.get(query.direction, {}).get(role, ())
            pos = bisect_left(records, query.timestamp, key=lambda item: item[0])
            for timestamp, row in records[max(0, pos-1):pos+1]:
                diff = abs(timestamp - query.timestamp)
                if best is None or diff < best[0]:
                    best = diff, timestamp, row, role
        event = query.event
        if best is None or best[0] >= event.threshold_us:
            unresolved.add(id(event))
            continue
        diff, timestamp, row, role = best
        resolved.add(id(event))
        counts[event.value_us] -= 1
        counts[diff] += 1
        matches.append(AbnormalFrame(
            csv_line=query.row + 2, row_index=query.row,
            value_us=diff, threshold_us=event.threshold_us,
            ts_a=query.timestamp, ts_b=timestamp,
            cam=query.role, cam_label=query.label,
            b_role=role, b_label=(labels or {}).get(role, role),
            b_row_index=row, b_csv_line=row + 2 if row is not None else None,
            detail=f"{query.label} → {role} 首尾异常跨段补查"))
    if not matches:
        return local
    abnormal = [event for event in local.abnormal
                if id(event) not in resolved or id(event) in unresolved]
    values = sorted((value, count) for value, count in counts.items() if count > 0)
    count = sum(n for _, n in values)

    def percentile(p):
        # Same interpolated percentile as domain.numeric._percentile.
        from math import floor, ceil
        k = (count - 1) * p
        def at(rank):
            for value, n in values:
                if rank < n:
                    return value
                rank -= n
        lo, hi = at(floor(k)), at(ceil(k))
        return lo + int((hi-lo) * (k-floor(k)))

    updated = replace(local, abnormal=abnormal, over_count=len(abnormal),
                      passed=not abnormal, context_matches=matches,
                      context_hits=len(matches), local_result=local,
                      max_diff_us=values[-1][0],
                      mean_diff_us=sum(v*n for v, n in values) / count,
                      p95_diff_us=percentile(.95), p50_diff_us=percentile(.50))
    prefix = local.detail.split('max=', 1)[0]
    updated.detail = (f"{prefix}max={updated.max_diff_us}µs mean={updated.mean_diff_us:.0f}µs "
                      f"超阈={updated.over_count}/{updated.matched_frames} "
                      f"首尾异常跨段补查通过{len(matches)}帧")
    return updated
