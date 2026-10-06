"""Deliver operator feedback independently of camera decoding and disk writes."""

import json
import queue
import threading
import time


class FeedbackWorker:
    def __init__(self, present, ready, metrics, metrics_interval=30):
        self.present = present
        self.ready = ready
        self.metrics = metrics
        self.metrics_interval = metrics_interval
        self.events = queue.Queue()
        self.thread = threading.Thread(target=self._run, name="scan-feedback", daemon=True)

    def start(self):
        self.thread.start()

    def put(self, event):
        self.events.put(dict(event, feedback_queued_at=time.monotonic()))

    def stop(self):
        self.events.put(None)
        self.thread.join(timeout=2)
        return not self.thread.is_alive()

    def _run(self):
        ready_at = 0
        report_at = time.monotonic() + self.metrics_interval
        while True:
            try:
                event = self.events.get(timeout=0.05)
            except queue.Empty:
                event = {}
            if event is None:
                return
            now = time.monotonic()
            if event:
                self.metrics.observe("feedback_queue_wait", now - event["feedback_queued_at"])
                try:
                    hold = self.present(event)
                except Exception as error:
                    print(f"Feedback error: {type(error).__name__}")
                    hold = 0
                if hold is not None:
                    ready_at = time.monotonic() + hold
            elif ready_at and now >= ready_at:
                ready_at = 0
                try:
                    self.ready()
                except Exception as error:
                    print(f"Ready indicator error: {type(error).__name__}")
            if self.metrics_interval and now >= report_at:
                print("METRICS:", json.dumps(self.metrics.summary(), sort_keys=True))
                report_at = now + self.metrics_interval
