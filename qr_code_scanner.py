import sys
import time
import queue
import threading
import signal
from dotenv import load_dotenv
import os
from PIL import Image, ImageDraw, ImageFont
from picamera2 import Picamera2
from libcamera import controls
from pyzbar.pyzbar import decode, ZBarSymbol
import requests
from pathlib import Path

from scanner_core import (
    SeenPayloadCache,
    is_retryable_result,
    parse_qr_url,
    payload_fingerprint,
)

# ----------------------------
# Project / env setup
# ----------------------------
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

API_URL = os.getenv("OFG_URL")
API_TOKEN = os.getenv("OFG_API_KEY")
SCANNER_ID = os.getenv("OFG_SCANNER_ID", "scanner-1")

print("ENV path:", BASE_DIR / ".env")
print("API URL loaded:", bool(API_URL))
print("KEY loaded:", bool(API_TOKEN))


# ----------------------------
# GPIO pin settings
# BCM numbers, not physical pin numbers
# ----------------------------

# Pi Traffic Light LEDs
RED_LED_PIN = 5       # physical pin 29
YELLOW_LED_PIN = 6    # physical pin 31
GREEN_LED_PIN = 16    # physical pin 36

# Passive beeper
BUZZER_PIN = 26       # physical pin 37

# 5V addressable LED strip
# DATA -> GPIO18 / physical pin 12
STRIP_PIN = 18
STRIP_LED_COUNT = 60
STRIP_BRIGHTNESS = 32  # 0-255, about 12.5%

# Waveshare e-paper wired connector pins
# These match the normal Waveshare Raspberry Pi SPI wiring:
EPD_RST_PIN = 17      # physical pin 11
EPD_DC_PIN = 25       # physical pin 22
EPD_CS_PIN = 8        # physical pin 24 / CE0
EPD_BUSY_PIN = 24     # physical pin 18
# EPD_DIN/MOSI = GPIO10 / physical pin 19
# EPD_CLK/SCLK = GPIO11 / physical pin 23
# EPD_VCC = 3.3V
# EPD_GND = GND


# ----------------------------
# LED / buzzer setup
# ----------------------------
USE_LIGHTS = True
USE_STRIP = True
USE_BUZZER = True
LED_BRIGHTNESS = 1.0
BUZZER_VOLUME = 0.5
SUCCESS_HOLD_SECONDS = 5
RESULT_HOLD_SECONDS = 0.8
CAMERA_CAPTURE_TIMEOUT_SECONDS = 5
QR_REARM_SECONDS = 0.4
API_WORKER_COUNT = 2
API_QUEUE_SIZE = 20
SOUND_QUEUE_SIZE = 4
SEEN_PAYLOAD_LIMIT = 10_000
SEEN_PAYLOAD_TTL_SECONDS = 24 * 60 * 60
STARTUP_FAILURE_RETRY_SECONDS = 10

shutdown_event = threading.Event()


class StartupFailure(RuntimeError):
    """A fatal scanner fault that should let the service supervisor restart us."""


def replace_queued_item(target_queue, item):
    """Put the newest item in a bounded queue, discarding one stale item if needed."""
    try:
        target_queue.put_nowait(item)
        return
    except queue.Full:
        pass

    try:
        target_queue.get_nowait()
    except queue.Empty:
        pass

    try:
        target_queue.put_nowait(item)
    except queue.Full:
        pass


try:
    from gpiozero import PWMLED

    red_led = PWMLED(RED_LED_PIN)
    yellow_led = PWMLED(YELLOW_LED_PIN)
    green_led = PWMLED(GREEN_LED_PIN)

except Exception as e:
    USE_LIGHTS = False
    red_led = None
    yellow_led = None
    green_led = None
    print("LEDs disabled:", e)


def init_status_strip():
    global status_strip, USE_STRIP

    try:
        from rpi_ws281x import PixelStrip

        status_strip = PixelStrip(
            STRIP_LED_COUNT,
            STRIP_PIN,
            800000,
            10,
            False,
            STRIP_BRIGHTNESS,
            0,
        )

        status_strip.begin()
        USE_STRIP = True

        print("LED strip initialized")

    except Exception as e:
        USE_STRIP = False
        status_strip = None
        print("LED strip disabled:", repr(e))


# Addressable status strip
try:
    from rpi_ws281x import PixelStrip, Color

    status_strip = None
    init_status_strip()

except Exception as e:
    USE_STRIP = False
    status_strip = None
    Color = None
    print("LED strip disabled:", repr(e))


