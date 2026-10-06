"""Bounded asynchronous persistence and delivery, independent of Pi hardware."""

from collections import OrderedDict
import logging
import queue
import threading
import time

from scanner_core import is_retryable_result


class ScanPipeline:
    """Persist before acceptance; lease every delivery through the durable outbox.

    ``on_event`` runs on worker threads and must be thread-safe. ``detected_at``
    is a monotonic timestamp. A successful submit reserves RAM, not durability;
    only an ``accepted`` event confirms the scan has reached the database.
    """

    def __init__(self, outbox, session_factory, send, worker_count=2,
                 queue_size=32, retry_base=5, retry_max=300, lease_seconds=60,
                 on_event=None, metrics=None):
        if worker_count < 1 or queue_size < 1:
            raise ValueError("worker_count and queue_size must be positive")
        if retry_base <= 0 or retry_max < retry_base or lease_seconds <= 0:
            raise ValueError("invalid retry or lease configuration")
        self.outbox = outbox
        self.session_factory = session_factory
        self.send = send
        self.worker_count = worker_count
        self.retry_base = retry_base
        self.retry_max = retry_max
        self.lease_seconds = lease_seconds
        self.on_event = on_event or (lambda event: None)
        self.metrics = metrics
        self._queue = queue.Queue(maxsize=queue_size)
        self._lock = threading.Lock()
        self._submit_lock = threading.Lock()
        self._pending = set()
        self._inflight = set()
        self._metadata = OrderedDict()
        self._metadata_limit = queue_size + worker_count
        self._accepting = False
        self._closing = threading.Event()
        self._api_stop = threading.Event()
        self._wake_events = [threading.Event() for _ in range(worker_count)]
        self._threads = []
        self._persistence = None

    def _emit(self, **event):
        # A feedback failure must never turn a definitive delivery into a retry.
        try:
            self.on_event(event)
        except Exception:
            logging.getLogger(__name__).exception("Scan feedback handler failed")

    def _measure(self, name, elapsed):
        if self.metrics is not None:
            try:
                self.metrics.observe(name, elapsed)
            except Exception:
                logging.getLogger(__name__).exception("Scan metric recorder failed")

    def start(self):
        if self._persistence is not None:
            raise RuntimeError("pipeline already started")
        self.outbox.release_all(time.time())
        self._accepting = True
        self._persistence = threading.Thread(target=self._persist, name="scan-persistence", daemon=True)
        self._persistence.start()
        for number in range(self.worker_count):
            thread = threading.Thread(target=self._deliver, args=(self._wake_events[number],),
                                      name=f"scan-api-{number + 1}", daemon=True)
            self._threads.append(thread)
            thread.start()

    def submit(self, scan_id, raw_payload, detected_at):
        """Reserve a bounded queue slot immediately, returning False when busy."""
        # Do not acquire the database ownership lock on the decoding thread.
        with self._submit_lock:
            if not self._accepting or raw_payload in self._pending:
                return False
            try:
                self._queue.put_nowait((scan_id, raw_payload, detected_at))
            except queue.Full:
                return False
            self._pending.add(raw_payload)
            return True

    def _persist(self):
        while not self._closing.is_set() or not self._queue.empty():
            try:
                scan_id, payload, detected_at = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            started = time.monotonic()
            self._measure("persist_queue_wait", started - detected_at)
            metadata = dict(scan_id=scan_id, raw_payload=payload, detected_at=detected_at)
            try:
                with self._lock:
                    entry_id = self.outbox.enqueue(payload, time.time(), 0)
                    metadata["saved_at"] = time.monotonic()
                    self._metadata[entry_id] = metadata
                    self._metadata.move_to_end(entry_id)
                    while len(self._metadata) > self._metadata_limit:
                        self._metadata.popitem(last=False)
                    elapsed = time.monotonic() - detected_at
                    self._measure("persistence", time.monotonic() - started)
                    self._measure("decode_to_saved", elapsed)
                    self._emit(kind="accepted", **metadata, elapsed=elapsed)
                for wake in self._wake_events:
                    wake.set()
            except Exception as error:
                self._emit(kind="error", **metadata, stage="persistence", error=type(error).__name__)
            finally:
                with self._submit_lock:
                    self._pending.discard(payload)
                self._queue.task_done()

    def _claim(self):
        with self._lock:
            # claim_due also renews an expired lease. Skip an entry already sent
            # by this process, then inspect the next due entry without starving it.
            while True:
                entry = self.outbox.claim_due(time.time(), self.lease_seconds)
                if entry is None:
                    return None
                entry_id, payload = entry
                if entry_id in self._inflight:
                    continue
                self._inflight.add(entry_id)
                metadata = self._metadata.pop(entry_id, dict(
                    scan_id=None, raw_payload=payload, detected_at=None))
                return entry_id, payload, metadata

    def _deliver(self, wake):
        session = None
        try:
            while not self._api_stop.is_set():
                # Clear before checking the DB: inserts after an empty claim
                # remain signaled until this worker waits. Each worker owns its
                # event, so another waiter cannot consume its notification.
                wake.clear()
                try:
                    if session is None:
                        session = self.session_factory()
                    entry = self._claim()
                except Exception as error:
                    self._emit(kind="error", stage="delivery", error=type(error).__name__, scan_id=None)
                    self._api_stop.wait(1)
                    continue
                if entry is None:
                    wake.wait(0.1)
                    continue
                entry_id, payload, metadata = entry
                started = time.monotonic()
                if metadata["detected_at"] is not None:
                    self._measure("api_queue_wait", started - metadata["saved_at"])
                try:
                    result = self.send(payload.decode("utf-8", errors="replace"), session)
                    if not isinstance(result, dict):
                        raise ValueError("send returned a non-object result")
                except Exception:
                    result = {"success": False, "status": "error", "message": "Check-in request failed"}
                elapsed = time.monotonic() - started
                self._measure("api", elapsed)
                retry_delay = None
                try:
                    with self._lock:
                        try:
                            if is_retryable_result(result):
                                retry_delay = self.outbox.schedule_retry(
                                    entry_id, time.time(), self.retry_base, self.retry_max)
                            else:
                                self.outbox.acknowledge(entry_id)
                        finally:
                            # SQLite can reuse a deleted row ID immediately.
                            # Release ownership before enqueue/claim can see it.
                            self._inflight.discard(entry_id)
                    if metadata["detected_at"] is not None:
                        self._measure("decode_to_result", time.monotonic() - metadata["detected_at"])
                    self._emit(kind="result", **metadata, result=result,
                               elapsed=elapsed, retry_delay=retry_delay)
                except Exception as error:
                    self._emit(kind="error", **metadata, stage="delivery_persistence", error=type(error).__name__)
        finally:
            if session is not None:
                session.close()

    def stop(self, timeout=15):
        """Drain RAM to disk and stop delivery; False means do not close the DB."""
        deadline = time.monotonic() + timeout
        with self._submit_lock:
            self._accepting = False
            self._closing.set()
        if self._persistence is not None:
            self._persistence.join(max(0, deadline - time.monotonic()))
        self._api_stop.set()
        for wake in self._wake_events:
            wake.set()
        for thread in self._threads:
            thread.join(max(0, deadline - time.monotonic()))
        return (self._persistence is None or not self._persistence.is_alive()) and all(
            not thread.is_alive() for thread in self._threads)
