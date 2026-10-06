"""Exercise durable delivery without importing Raspberry Pi dependencies."""

import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from scanner_core import ScanOutbox
from scanner_pipeline import ScanPipeline


class Session:
    def close(self):
        pass


class ScanPipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.outbox = ScanOutbox(self.directory.name + "/outbox.db")
        self.events = queue.Queue()
        self.pipeline = None
        self.gates = []

    def tearDown(self):
        for gate in self.gates:
            gate.set()
        if self.pipeline is not None:
            self.assertTrue(self.pipeline.stop(timeout=3))
        self.outbox.close()
        self.directory.cleanup()

    def start(self, send=None, **options):
        self.pipeline = ScanPipeline(
            self.outbox, Session,
            send or (lambda payload, session: {"status": "success"}),
            on_event=self.events.put, **options)
        self.pipeline.start()

    def event(self, kind):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            event = self.events.get(timeout=max(0.01, deadline - time.monotonic()))
            if event["kind"] == kind:
                return event
        self.fail(f"No {kind} event")

    def gate(self):
        gate = threading.Event()
        self.gates.append(gate)
        return gate

    def test_acceptance_follows_durable_write_and_result_removes_entry(self):
        gate = self.gate()
        self.start(send=lambda payload, session: (gate.wait(3), {"status": "success"})[1])
        self.assertTrue(self.pipeline.submit(1, b"badge", time.monotonic()))
        accepted = self.event("accepted")
        self.assertEqual(accepted["scan_id"], 1)
        self.assertEqual(self.outbox.count(), 1)
        gate.set()
        self.assertEqual(self.event("result")["result"]["status"], "success")
        self.assertEqual(self.outbox.count(), 0)

    def test_bounded_queue_and_duplicate_reservation_do_not_block_submit(self):
        gate = self.gate()
        entered = threading.Event()
        enqueue = self.outbox.enqueue

        def blocked_enqueue(*args):
            entered.set()
            gate.wait(3)
            return enqueue(*args)

        with patch.object(self.outbox, "enqueue", side_effect=blocked_enqueue):
            self.start(queue_size=1)
            self.assertTrue(self.pipeline.submit(1, b"first", time.monotonic()))
            self.assertTrue(entered.wait(3))
            self.assertFalse(self.pipeline.submit(2, b"first", time.monotonic()))
            self.assertTrue(self.pipeline.submit(3, b"second", time.monotonic()))
            self.assertFalse(self.pipeline.submit(4, b"third", time.monotonic()))
            gate.set()
            self.event("accepted")
            self.event("accepted")

    def test_persistence_failure_allows_resubmit_without_acceptance(self):
        self.start()
        with patch.object(self.outbox, "enqueue", side_effect=OSError("disk full")):
            self.assertTrue(self.pipeline.submit(1, b"badge", time.monotonic()))
            self.assertEqual(self.event("error")["stage"], "persistence")
            self.pipeline._queue.join()
        self.assertTrue(self.pipeline.submit(2, b"badge", time.monotonic()))
        self.assertEqual(self.event("accepted")["scan_id"], 2)

    def test_expired_lease_never_sends_same_payload_concurrently(self):
        gate = self.gate()
        started = threading.Event()
        second = threading.Event()
        calls = []

        def send(payload, session):
            calls.append(payload)
            if payload == "first":
                started.set()
                gate.wait(3)
            else:
                second.set()
            return {"status": "success"}

        self.start(send=send, worker_count=3, lease_seconds=0.02)
        self.pipeline.submit(1, b"first", time.monotonic())
        self.assertTrue(started.wait(3))
        # A real expired lease exercises reclamation while a request is active.
        time.sleep(0.15)
        self.pipeline.submit(2, b"second", time.monotonic())
        self.assertTrue(second.wait(3))
        self.assertEqual(calls.count("first"), 1)
        gate.set()

    def test_restart_recovers_leased_scan_and_retries_transient_result(self):
        self.outbox.enqueue(b"recovered", time.time(), 3600)
        calls = []

        def send(payload, session):
            calls.append(payload)
            return {"status": "offline" if len(calls) == 1 else "success"}

        self.start(send=send, retry_base=0.01, retry_max=0.02)
        retry = self.event("result")
        self.assertIsNone(retry["scan_id"])
        self.assertEqual(retry["retry_delay"], 0.01)
        self.assertEqual(self.event("result")["result"]["status"], "success")
        self.assertEqual(calls, ["recovered", "recovered"])
        self.assertEqual(self.outbox.count(), 0)

    def test_reused_database_id_is_delivered_while_previous_feedback_blocks(self):
        callback_entered = threading.Event()
        gate = self.gate()
        second_sent = threading.Event()

        def callback(event):
            self.events.put(event)
            if event["kind"] == "result" and event["raw_payload"] == b"first":
                callback_entered.set()
                gate.wait(3)

        def send(payload, session):
            if payload == "second":
                second_sent.set()
            return {"status": "success"}

        self.pipeline = ScanPipeline(self.outbox, Session, send, worker_count=2,
                                     on_event=callback, lease_seconds=60)
        self.pipeline.start()
        self.pipeline.submit(1, b"first", time.monotonic())
        self.assertTrue(callback_entered.wait(3))
        self.assertEqual(self.outbox.count(), 0)
        # The now-empty SQLite table reuses the deleted primary key.
        self.pipeline.submit(2, b"second", time.monotonic())
        self.assertTrue(second_sent.wait(2), "Reused row ID must not wait for an old lease")
        gate.set()

    def test_insertion_between_empty_claim_and_wait_keeps_wakeup(self):
        empty_claim = threading.Event()
        gate = self.gate()
        signaled_when_waiting = []
        self.pipeline = ScanPipeline(self.outbox, Session,
                                     lambda payload, session: {"status": "success"},
                                     worker_count=1, on_event=self.events.put)
        claim = self.pipeline._claim
        wake = self.pipeline._wake_events[0]
        wait = wake.wait

        def paused_claim():
            entry = claim()
            if entry is None and not empty_claim.is_set():
                empty_claim.set()
                gate.wait(3)
            return entry

        def observe_wait(timeout):
            signaled_when_waiting.append(wake.is_set())
            return wait(timeout)

        with patch.object(self.pipeline, "_claim", side_effect=paused_claim), \
                patch.object(wake, "wait", side_effect=observe_wait):
            self.pipeline.start()
            self.assertTrue(empty_claim.wait(3))
            self.pipeline.submit(1, b"badge", time.monotonic())
            self.event("accepted")
            self.pipeline._queue.join()
            gate.set()
            self.event("result")
            self.assertTrue(signaled_when_waiting[0])

    def test_acknowledgement_failure_keeps_scan_durable(self):
        self.start()
        with patch.object(self.outbox, "acknowledge", side_effect=OSError("disk full")):
            self.pipeline.submit(1, b"badge", time.monotonic())
            self.assertEqual(self.event("error")["stage"], "delivery_persistence")
            self.assertEqual(self.outbox.count(), 1)

    def test_worker_reuses_session_and_closes_it_on_shutdown(self):
        sessions = []
        delivered_sessions = []

        class TrackedSession:
            closed = False

            def close(self):
                self.closed = True

        def factory():
            session = TrackedSession()
            sessions.append(session)
            return session

        def send(payload, session):
            delivered_sessions.append(session)
            return {"status": "success"}

        self.pipeline = ScanPipeline(self.outbox, factory, send, worker_count=1,
                                     on_event=self.events.put)
        self.pipeline.start()
        for number in range(2):
            self.pipeline.submit(number, str(number).encode(), time.monotonic())
            self.event("result")
        self.assertTrue(self.pipeline.stop())
        self.assertEqual(len(sessions), 1)
        self.assertEqual(delivered_sessions, [sessions[0], sessions[0]])
        self.assertTrue(sessions[0].closed)

    def test_shutdown_drains_ram_and_reports_active_request(self):
        gate = self.gate()
        started = threading.Event()

        def send(payload, session):
            started.set()
            gate.wait(3)
            return {"status": "offline"}

        self.start(send=send, worker_count=1)
        self.pipeline.submit(1, b"first", time.monotonic())
        self.assertTrue(started.wait(3))
        self.pipeline.submit(2, b"second", time.monotonic())
        self.assertFalse(self.pipeline.stop(timeout=0.2))
        self.assertEqual(self.outbox.count(), 2)
        self.assertFalse(self.pipeline.submit(3, b"third", time.monotonic()))
        gate.set()
        self.assertTrue(self.pipeline.stop(timeout=3))


if __name__ == "__main__":
    unittest.main()