def strip_set(red, green, blue):
    if not USE_STRIP or status_strip is None:
        return

    color = Color(red, green, blue)

    for i in range(status_strip.numPixels()):
        status_strip.setPixelColor(i, color)

    status_strip.show()


def strip_off():
    strip_set(0, 0, 0)


def strip_test_marker(name, red, green, blue):
    if not USE_STRIP or status_strip is None:
        return

    print(f"STRIP TEST: {name}")
    strip_set(red, green, blue)
    time.sleep(1)


# Turn the strip blue as soon as its driver is ready.
# It remains blue through camera, e-paper and API worker initialization.
strip_set(0, 0, 255)


try:
    from gpiozero import PWMOutputDevice

    buzzer = PWMOutputDevice(
        BUZZER_PIN,
        active_high=True,
        initial_value=0,
        frequency=1000,
    )
    strip_test_marker("after strip init - BLUE", 0, 0, 255)

except Exception as e:
    USE_BUZZER = False
    buzzer = None
    print("Buzzer disabled:", e)


def traffic_lights_off():
    if not USE_LIGHTS:
        return

    red_led.off()
    yellow_led.off()
    green_led.off()


def lights_off():
    traffic_lights_off()
    strip_off()


def signal_ready():
    # Scanner is loaded and waiting for a badge.
    lights_off()


def signal_processing():
    # Keep the existing traffic-light yellow processing indication.
    # The addressable strip stays off while the API request is running.
    traffic_lights_off()

    if USE_LIGHTS:
        yellow_led.value = LED_BRIGHTNESS

    strip_off()


def signal_success():
    traffic_lights_off()

    if USE_LIGHTS:
        green_led.value = LED_BRIGHTNESS

    # Brief green flash without blocking the scanner
    strip_set(0, 255, 0)
    threading.Timer(0.15, strip_off).start()


def signal_duplicate():
    traffic_lights_off()

    if USE_LIGHTS:
        green_led.value = LED_BRIGHTNESS

    # Brief green flash without blocking the scanner
    strip_set(0, 255, 0)
    threading.Timer(0.15, strip_off).start()


def signal_failure():
    traffic_lights_off()

    if USE_LIGHTS:
        red_led.value = LED_BRIGHTNESS

    strip_set(255, 0, 0)


def play_tone(frequency=1000, duration=0.12):
    if not USE_BUZZER:
        return

    buzzer.frequency = frequency
    buzzer.value = BUZZER_VOLUME
    time.sleep(duration)
    buzzer.off()


def play_success_sound():
    play_tone(1200, 0.08)
    time.sleep(0.02)
    play_tone(1600, 0.05)


def play_failure_sound():
    play_tone(350, 0.35)


def play_duplicate_sound():
    play_tone(900, 0.2)


def play_startup_sound():
    play_tone(1800, 0.355555)
    time.sleep(0.02)
    play_tone(2000, 0.35)


sound_queue = queue.Queue(maxsize=SOUND_QUEUE_SIZE)
sound_stop_event = threading.Event()


def sound_worker():
    sounds = {
        "startup": play_startup_sound,
        "success": play_success_sound,
        "failure": play_failure_sound,
        "duplicate": play_duplicate_sound,
    }

    while not sound_stop_event.is_set():
        sound_name = sound_queue.get()

        if sound_name is None:
            break

        try:
            sounds[sound_name]()
        except Exception as e:
            print("Buzzer error:", repr(e))


sound_thread = threading.Thread(
    target=sound_worker,
    name="buzzer-worker",
    daemon=True,
)
sound_thread.start()


def queue_sound(sound_name):
    if USE_BUZZER:
        replace_queued_item(sound_queue, sound_name)


def hold_startup_failure(text, subtext="", error=None):
    print(f"STARTUP FAILURE: {text} {subtext}")

    if error is not None:
        print("STARTUP ERROR:", repr(error))

    signal_failure()
    queue_sound("failure")
    show_status(text, subtext)

    if shutdown_event.wait(STARTUP_FAILURE_RETRY_SECONDS):
        raise KeyboardInterrupt

    raise StartupFailure(f"{text}: {subtext}")


