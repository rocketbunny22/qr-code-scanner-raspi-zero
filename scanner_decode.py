"""Lazy QR decoder adapters and a bounded full-frame search policy."""


def create_decoder(backend="pyzbar"):
    """Return a grayscale-image callable producing original payload bytes.

    Dependencies load only when selected, so core tests need no camera or native
    decoder libraries. A missing requested backend fails explicitly at startup.
    """
    if backend == "pyzbar":
        from pyzbar.pyzbar import ZBarSymbol, decode

        def decode_image(image):
            return [bytes(code.data) for code in decode(image, symbols=[ZBarSymbol.QRCODE])]

    elif backend == "zxingcpp":
        import zxingcpp

        def decode_image(image):
            return [
                bytes(code.bytes)
                for code in zxingcpp.read_barcodes(image, formats=zxingcpp.BarcodeFormat.QRCode)
                if code.valid
            ]

    else:
        raise ValueError(f"Unknown QR decoder backend: {backend}")
    return decode_image


class QrDecoder:
    """Search the centre first, preserving off-centre discovery on crop hits.

    Every ``full_frame_interval`` frames also searches the full image, even
    when the crop reads successfully. Crop misses search the full frame
    immediately. Set ``crop_size=0`` to disable cropping for comparison.
    """

    def __init__(self, decoder, crop_size=320, full_frame_interval=5):
        if crop_size < 0:
            raise ValueError("crop_size must be nonnegative")
        if full_frame_interval < 1:
            raise ValueError("full_frame_interval must be positive")
        self.decoder = decoder
        self.crop_size = crop_size
        self.full_frame_interval = full_frame_interval
        self._frame_number = 0

    def decode(self, image):
        self._frame_number += 1
        height, width = image.shape[:2]
        crop_height = min(self.crop_size, height)
        crop_width = min(self.crop_size, width)
        if not self.crop_size or (crop_height == height and crop_width == width):
            return set(self.decoder(image))

        top = (height - crop_height) // 2
        left = (width - crop_width) // 2
        payloads = set(self.decoder(image[top:top + crop_height, left:left + crop_width]))
        if not payloads or self._frame_number % self.full_frame_interval == 0:
            payloads.update(self.decoder(image))
        return payloads
