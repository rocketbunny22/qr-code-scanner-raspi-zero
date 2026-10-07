"""Validated performance controls shared by deployment and diagnostics."""

from dataclasses import dataclass
import math
import os


@dataclass(frozen=True)
class ScannerSettings:
    width: int = 640
    height: int = 480
    fps: float = 30.0
    lens_position: float = 20.0
    focus_mode: str = "manual"
    af_range: str = "full"
    af_speed: str = "fast"
    exposure_us: int = 0
    gain: float = 1.0
    buffer_count: int = 4
    camera_queue: bool = False
    copy_mode: str = "luma"
    sensor_mode: int = -1
    decoder: str = "pyzbar"
    crop_size: int = 384
    full_frame_interval: int = 3
    api_workers: int = 2
    persistence_queue: int = 20
    connect_timeout: float = 3.05
    read_timeout: float = 10.0
    metrics_interval: float = 30.0

    @classmethod
    def from_env(cls, environ=None):
        env = os.environ if environ is None else environ
        defaults = cls()
        values = {}
        for name in cls.__dataclass_fields__:
            default = getattr(defaults, name)
            key = "SCANNER_" + name.upper()
            raw = env.get(key, str(default))
            try:
                if isinstance(default, bool):
                    if raw.lower() not in {"true", "false", "1", "0"}:
                        raise ValueError()
                    values[name] = raw.lower() in {"true", "1"}
                else:
                    values[name] = type(default)(raw)
            except (ValueError, TypeError) as error:
                raise ValueError(f"Invalid {key}") from error
        settings = cls(**values)
        settings.validate()
        return settings

    def validate(self):
        for name in ("width", "height", "fps", "gain", "api_workers",
                     "persistence_queue", "connect_timeout", "read_timeout",
                     "full_frame_interval"):
            if getattr(self, name) <= 0:
                raise ValueError(f"SCANNER_{name.upper()} must be positive")
        for name in ("fps", "gain", "connect_timeout", "read_timeout",
                     "metrics_interval", "lens_position"):
            if not math.isfinite(getattr(self, name)):
                raise ValueError(f"SCANNER_{name.upper()} must be finite")
        if self.width % 2 or self.height % 2:
            raise ValueError("YUV420 width and height must be even")
        if self.buffer_count < 2:
            raise ValueError("SCANNER_BUFFER_COUNT must be at least 2")
        if min(self.crop_size, self.exposure_us, self.lens_position,
               self.metrics_interval) < 0 or self.sensor_mode < -1:
            raise ValueError("Invalid negative scanner setting")
        if self.exposure_us > 1_000_000 / self.fps:
            raise ValueError("Exposure exceeds the requested frame period")
        if self.decoder not in {"pyzbar", "zxingcpp"}:
            raise ValueError("SCANNER_DECODER must be pyzbar or zxingcpp")
        if self.copy_mode not in {"luma", "array"}:
            raise ValueError("SCANNER_COPY_MODE must be luma or array")
        if self.focus_mode not in {"manual", "continuous"}:
            raise ValueError("SCANNER_FOCUS_MODE must be manual or continuous")
        if self.af_range not in {"normal", "macro", "full"}:
            raise ValueError("SCANNER_AF_RANGE must be normal, macro, or full")
        if self.af_speed not in {"normal", "fast"}:
            raise ValueError("SCANNER_AF_SPEED must be normal or fast")