# ----------------------------
# QR/API helpers
# ----------------------------
def send_checkin(qr_data, session):
    qr = parse_qr_url(qr_data)

    if not qr["company_id"] or not qr["attendee"]:
        return {
            "success": False,
            "status": "invalid",
            "message": "Missing company_id or attendee",
        }

    try:
        response = session.post(
            API_URL,
            json={
                "company_id": qr["company_id"],
                "attendee": qr["attendee"],
                "scanner_id": SCANNER_ID,
            },
            timeout=10,
        )

        try:
            result = response.json()
        except ValueError:
            return {
                "success": False,
                "status": "bad_response",
                "message": "Server did not return JSON",
                "http_status": response.status_code,
                "body": response.text[:500],
            }

        if not isinstance(result, dict):
            return {
                "success": False,
                "status": "bad_response",
                "message": "Server returned JSON that was not an object",
                "http_status": response.status_code,
                "body": response.text[:500],
            }

        return result

    except requests.RequestException as e:
        print("REQUEST ERROR:", repr(e))

        return {
            "success": False,
            "status": "offline",
            "message": str(e),
        }


api_request_queue = queue.Queue(maxsize=API_QUEUE_SIZE)
api_result_queue = queue.Queue()
api_threads = []


def api_worker():
    with requests.Session() as session:
        session.headers.update(
            {
                "X-Scanner-Token": API_TOKEN,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "OFG-QR-Scanner/1.0",
            }
        )

        while True:
            request_item = api_request_queue.get()

            if request_item is None:
                break

            scan_id, qr_data, raw_payload = request_item
            started_at = time.monotonic()

            try:
                result = send_checkin(qr_data, session)
            except Exception as e:
                print("CHECK-IN ERROR:", repr(e))
                result = {
                    "success": False,
                    "status": "error",
                    "message": str(e),
                }

            api_result_queue.put(
                (
                    scan_id,
                    raw_payload,
                    result,
                    time.monotonic() - started_at,
                )
            )


def start_api_workers():
    for worker_number in range(API_WORKER_COUNT):
        thread = threading.Thread(
            target=api_worker,
            name=f"api-worker-{worker_number + 1}",
            daemon=True,
        )
        thread.start()
        api_threads.append(thread)


def stop_api_workers():
    # Do not wait for stale queued scans during service shutdown.
    while True:
        try:
            api_request_queue.get_nowait()
        except queue.Empty:
            break

    for _ in api_threads:
        api_request_queue.put(None)

    for thread in api_threads:
        thread.join(timeout=0.5)


# ----------------------------
# Camera settings
# ----------------------------
WIDTH = 640
HEIGHT = 480


# ----------------------------
# E-paper setup
# ----------------------------
USE_EINK = True
epd = None
EINK_WIDTH = 250
EINK_HEIGHT = 122
_last_eink_message = None

EPAPER_LIB = os.getenv(
    "EPAPER_LIB",
    str(Path.home() / "e-Paper/RaspberryPi_JetsonNano/python/lib"),
)

if EPAPER_LIB not in sys.path:
    sys.path.append(EPAPER_LIB)


def clear_epaper():
    if not USE_EINK or epd is None:
        return

    try:
        epd.Clear(0xFF)
    except TypeError:
        epd.Clear()


def load_font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


font_big = load_font(
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    24,
)

font_small = load_font(
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    14,
)


try:
    # This section is for the wired connector setup.
    #
    # Wire the e-paper connector like this:
    # VCC  -> 3.3V
    # GND  -> GND
    # DIN  -> GPIO10 / physical pin 19
    # CLK  -> GPIO11 / physical pin 23
    # CS   -> GPIO8  / physical pin 24
    # DC   -> GPIO25 / physical pin 22
    # RST  -> GPIO17 / physical pin 11
    # BUSY -> GPIO24 / physical pin 18

    from waveshare_epd import epd2in13_V4

    epd = epd2in13_V4.EPD()
    epd.init()
    clear_epaper()

    # Most Waveshare 2.13" examples use landscape as:
    # width = epd.height, height = epd.width
    EINK_WIDTH = epd.height
    EINK_HEIGHT = epd.width

    print("E-paper enabled:", EINK_WIDTH, "x", EINK_HEIGHT)

except Exception as e:
    USE_EINK = False
    epd = None
    print("E-paper disabled:", e)


def render_status(text, subtext=""):
    global _last_eink_message

    if not USE_EINK or epd is None:
        return

    message_key = (text, subtext)

    # Prevent unnecessary full e-paper refreshes
    if message_key == _last_eink_message:
        return

    image = Image.new("1", (EINK_WIDTH, EINK_HEIGHT), 255)
    draw = ImageDraw.Draw(image)

    draw.text((10, 25), text, font=font_big, fill=0)

    if subtext:
        draw.text((10, 65), subtext[:30], font=font_small, fill=0)

    epd.display(epd.getbuffer(image))
    _last_eink_message = message_key


