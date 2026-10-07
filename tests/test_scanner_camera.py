import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

try:
    import numpy as np
except ImportError:
    np = None

from scanner_camera import LatestFrameCapture, configure_camera
from scanner_config import ScannerSettings


class ConfigureCameraTests(unittest.TestCase):
    def setUp(self):
        self.controls = SimpleNamespace(
            AfModeEnum=SimpleNamespace(Manual=0, Continuous=2),
            AfRangeEnum=SimpleNamespace(Normal=0, Macro=1, Full=2),
            AfSpeedEnum=SimpleNamespace(Normal=0, Fast=1),
        )
        fake_libcamera = SimpleNamespace(
            controls=self.controls,
        )
        self.modules = patch.dict("sys.modules", {"libcamera": fake_libcamera})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.camera = Mock()
        self.camera.camera_controls = {
            "LensPosition": (0.0, 32.0, 1.0),
            "ExposureTime": (100, 100000, 10000),
            "AnalogueGain": (1.0, 16.0, 1.0),
        }
        self.camera.camera_configuration.return_value = {
            "main": {"size": (640, 480)}, "sensor": {"bit_depth": 10},
        }
        self.camera.sensor_modes = [
            {"size": (1536, 864), "bit_depth": 10, "fps": 120.0},
        ]

    @patch("builtins.print")
    def test_configures_defaults_without_starting_camera(self, output):
        result = configure_camera(self.camera, ScannerSettings())
        self.camera.create_video_configuration.assert_called_once_with(
            main={"format": "YUV420", "size": (640, 480)},
            controls={"FrameRate": 30.0, "AfMode": 0, "LensPosition": 20.0},
            queue=False, buffer_count=4,
        )
        self.camera.configure.assert_called_once_with(
            self.camera.create_video_configuration.return_value,
        )
        self.camera.start.assert_not_called()
        self.assertEqual(result, self.camera.camera_configuration.return_value)
        self.assertGreaterEqual(output.call_count, 2)
        output.assert_any_call("Camera focus settings: {'mode': 'manual', 'lens_position': 20.0}")

    @patch("builtins.print")
    def test_continuous_autofocus_ranges_and_speeds_without_manual_position(self, output):
        for af_range, range_value in (("normal", 0), ("macro", 1), ("full", 2)):
            for af_speed, speed_value in (("normal", 0), ("fast", 1)):
                with self.subTest(af_range=af_range, af_speed=af_speed):
                    configure_camera(self.camera, ScannerSettings(
                        focus_mode="continuous", af_range=af_range, af_speed=af_speed,
                        lens_position=100,
                    ))
                    requested = self.camera.create_video_configuration.call_args.kwargs["controls"]
                    self.assertEqual(requested, {
                        "FrameRate": 30.0, "AfMode": 2,
                        "AfRange": range_value, "AfSpeed": speed_value,
                    })
                    output.assert_any_call(
                        f"Camera focus settings: {{'mode': 'continuous', "
                        f"'range': '{af_range}', 'speed': '{af_speed}'}}"
                    )
        self.camera.autofocus_cycle.assert_not_called()
        self.camera.start.assert_not_called()

    @patch("builtins.print")
    def test_manual_mode_does_not_require_continuous_autofocus_enums(self, output):
        del self.controls.AfRangeEnum
        del self.controls.AfSpeedEnum
        del self.controls.AfModeEnum.Continuous
        configure_camera(self.camera, ScannerSettings())

    def test_continuous_mode_fails_when_autofocus_enum_is_unavailable(self):
        del self.controls.AfSpeedEnum
        with self.assertRaises(AttributeError):
            configure_camera(self.camera, ScannerSettings(focus_mode="continuous"))
        self.camera.configure.assert_not_called()

    @patch("builtins.print")
    def test_explicit_sensor_and_manual_exposure(self, output):
        configure_camera(self.camera, ScannerSettings(
            sensor_mode=0, fps=60, exposure_us=5000, gain=2,
            buffer_count=6, camera_queue=True,
        ))
        options = self.camera.create_video_configuration.call_args.kwargs
        self.assertEqual(options["sensor"], {"output_size": (1536, 864), "bit_depth": 10})
        self.assertFalse(options["controls"]["AeEnable"])
        self.assertEqual(options["controls"]["ExposureTime"], 5000)
        self.assertEqual(options["controls"]["AnalogueGain"], 2)
        self.assertEqual(options["buffer_count"], 6)
        self.assertTrue(options["queue"])

    def test_rejects_missing_sensor_mode(self):
        with self.assertRaisesRegex(ValueError, "unavailable"):
            configure_camera(self.camera, ScannerSettings(sensor_mode=1))
        self.camera.configure.assert_not_called()

    def test_rejects_fps_above_sensor_limit(self):
        with self.assertRaisesRegex(ValueError, "exceeds sensor mode"):
            configure_camera(self.camera, ScannerSettings(sensor_mode=0, fps=121))
        self.camera.configure.assert_not_called()

    @patch("builtins.print")
    def test_rejects_controls_outside_advertised_ranges(self, output):
        for settings, name in (
            (ScannerSettings(lens_position=33), "LensPosition"),
            (ScannerSettings(exposure_us=50), "ExposureTime"),
            (ScannerSettings(exposure_us=5000, gain=17), "AnalogueGain"),
        ):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, name):
                    configure_camera(self.camera, settings)

    @patch("builtins.print")
    def test_does_not_invent_limits_when_ranges_are_absent(self, output):
        self.camera.camera_controls = {}
        configure_camera(self.camera, ScannerSettings())


