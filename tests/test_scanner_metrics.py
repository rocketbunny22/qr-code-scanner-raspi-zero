import unittest

from scanner_metrics import Metrics


class MetricsTests(unittest.TestCase):
    def test_bounds_samples_but_preserves_total_count(self):
        metrics = Metrics(capacity=2)
        for value in (1, 2, 3):
            metrics.observe("decode", value / 1000)
        self.assertEqual(metrics.summary()["decode"], {
            "count": 3, "samples": 2, "p50_ms": 2, "p95_ms": 3,
        })

    def test_ignores_invalid_durations(self):
        metrics = Metrics()
        for value in (-1, float("nan"), float("inf")):
            metrics.observe("invalid", value)
        self.assertEqual(metrics.summary(), {})

    def test_rejects_zero_capacity(self):
        with self.assertRaises(ValueError):
            Metrics(0)