display_queue = queue.Queue(maxsize=1)
display_stop_event = threading.Event()


def display_worker():
    while not display_stop_event.is_set():
        status_item = display_queue.get()

        if status_item is None:
            break

        try:
            render_status(*status_item)
        except Exception as e:
            print("E-paper update failed:", repr(e))


display_thread = threading.Thread(
    target=display_worker,
    name="e-paper-worker",
    daemon=True,
)
display_thread.start()


def show_status(text, subtext=""):
    print(f"STATUS: {text} {subtext}")

    if USE_EINK and epd is not None:
        replace_queued_item(display_queue, (text, subtext))


def stop_display_worker():
    display_stop_event.set()
    replace_queued_item(display_queue, None)
    display_thread.join(timeout=3)

    return not display_thread.is_alive()


class LatestFrameCapture:
    def __init__(self, camera):
        self.camera = camera
        self.items = queue.Queue(maxsize=1)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self.capture_frames,
            name="camera-capture-worker",
            daemon=True,
        )

    def start(self):
        self.thread.start()

    def capture_frames(self):
        while not self.stop_event.is_set():
            try:
                frame = self.camera.capture_array("main")

                if frame is None or frame.size == 0:
                    raise RuntimeError("Camera returned an empty frame")

                replace_queued_item(self.items, ("frame", frame))
            except Exception as e:
                if not self.stop_event.is_set():
                    replace_queued_item(self.items, ("error", e))
                return

    def get_frame(self, timeout=CAMERA_CAPTURE_TIMEOUT_SECONDS):
        try:
            result_type, result = self.items.get(timeout=timeout)
        except queue.Empty as e:
            raise TimeoutError("Timed out waiting for camera frame") from e

        if result_type == "error":
            raise result

        return result

    def stop(self):
        self.stop_event.set()

    def wait(self):
        self.thread.join(timeout=1)


def present_checkin_result(scan_id, result, elapsed_seconds):
    status = result.get("status")

    print(
        f"Scan {scan_id} completed in {elapsed_seconds:.3f}s "
        f"with status {status!r}"
    )

    if status == "checked_in":
        print(f"Scan {scan_id} checked in")
        signal_success()
        queue_sound("success")
        attendee = str(result.get("attendee") or "")[:30]
        show_status("CHECKED IN", attendee)
        return SUCCESS_HOLD_SECONDS

    signal_failure()
    queue_sound("failure")

    if status == "not_found":
        print(f"Scan {scan_id} was not found")
        show_status("NOT FOUND", "See kiosk")
    elif status == "invalid":
        print(f"Scan {scan_id} contained invalid badge data")
        show_status("INVALID QR", "Missing data")
    elif status == "offline":
        print(f"Scan {scan_id} could not reach the API")
        show_status("OFFLINE", "Network error")
    elif status == "bad_response":
        print(f"Scan {scan_id} received an invalid API response")
        show_status("BAD RESPONSE", str(result.get("http_status", "")))
    elif status == "busy":
        print(f"Scan {scan_id} was rejected because the request queue is full")
        show_status("BUSY", "Try badge again")
    else:
        print(f"Scan {scan_id} returned unexpected status {status!r}")
        show_status("ERROR", "See kiosk")

    return RESULT_HOLD_SECONDS


# ----------------------------
# Camera / scanner lifecycle
# ----------------------------
def request_shutdown(signum, _frame):
    print(f"Received signal {signum}; scanner stopping.")
    shutdown_event.set()


