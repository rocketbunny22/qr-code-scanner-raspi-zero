import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scanner_decode import QrDecoder, create_decoder

try:
    import numpy as np
except ImportError:
    np = None


@unittest.skipIf(np is None, "NumPy is required for image slicing tests")
class QrDecoderTests(unittest.TestCase):
    def setUp(self):
        self.image = np.arange(480 * 640, dtype=np.uint8).reshape((480, 640))
        self.shapes = []

    def backend(self, image):
        self.shapes.append(image.shape)
        return [b"centre"] if image.shape == (320, 320) else [b"centre", b"edge"]

    def test_searches_center_crop_first(self):
        images = []

        def backend(image):
            images.append(image)
            return [b"centre"]

        decoder = QrDecoder(backend)
        self.assertEqual(decoder.decode(self.image), {b"centre"})
        np.testing.assert_array_equal(images[0], self.image[80:400, 160:480])
        self.assertEqual(len(images), 1)

    def test_full_frame_search_finds_edges_even_with_continuous_crop_hits(self):
        decoder = QrDecoder(self.backend, full_frame_interval=2)
        self.assertEqual(decoder.decode(self.image), {b"centre"})
        self.assertEqual(decoder.decode(self.image), {b"centre", b"edge"})
        self.assertEqual(self.shapes, [(320, 320), (320, 320), (480, 640)])

    def test_crop_miss_searches_full_frame_immediately(self):
        def backend(image):
            return [] if image.shape == (320, 320) else [b"edge"]

        self.assertEqual(QrDecoder(backend).decode(self.image), {b"edge"})

    def test_disabled_or_oversized_crop_decodes_full_frame_once(self):
        for size in (0, 1000):
            with self.subTest(size=size):
                self.shapes.clear()
                decoder = QrDecoder(self.backend, crop_size=size)
                self.assertEqual(decoder.decode(self.image), {b"centre", b"edge"})
                self.assertEqual(self.shapes, [(480, 640)])


class DecoderConfigurationTests(unittest.TestCase):
    def test_zxing_preserves_binary_payload_and_ignores_invalid_results(self):
        calls = []

        def read_barcodes(image, formats):
            calls.append((image, formats))
            return [SimpleNamespace(bytes=b"\xff\x00badge", valid=True),
                    SimpleNamespace(bytes=b"bad", valid=False)]

        backend = SimpleNamespace(
            read_barcodes=read_barcodes,
            BarcodeFormat=SimpleNamespace(QRCode="QR"),
        )
        with patch.dict("sys.modules", {"zxingcpp": backend}):
            self.assertEqual(create_decoder("zxingcpp")("image"), [b"\xff\x00badge"])
        self.assertEqual(calls, [("image", "QR")])

    def test_pyzbar_restricts_search_to_qr_and_preserves_binary_payload(self):
        calls = []

        def decode(image, symbols):
            calls.append((image, symbols))
            return [SimpleNamespace(data=b"\xff\x00badge")]

        backend = SimpleNamespace(decode=decode, ZBarSymbol=SimpleNamespace(QRCODE="QR"))
        with patch.dict("sys.modules", {"pyzbar": SimpleNamespace(), "pyzbar.pyzbar": backend}):
            self.assertEqual(create_decoder("pyzbar")("image"), [b"\xff\x00badge"])
        self.assertEqual(calls, [("image", ["QR"])])

    def test_rejects_invalid_configuration(self):
        with self.assertRaises(ValueError):
            QrDecoder(lambda image: [], crop_size=-1)
        with self.assertRaises(ValueError):
            QrDecoder(lambda image: [], full_frame_interval=0)
        with self.assertRaises(ValueError):
            create_decoder("unknown")


if __name__ == "__main__":
    unittest.main()
