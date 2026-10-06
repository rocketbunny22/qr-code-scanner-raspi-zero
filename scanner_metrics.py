"""Bounded, thread-safe latency samples; never stores badge data."""

from collections import defaultdict, deque
import math
import threading


class Metrics:
    def __init__(self, capacity=2048):
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._samples = defaultdict(lambda: deque(maxlen=capacity))
        self._counts = defaultdict(int)
        self._lock = threading.Lock()

    def observe(self, name, seconds):
        if not math.isfinite(seconds) or seconds < 0:
            return
        with self._lock:
            self._samples[name].append(seconds)
            self._counts[name] += 1

    def summary(self):
        """Return rolling-window percentiles plus counts since startup."""
        with self._lock:
            snapshot = {name: (list(values), self._counts[name])
                        for name, values in self._samples.items()}
        result = {}
        for name, (values, count) in snapshot.items():
            values.sort()
            result[name] = {
                "count": count,
                "samples": len(values),
                "p50_ms": values[math.ceil(len(values) * 0.50) - 1] * 1000,
                "p95_ms": values[math.ceil(len(values) * 0.95) - 1] * 1000,
            }
        return result
