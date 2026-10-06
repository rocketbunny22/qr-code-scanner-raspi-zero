from pathlib import Path
import stat
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from capture_benchmark_frames import save_frames

try:
    import numpy as np
except ImportError:
    np = None


@unittest.skipIf(np is None, "NumPy is required for capture tests")
class CaptureBenchmarkFramesTests(unittest.TestCase):
    def test_writes_private_luma_copy_after_releasing_buffer(self):
        source = np.arange(24, dtype=np.uint8).reshape((6, 4))
        released = []
        request = SimpleNamespace(release=lambda: (released.append(True), source.fill(0)))
        camera = SimpleNamespace(capture_request=lambda: request)

        class Mapping:
            def __init__(self, captured_request, stream):
                self.array = source

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        expected = source[:4, :4].copy()
        with TemporaryDirectory() as directory:
            save_frames(camera, SimpleNamespace(height=4, width=4), Path(directory), 1, Mapping, np)
            path = Path(directory) / "frame-00000.npy"
            np.testing.assert_array_equal(np.load(path, allow_pickle=False), expected)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(released, [True])

    def test_releases_buffer_when_mapping_fails(self):
        released = []
        request = SimpleNamespace(release=lambda: released.append(True))
        camera = SimpleNamespace(capture_request=lambda: request)

        def fail_mapping(*args):
            raise RuntimeError("mapping failed")

        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "mapping failed"):
                save_frames(camera, SimpleNamespace(height=4, width=4), Path(directory), 1,
                            fail_mapping, np)
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertEqual(released, [True])


if __name__ == "__main__":
    unittest.main()