def main():
    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)

    picam2 = None
    camera_capture = None
    exit_code = 0

    try:
        if not API_URL or not API_TOKEN:
            hold_startup_failure("STARTUP FAIL", "Missing API config")

        try:
            picam2 = Picamera2()

            picam2.configure(
                picam2.create_video_configuration(
                    main={"format": "YUV420", "size": (WIDTH, HEIGHT)},
                    controls={"FrameRate": 30},
                )
            )

            picam2.start()

            # Use the calibrated fixed-focus position for the kiosk scan distance.
            picam2.set_controls({
                "AfMode": controls.AfModeEnum.Manual,
                "LensPosition": 10.0,
            })

            camera_capture = LatestFrameCapture(picam2)
            camera_capture.start()
            camera_capture.get_frame()
            print("Camera fully initialized")

            # Reclaim the WS281x hardware after all other hardware is ready.
            init_status_strip()
            strip_test_marker("after hardware init - GREEN", 0, 255, 0)

        except Exception as e:
            hold_startup_failure("STARTUP FAIL", "Camera error", e)

        start_api_workers()

        print("Scanner started. Press Ctrl+C to quit.")
        signal_ready()
        queue_sound("startup")
        show_status("READY", "Scan badge QR")

        seen_payloads = SeenPayloadCache(
            max_entries=SEEN_PAYLOAD_LIMIT,
            ttl_seconds=SEEN_PAYLOAD_TTL_SECONDS,
        )
        payload_last_seen = {}
        scan_id = 0
        pending_scans = 0
        feedback_ready_at = 0.0

        while not shutdown_event.is_set():
            while True:
                try:
                    completed_scan = api_result_queue.get_nowait()
                except queue.Empty:
                    break

                completed_id, raw_payload, result, elapsed_seconds = completed_scan
                pending_scans = max(0, pending_scans - 1)

                # Transport/protocol failures are safe to retry after the badge
                # leaves the camera view. Definitive API outcomes remain deduped.
                if is_retryable_result(result):
                    seen_payloads.discard(raw_payload)

                hold_seconds = present_checkin_result(
                    completed_id,
                    result,
                    elapsed_seconds,
                )
                feedback_ready_at = time.monotonic() + hold_seconds

            now = time.monotonic()

            if feedback_ready_at and now >= feedback_ready_at:
                feedback_ready_at = 0.0

                if pending_scans:
                    signal_processing()
                    show_status("PROCESSING", "Checking badge")
                else:
                    signal_ready()
                    show_status("READY", "Scan next badge")

            try:
                frame = camera_capture.get_frame()
            except Exception as e:
                hold_startup_failure("STARTUP FAIL", "Camera error", e)

            if shutdown_event.is_set():
                break

            # Extract grayscale plane from YUV420
            gray = frame[:HEIGHT, :WIDTH]
            codes = decode(gray, symbols=[ZBarSymbol.QRCODE])
            detected_payloads = {bytes(code.data) for code in codes}
            now = time.monotonic()
            seen_payloads.prune(now)

            for raw_payload in detected_payloads:
                previous_seen_at = payload_last_seen.get(raw_payload)
                payload_last_seen[raw_payload] = now

                if (
                    previous_seen_at is not None
                    and now - previous_seen_at < QR_REARM_SECONDS
                ):
                    continue

                data = raw_payload.decode("utf-8", errors="replace")
                fingerprint = payload_fingerprint(raw_payload)

                if raw_payload in seen_payloads:
                    print(f"Duplicate QR {fingerprint}")
                    signal_duplicate()
                    queue_sound("duplicate")
                    show_status("DUPLICATE", "Already scanned")
                    feedback_ready_at = now + RESULT_HOLD_SECONDS
                    continue

                scan_id += 1
                seen_payloads.add(raw_payload, now)

                try:
                    api_request_queue.put_nowait((scan_id, data, raw_payload))
                except queue.Full:
                    seen_payloads.discard(raw_payload)
                    result = {"status": "busy", "success": False}
                    hold_seconds = present_checkin_result(scan_id, result, 0.0)
                    feedback_ready_at = now + hold_seconds
                    continue

                pending_scans += 1
                print(f"Scan {scan_id} QR {fingerprint}")
                signal_processing()
                show_status("PROCESSING", "Checking badge")

            stale_before = now - (QR_REARM_SECONDS * 4)
            payload_last_seen = {
                payload: last_seen_at
                for payload, last_seen_at in payload_last_seen.items()
                if last_seen_at >= stale_before
            }

    except KeyboardInterrupt:
        shutdown_event.set()
    except StartupFailure as e:
        print(f"Scanner exiting for supervisor restart: {e}")
        exit_code = 1
    finally:
        print("Scanner stopping.")

        if camera_capture is not None:
            camera_capture.stop()

        if picam2 is not None:
            try:
                picam2.stop()
            except Exception as e:
                print("Camera shutdown error:", repr(e))

        if camera_capture is not None:
            camera_capture.wait()

        stop_api_workers()

        sound_stop_event.set()
        replace_queued_item(sound_queue, None)
        sound_thread.join(timeout=1)

        lights_off()

        if USE_BUZZER and buzzer is not None:
            buzzer.off()

        display_stopped = stop_display_worker()

        if USE_EINK and epd is not None and display_stopped:
            epd.sleep()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
