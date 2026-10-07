"""Latest-frame capture with camera buffers released before frames escape."""

from dataclasses import dataclass
import queue
import threading
import time


def configure_camera(camera, settings):
    """Configure an unstarted Picamera2 and report its negotiated capture mode."""
    from libcamera import controls

    settings.validate()
    requested_controls = {"FrameRate": settings.fps}
    focus_settings = {"mode": settings.focus_mode}
    if settings.focus_mode == "manual":
        requested_controls.update({
            "AfMode": controls.AfModeEnum.Manual,
            "LensPosition": settings.lens_position,
        })
        focus_settings["lens_position"] = settings.lens_position
    else:
        requested_controls.update({
            "AfMode": controls.AfModeEnum.Continuous,
            "AfRange": getattr(controls.AfRangeEnum, settings.af_range.capitalize()),
            "AfSpeed": getattr(controls.AfSpeedEnum, settings.af_speed.capitalize()),
        })
        focus_settings.update(range=settings.af_range, speed=settings.af_speed)
    if settings.exposure_us > 0:
        requested_controls.update({
            "AeEnable": False,
            "ExposureTime": settings.exposure_us,
            "AnalogueGain": settings.gain,
        })
    options = {
        "main": {"format": "YUV420", "size": (settings.width, settings.height)},
        "controls": requested_controls,
        "queue": settings.camera_queue,
        "buffer_count": settings.buffer_count,
    }
    if settings.sensor_mode >= 0:
        modes = camera.sensor_modes
        if settings.sensor_mode >= len(modes):
            raise ValueError(
                f"SCANNER_SENSOR_MODE {settings.sensor_mode} is unavailable; "
                f"camera exposes {len(modes)} modes"
            )
        mode = modes[settings.sensor_mode]
        if settings.fps > mode["fps"]:
            raise ValueError(
                f"Requested {settings.fps:g} FPS exceeds sensor mode "
                f"{settings.sensor_mode} maximum {mode['fps']:g} FPS"
            )
        options["sensor"] = {
            "output_size": mode["size"], "bit_depth": mode["bit_depth"],
        }
    camera.configure(camera.create_video_configuration(**options))
    available_controls = camera.camera_controls
    for name in ("LensPosition", "ExposureTime", "AnalogueGain"):
        limits = available_controls.get(name)
        if limits is not None:
            print(f"Camera {name} range: {limits}")
            if name in requested_controls:
                minimum, maximum = limits[:2]
                value = requested_controls[name]
                if minimum is not None and value < minimum:
                    raise ValueError(f"Requested {name} {value} is below camera minimum {minimum}")
                if maximum is not None and value > maximum:
                    raise ValueError(f"Requested {name} {value} exceeds camera maximum {maximum}")
    configuration = camera.camera_configuration()
    print(f"Camera focus settings: {focus_settings}")
    print(f"Camera main configuration: {configuration.get('main')}")
    print(f"Camera sensor configuration: {configuration.get('sensor')}")
    return configuration


@dataclass
class Frame:
    gray: object
    captured_at: float
    sensor_timestamp: int | None
    metadata: dict


class LatestFrameCapture:
    def __init__(self, camera, width, height, metrics=None, copy_mode="luma",
                 mapped_array_factory=None):
        if copy_mode not in {"luma", "array"}:
            raise ValueError("copy_mode must be 'luma' or 'array'")
        if width < 1 or height < 1:
            raise ValueError("Frame dimensions must be positive")
        self.camera = camera
        self.width = width
        self.height = height
        self.metrics = metrics
        self.copy_mode = copy_mode
        self.mapped_array_factory = mapped_array_factory
        self.items = queue.Queue(maxsize=1)
        self.stop_event = threading.Event()
        self._publication_lock = threading.Lock()
        self.thread = threading.Thread(
            target=self.capture_frames, name="camera-capture-worker", daemon=True,
        )
        self._previous_sensor_timestamp = None

    def start(self):
        self.thread.start()

    def _replace(self, item):
        try:
            self.items.put_nowait(item)
            return
        except queue.Full:
            pass
        try:
            self.items.get_nowait()
        except queue.Empty:
            pass
        self.items.put_nowait(item)

    def _capture(self):
        started_at = time.monotonic()
        request = self.camera.capture_request()
        captured_at = time.monotonic()
        try:
            metadata = dict(request.get_metadata())
            if self.copy_mode == "array":
                # make_array already owns a full YUV copy. Keep a view of its
                # luma plane, matching the original capture_array baseline.
                array = request.make_array("main")
                gray = array[:self.height, :self.width]
            else:
                factory = self.mapped_array_factory
                if factory is None:
                    from picamera2 import MappedArray
                    factory = MappedArray
                with factory(request, "main") as mapped:
                    gray = mapped.array[:self.height, :self.width].copy(order="C")
            if gray.shape != (self.height, self.width):
                raise RuntimeError("Camera returned an unexpected grayscale shape")
        finally:
            request.release()
        sensor_timestamp = metadata.get("SensorTimestamp")
        if self.metrics is not None:
            self.metrics.observe("camera_capture", time.monotonic() - started_at)
            previous = self._previous_sensor_timestamp
            if previous is not None and sensor_timestamp is not None:
                interval = (sensor_timestamp - previous) / 1_000_000_000
                if interval > 0:
                    self.metrics.observe("sensor_frame_interval", interval)
        self._previous_sensor_timestamp = sensor_timestamp
        return Frame(gray, captured_at, sensor_timestamp, metadata)

    def capture_frames(self):
        while not self.stop_event.is_set():
            try:
                frame = self._capture()
                with self._publication_lock:
                    if not self.stop_event.is_set():
                        self._replace(("frame", frame))
            except Exception as error:
                with self._publication_lock:
                    if not self.stop_event.is_set():
                        self._replace(("error", error))
                return

    def get_frame(self, timeout=5):
        try:
            kind, value = self.items.get(timeout=timeout)
        except queue.Empty as error:
            raise TimeoutError("Timed out waiting for camera frame") from error
        if kind == "error":
            raise value
        return value

    def stop(self):
        with self._publication_lock:
            self.stop_event.set()
            try:
                self.items.get_nowait()
            except queue.Empty:
                pass

    def wait(self):
        if self.thread.ident is not None:
            self.thread.join(timeout=1)
