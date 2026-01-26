from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional


class Metrics:
    def __init__(self) -> None:
        self._counters: Dict[str, int] = defaultdict(int)
        self._reason_counters: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._histograms: Dict[str, List[float]] = defaultdict(list)
        self._max_samples = 2000

    def reset(self) -> None:
        self._counters.clear()
        self._reason_counters.clear()
        self._histograms.clear()

    def inc(self, name: str, n: int = 1) -> None:
        if not name:
            return
        self._counters[str(name)] += int(n)

    def inc_reason(self, group: str, reason: str, n: int = 1) -> None:
        if not group or not reason:
            return
        self._reason_counters[str(group)][str(reason)] += int(n)

    def observe(self, name: str, value: float) -> None:
        if not name:
            return
        try:
            v = float(value)
        except Exception:
            return
        if v != v:  # NaN
            return
        bucket = self._histograms[str(name)]
        bucket.append(float(v))
        if len(bucket) > self._max_samples:
            del bucket[: len(bucket) - self._max_samples]

    @staticmethod
    def _percentile(vals: List[float], pct: float) -> Optional[float]:
        if not vals:
            return None
        v = sorted(vals)
        if len(v) == 1:
            return float(v[0])
        k = max(0, min(len(v) - 1, int(round((pct / 100.0) * (len(v) - 1)))))
        return float(v[k])

    def snapshot(self) -> Dict[str, Any]:
        hist_stats: Dict[str, Any] = {}
        for name, vals in self._histograms.items():
            p50 = self._percentile(vals, 50.0)
            p95 = self._percentile(vals, 95.0)
            hist_stats[name] = {
                "count": len(vals),
                "p50": p50,
                "p95": p95,
            }

        reasons: Dict[str, Dict[str, int]] = {}
        for group, counts in self._reason_counters.items():
            reasons[group] = dict(counts)

        return {
            "counters": dict(self._counters),
            "reason_counters": reasons,
            "histograms": hist_stats,
        }


METRICS = Metrics()