class FakeRequest:
    def __init__(self, timestamp=1_000_000_000):
        self.array = np.arange(36, dtype=np.uint8).reshape(6, 6)
        self.timestamp = timestamp
        self.released = False

    def get_metadata(self):
        return {"SensorTimestamp": self.timestamp}

    def make_array(self, name):
        return self.array.copy()

    def release(self):
        self.released = True
        self.array.fill(0)


class FakeMapping:
    def __init__(self, request, name):
        self.array = request.array

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeCamera:
    def __init__(self):
        self.requests = queue.Queue()

    def capture_request(self):
        request = self.requests.get(timeout=1)
        if isinstance(request, Exception):
            raise request
        return request


@unittest.skipIf(np is None, "Camera buffer tests require NumPy")
class LatestFrameCaptureTests(unittest.TestCase):
    def setUp(self):
        self.camera = FakeCamera()
        self.capture = LatestFrameCapture(
            self.camera, 4, 4, mapped_array_factory=FakeMapping,
        )

    def tearDown(self):
        self.capture.stop()
        self.camera.requests.put(RuntimeError("stopped"))
        self.capture.wait()

    def test_copies_only_luma_with_independent_contiguous_ownership(self):
        for mode in ("luma", "array"):
            with self.subTest(mode=mode):
                self.capture.copy_mode = mode
                request = FakeRequest()
                expected = request.array[:4, :4].copy()
                self.camera.requests.put(request)
                frame = self.capture._capture()
                self.assertTrue(request.released)
                self.assertEqual(frame.gray.shape, (4, 4))
                if mode == "luma":
                    self.assertTrue(frame.gray.flags.c_contiguous)
                    self.assertTrue(frame.gray.flags.owndata)
                np.testing.assert_array_equal(frame.gray, expected)
                self.assertEqual(frame.sensor_timestamp, request.timestamp)
                self.assertGreater(frame.captured_at, 0)

    def test_releases_request_when_mapping_fails(self):
        def broken_mapping(request, name):
            raise ValueError("copy failed")

        self.capture.mapped_array_factory = broken_mapping
        request = FakeRequest()
        self.camera.requests.put(request)
        with self.assertRaisesRegex(ValueError, "copy failed"):
            self.capture._capture()
        self.assertTrue(request.released)

    def test_releases_request_when_copy_fails(self):
        class BrokenArray:
            def __getitem__(self, key):
                return self

            def copy(self, order):
                raise MemoryError("copy failed")

        class BrokenMapping(FakeMapping):
            def __init__(self, request, name):
                self.array = BrokenArray()

        self.capture.mapped_array_factory = BrokenMapping
        request = FakeRequest()
        self.camera.requests.put(request)
        with self.assertRaisesRegex(MemoryError, "copy failed"):
            self.capture._capture()
        self.assertTrue(request.released)

    def test_keeps_only_latest_frame(self):
        published = threading.Event()
        original_replace = self.capture._replace

        def replace_and_notify(item):
            original_replace(item)
            if item[0] == "frame" and item[1].sensor_timestamp == 2_000_000_000:
                published.set()

        self.capture._replace = replace_and_notify
        self.camera.requests.put(FakeRequest())
        self.camera.requests.put(FakeRequest(2_000_000_000))
        self.capture.start()
        self.assertTrue(published.wait(1))
        self.assertEqual(self.capture.items.qsize(), 1)
        self.assertEqual(self.capture.get_frame().sensor_timestamp, 2_000_000_000)

    def test_propagates_capture_error(self):
        self.camera.requests.put(RuntimeError("camera disconnected"))
        self.capture.start()
        with self.assertRaisesRegex(RuntimeError, "camera disconnected"):
            self.capture.get_frame(timeout=1)

    def test_empty_queue_times_out(self):
        with self.assertRaises(TimeoutError):
            self.capture.get_frame(timeout=0.001)

    def test_stop_discards_queued_frame(self):
        self.capture._replace(("frame", object()))
        self.capture.stop()
        self.assertTrue(self.capture.items.empty())

    def test_records_capture_and_sensor_intervals(self):
        class Metrics:
            def __init__(self):
                self.values = []

            def observe(self, name, value):
                self.values.append((name, value))

        metrics = Metrics()
        self.capture.metrics = metrics
        for timestamp in (1_000_000_000, 1_033_000_000):
            self.camera.requests.put(FakeRequest(timestamp))
            self.capture._capture()
        self.assertIn(("sensor_frame_interval", 0.033), metrics.values)
        self.assertEqual(sum(name == "camera_capture" for name, _ in metrics.values), 2)


if __name__ == "__main__":
    unittest.main()
