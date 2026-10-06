import unittest

from scanner_config import ScannerSettings


class ScannerSettingsTests(unittest.TestCase):
    def test_defaults_preserve_resolution_rate_and_focus(self):
        settings = ScannerSettings.from_env({})
        self.assertEqual((settings.width, settings.height, settings.fps), (640, 480, 30))
        self.assertEqual(settings.lens_position, 20)
        self.assertFalse(settings.camera_queue)

    def test_reads_tuning_options_without_rounding(self):
        settings = ScannerSettings.from_env({
            "SCANNER_FPS": "59.9", "SCANNER_LENS_POSITION": "3.5",
            "SCANNER_EXPOSURE_US": "2000", "SCANNER_GAIN": "1.5",
            "SCANNER_CAMERA_QUEUE": "true", "SCANNER_DECODER": "zxingcpp",
        })
        self.assertEqual(settings.fps, 59.9)
        self.assertEqual(settings.lens_position, 3.5)
        self.assertEqual(settings.gain, 1.5)
        self.assertTrue(settings.camera_queue)

    def test_rejects_unsafe_or_invalid_settings(self):
        for key, value in {
            "FPS": "nan", "GAIN": "inf", "WIDTH": "641",
            "HEIGHT": "0", "BUFFER_COUNT": "1", "CAMERA_QUEUE": "maybe",
            "EXPOSURE_US": "1000000", "CONNECT_TIMEOUT": "0",
            "PERSISTENCE_QUEUE": "0", "API_WORKERS": "-1",
            "LENS_POSITION": "-1", "SENSOR_MODE": "-2",
            "COPY_MODE": "none", "DECODER": "unknown",
            "CROP_SIZE": "-1", "FULL_FRAME_INTERVAL": "0",
        }.items():
            with self.subTest(key=key), self.assertRaises(ValueError):
                ScannerSettings.from_env({"SCANNER_" + key: value})
