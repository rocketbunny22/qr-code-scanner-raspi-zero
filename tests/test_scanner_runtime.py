"""Exercise the actual scanner loop with deterministic device/worker boundaries."""

from contextlib import ExitStack
import importlib.util
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scanner_config import ScannerSettings


BADGE = b"https://example.invalid/?company_id=company&attendee=attendee"


class ScannerRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("builtins.print"))
        self.load_dotenv = Mock()
        self.stack.enter_context(patch.dict("sys.modules", {
            "dotenv": SimpleNamespace(load_dotenv=self.load_dotenv),
            "picamera2": SimpleNamespace(Picamera2=Mock()),
            "gpiozero": None,
            "rpi_ws281x": None,
            "requests": SimpleNamespace(Session=Mock(), RequestException=Exception),
        }))
        path = Path(__file__).resolve().parents[1] / "qr_code_scanner.py"
        spec = importlib.util.spec_from_file_location("scanner_runtime_under_test", path)
        self.runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.runtime)
        # The module starts a sound worker at import; always stop and join it,
        # including tests that fail before main() can perform its cleanup.
        self.runtime.sound_stop_event.set()
        self.runtime.sound_queue.put(None)
        self.runtime.sound_thread.join(timeout=1)
        self.assertFalse(self.runtime.sound_thread.is_alive())
        self.runtime.API_URL = "https://example.invalid/checkin"
        self.runtime.API_TOKEN = "test-token"
        self.runtime.shutdown_event = threading.Event()
        self.runtime.STARTUP_FAILURE_RETRY_SECONDS = 0
        self.signal_functions = {
            name: getattr(self.runtime, name) for name in (
                "signal_ready", "signal_processing", "signal_saved",
                "signal_success", "signal_duplicate", "signal_failure",
            )
        }
        self.clock = 100.0
        self.frame_step = 1.0
        self.runtime.time = SimpleNamespace(monotonic=lambda: self.clock)
        self.patch("signal", SimpleNamespace(SIGTERM=15, SIGINT=2, signal=Mock()))
        self.patch("ScannerSettings", SimpleNamespace(from_env=lambda: ScannerSettings()))
        self.camera = Mock()
        self.patch("Picamera2", Mock(return_value=self.camera))
        self.patch("configure_camera", Mock())
        self.outbox = Mock()
        self.outbox.count.return_value = 0
        self.patch("ScanOutbox", Mock(return_value=self.outbox))
        self.patch("create_decoder", Mock())
        self.decoder = Mock()
        self.patch("QrDecoder", Mock(return_value=self.decoder))
        self.status = self.patch("show_status", Mock())
        self.sound = self.patch("queue_sound", Mock())
        self.saved_signal = self.patch("signal_saved", Mock())
        self.success_signal = self.patch("signal_success", Mock())
        self.patch("strip_test_marker", Mock())
        self.patch("init_status_strip", Mock())
        self.events = []
        self.submissions = []
        self.pending_events = []
        self.submit_outcomes = []
        self.event_callback = None
        self.feedback = Mock()

        def make_feedback(present, *args):
            self.present_event = present
            def present_recorded(event):
                self.events.append(event)
                return present(event)
            self.feedback.put.side_effect = present_recorded
            return self.feedback

        self.patch("FeedbackWorker", Mock(side_effect=make_feedback))
        self.pipeline = Mock()
        self.pipeline.stop.return_value = True

        def make_pipeline(*args, on_event, **kwargs):
            self.event_callback = on_event
            return self.pipeline

        def submit(scan_id, raw_payload, detected_at):
            self.submissions.append(raw_payload)
            outcome = self.submit_outcomes.pop(0) if self.submit_outcomes else "accepted"
            if outcome == "busy":
                return False
            event = {"kind": "accepted" if outcome == "saved_only" else outcome, "scan_id": scan_id,
                     "raw_payload": raw_payload, "elapsed": 0.001,
                     "detected_at": detected_at}
            if outcome == "error":
                event.update(stage="persistence", error="OSError")
            if outcome != "pending":
                self.pending_events.append(event)
            if outcome == "accepted":
                self.pending_events.append(dict(event, kind="result",
                                                result={"status": "checked_in"}))
            return True

        self.pipeline.submit.side_effect = submit
        self.patch("ScanPipeline", Mock(side_effect=make_pipeline))
        self.capture = Mock()
        self.patch("LatestFrameCapture", Mock(return_value=self.capture))

    def patch(self, name, value):
        return self.stack.enter_context(patch.object(self.runtime, name, value))

    def run_frames(self, payload_sets):
        frames = iter([None] + list(payload_sets))

        def get_frame(timeout=5):
            self.clock += self.frame_step
            while self.pending_events:
                self.event_callback(self.pending_events.pop(0))
            try:
                payloads = next(frames)
            except StopIteration:
                self.runtime.shutdown_event.set()
                payloads = []
            self.decoder.decode.return_value = payloads
            return SimpleNamespace(gray=object(), metadata={}, sensor_timestamp=None,
                                   captured_at=self.clock)

        self.capture.get_frame.side_effect = get_frame
        return self.runtime.main()

    def test_valid_scan_waits_for_acceptance_then_presents_result(self):
        self.assertEqual(self.run_frames([[BADGE], [], [BADGE]]), 0)
        self.assertEqual(self.submissions, [BADGE])
        self.assertEqual([event["kind"] for event in self.events],
                         ["accepted", "result", "duplicate"])
        self.status.assert_any_call("SAVED", "Scan next badge")
        self.status.assert_any_call("CHECKED IN", "")
        self.pipeline.start.assert_called_once()
        self.pipeline.stop.assert_called_once_with(timeout=15)
        self.outbox.close.assert_called_once()
        self.camera.stop.assert_called_once()
        self.capture.stop.assert_called_once()

    def test_saved_badge_beeps_without_any_api_result(self):
        self.submit_outcomes = ["saved_only"]
        self.assertEqual(self.run_frames([[BADGE], []]), 0)
        self.assertEqual([event["kind"] for event in self.events], ["accepted"])
        self.sound.assert_any_call("saved")
        self.saved_signal.assert_called_once()
        self.success_signal.assert_not_called()
        self.status.assert_any_call("SAVED", "Scan next badge")

    def test_api_success_does_not_play_a_second_acknowledgement(self):
        self.assertEqual(self.run_frames([[BADGE], []]), 0)
        self.assertEqual([call.args[0] for call in self.sound.call_args_list],
                         ["startup", "saved"])
        self.success_signal.assert_called_once()

    def test_older_success_cannot_overwrite_newer_saved_badge(self):
        self.assertEqual(self.run_frames([]), 0)
        for scan_id in (1, 2):
            self.present_event({"kind": "accepted", "scan_id": scan_id,
                                "raw_payload": BADGE, "detected_at": self.clock,
                                "elapsed": 0.01})
        self.present_event({"kind": "result", "scan_id": 1, "raw_payload": BADGE,
                            "result": {"status": "checked_in"}})
        self.success_signal.assert_not_called()
        self.present_event({"kind": "result", "scan_id": 2, "raw_payload": BADGE,
                            "result": {"status": "checked_in"}})
        self.success_signal.assert_called_once()

    def test_late_api_rejection_is_still_reported(self):
        self.assertEqual(self.run_frames([]), 0)
        self.present_event({"kind": "accepted", "scan_id": 2, "raw_payload": BADGE,
                            "detected_at": self.clock, "elapsed": 0.01})
        self.present_event({"kind": "result", "scan_id": 1, "raw_payload": BADGE,
                            "result": {"status": "not_found"}})
        self.sound.assert_called_with("failure")
        self.status.assert_any_call("NOT FOUND", "See kiosk")

    def test_failed_disk_write_does_not_acknowledge_capture(self):
        self.submit_outcomes = ["error"]
        self.assertEqual(self.run_frames([[BADGE], []]), 0)
        self.saved_signal.assert_not_called()
        self.assertNotIn("saved", [call.args[0] for call in self.sound.call_args_list])

    def test_busy_submission_remains_eligible_for_retry(self):
        self.submit_outcomes = ["busy", "accepted"]
        self.assertEqual(self.run_frames([[BADGE], [BADGE], [], [BADGE]]), 0)
        self.assertEqual(self.submissions, [BADGE, BADGE])
        self.assertEqual(sum(event["kind"] == "accepted" for event in self.events), 1)
        self.status.assert_any_call("BUSY", "Try badge again")

    def test_busy_retry_is_throttled_while_badge_remains_visible(self):
        self.frame_step = 0.1
        self.submit_outcomes = ["busy", "accepted"]
        self.assertEqual(self.run_frames([[BADGE]] * 10), 0)
        self.assertEqual(self.submissions, [BADGE, BADGE])
        self.assertEqual(sum(event.get("result", {}).get("status") == "busy"
                             for event in self.events), 1)

    def test_background_success_does_not_signal_a_current_checkin(self):
        self.pipeline.start.side_effect = lambda: self.event_callback({
            "kind": "result", "scan_id": None, "raw_payload": BADGE,
            "result": {"status": "checked_in"}, "elapsed": 0.1,
        })
        self.assertEqual(self.run_frames([[]]), 0)
        self.assertFalse(any(call.args[0] == "CHECKED IN" for call in self.status.call_args_list))

    def test_persistence_failure_releases_pending_payload_for_retry(self):
        self.submit_outcomes = ["error", "accepted"]
        self.assertEqual(self.run_frames([[BADGE], [], [BADGE], [], [BADGE]]), 0)
        self.assertEqual(self.submissions, [BADGE, BADGE])
        self.assertEqual(sum(event["kind"] == "accepted" for event in self.events), 1)
        self.assertEqual(self.events[0]["kind"], "error")

    def test_pending_payload_is_not_submitted_twice(self):
        self.submit_outcomes = ["pending"]
        self.assertEqual(self.run_frames([[BADGE], [BADGE], [BADGE]]), 0)
        self.assertEqual(self.submissions, [BADGE])
        self.assertEqual(self.events, [])

    def test_missing_api_configuration_fails_before_camera_start(self):
        self.runtime.API_TOKEN = None
        self.assertEqual(self.runtime.main(), 1)
        self.camera.start.assert_not_called()
        self.pipeline.start.assert_not_called()
        self.status.assert_any_call("STARTUP FAIL", "Missing API config")

    def test_camera_failure_stops_camera_and_skips_pipeline(self):
        self.runtime.configure_camera.side_effect = RuntimeError("camera unavailable")
        self.assertEqual(self.runtime.main(), 1)
        self.camera.stop.assert_called_once()
        self.pipeline.start.assert_not_called()
        self.status.assert_any_call("STARTUP FAIL", "Camera error")

    def test_pipeline_shutdown_timeout_keeps_database_open(self):
        self.pipeline.stop.return_value = False
        self.assertEqual(self.run_frames([]), 1)
        self.outbox.close.assert_not_called()

    def test_strip_stays_off_for_non_capture_states(self):
        strip = Mock()
        strip.numPixels.return_value = 3
        with patch.multiple(self.runtime, **self.signal_functions), patch.multiple(
            self.runtime, USE_LIGHTS=False, USE_STRIP=True, status_strip=strip,
            Color=lambda red, green, blue: (red, green, blue),
        ):
            for name in ("signal_ready", "signal_processing", "signal_failure"):
                action = self.signal_functions[name]
                with self.subTest(state=name):
                    strip.reset_mock()
                    action()
                    self.assertEqual([call.args for call in strip.setPixelColor.call_args_list],
                                     [(index, (0, 0, 0)) for index in range(3)])
                    strip.show.assert_called_once()

    def test_capture_flash_returns_to_off_without_waiting_for_api(self):
        strip = Mock()
        strip.numPixels.return_value = 1
        with patch.multiple(self.runtime, **self.signal_functions), patch.multiple(
            self.runtime, USE_LIGHTS=False, USE_STRIP=True, status_strip=strip,
            Color=lambda *rgb: rgb,
        ), patch.object(self.runtime.threading, "Timer") as timer:
            for name in ("signal_saved", "signal_duplicate"):
                with self.subTest(state=name):
                    self.signal_functions[name]()
                    strip.setPixelColor.assert_called_with(0, (0, 255, 0))
                    timer.return_value.start.assert_called()
                    self.assertEqual(timer.call_args.args[0], 0.4)
                    strip.reset_mock()
                    self.runtime.signal_success()
                    strip.show.assert_not_called()
                    timer.call_args.args[1]()
                    strip.setPixelColor.assert_called_with(0, (0, 0, 0))

    def test_old_flash_timer_cannot_override_new_flash_or_shutdown(self):
        strip = Mock()
        strip.numPixels.return_value = 1
        with patch.multiple(self.runtime, USE_LIGHTS=False, USE_STRIP=True,
                            status_strip=strip, Color=lambda *rgb: rgb), \
                patch.object(self.runtime.threading, "Timer") as timer:
            self.runtime.strip_flash_green()
            old_callback = timer.call_args.args[1]
            self.runtime.strip_flash_green()
            new_callback = timer.call_args.args[1]
            strip.reset_mock()
            old_callback()
            strip.show.assert_not_called()
            self.runtime.lights_off()
            strip.setPixelColor.assert_called_with(0, (0, 0, 0))
            strip.reset_mock()
            new_callback()
            strip.show.assert_not_called()

    def test_shutdown_turns_illumination_off(self):
        strip = Mock()
        strip.numPixels.return_value = 3
        with patch.multiple(self.runtime, USE_LIGHTS=False, USE_STRIP=True,
                            status_strip=strip, Color=lambda *rgb: rgb):
            self.runtime.lights_off()
        self.assertEqual([call.args for call in strip.setPixelColor.call_args_list],
                         [(index, (0, 0, 0)) for index in range(3)])
        strip.show.assert_called_once()


if __name__ == "__main__":
    unittest.main()
