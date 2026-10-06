import threading
import unittest

from scanner_feedback import FeedbackWorker
from scanner_metrics import Metrics


class FeedbackWorkerTests(unittest.TestCase):
    def test_result_does_not_need_decoder_loop_and_expires(self):
        shown = threading.Event()
        ready = threading.Event()
        events = []
        def present(event):
            events.append(event["kind"])
            shown.set()
            return 0.01
        metrics = Metrics()
        worker = FeedbackWorker(present, ready.set, metrics, metrics_interval=0)
        worker.start()
        try:
            worker.put({"kind": "result"})
            self.assertTrue(shown.wait(1))
            self.assertTrue(ready.wait(1))
            self.assertEqual(events, ["result"])
            self.assertIn("feedback_queue_wait", metrics.summary())
        finally:
            self.assertTrue(worker.stop())

    def test_stop_drains_already_queued_feedback(self):
        received = []
        worker = FeedbackWorker(lambda event: received.append(event["kind"]),
                                lambda: None, Metrics(), metrics_interval=0)
        worker.start()
        worker.put({"kind": "accepted"})
        worker.put({"kind": "result"})
        self.assertTrue(worker.stop())
        self.assertEqual(received, ["accepted", "result"])
