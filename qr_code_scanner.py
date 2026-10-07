import os
from pathlib import Path
import queue
import signal
import sys
import threading
import time

from dotenv import load_dotenv
from picamera2 import Picamera2
import requests

from scanner_camera import LatestFrameCapture, configure_camera
from scanner_config import ScannerSettings
from scanner_core import (
    ScanOutbox,
    SeenPayloadCache,
    is_retryable_result,
    parse_qr_url,
    payload_fingerprint,
)
from scanner_decode import QrDecoder, create_decoder
from scanner_feedback import FeedbackWorker
from scanner_metrics import Metrics
from scanner_pipeline import ScanPipeline

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

# ----------------------------
# LED / buzzer setup
# ----------------------------
USE_LIGHTS = True
USE_STRIP = True
USE_BUZZER = True
LED_BRIGHTNESS = 1.0
BUZZER_VOLUME = 0.5  # PWM duty cycle, not a linear volume control.
BUZZER_SCAN_FREQUENCY = 4000  # Selected by listening at the installed kiosk.
SUCCESS_HOLD_SECONDS = 5
STRIP_FLASH_SECONDS = 0.4
RESULT_HOLD_SECONDS = 0.8
CAMERA_CAPTURE_TIMEOUT_SECONDS = 5
QR_REARM_SECONDS = 0.4
API_TIMEOUT = (3.05, 10.0)
SOUND_QUEUE_SIZE = 4
SEEN_PAYLOAD_LIMIT = 10_000
SEEN_PAYLOAD_TTL_SECONDS = 24 * 60 * 60
STARTUP_FAILURE_RETRY_SECONDS = 10
OUTBOX_PATH = BASE_DIR / "scanner_outbox.sqlite3"
OUTBOX_RETRY_BASE_SECONDS = 5
OUTBOX_RETRY_MAX_SECONDS = 300
OUTBOX_LEASE_SECONDS = 60

shutdown_event = threading.Event()
strip_lock = threading.Lock()
strip_generation = 0


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


def strip_set(red, green, blue, *, expected_generation=None):
    global strip_generation
    if not USE_STRIP or status_strip is None:
        return None

    with strip_lock:
        if expected_generation is not None and expected_generation != strip_generation:
            return None
        strip_generation += 1
        color = Color(red, green, blue)

        for i in range(status_strip.numPixels()):
            status_strip.setPixelColor(i, color)

        status_strip.show()
        return strip_generation


def strip_off():
    strip_set(0, 0, 0)


def strip_flash_green():
    generation = strip_set(0, 255, 0)
    if generation is None:
        return
    # A newer flash or shutdown must take precedence over this timer.
    timer = threading.Timer(
        STRIP_FLASH_SECONDS,
        lambda: strip_set(0, 0, 0, expected_generation=generation),
    )
    timer.daemon = True
    timer.start()


def strip_test_marker(name, red, green, blue):
    if not USE_STRIP or status_strip is None:
        return

    print(f"STRIP TEST: {name}")
    strip_set(red, green, blue)
    time.sleep(1)


# Keep the strip dark between scan feedback flashes.
strip_off()


try:
    from gpiozero import PWMOutputDevice

    buzzer = PWMOutputDevice(
        BUZZER_PIN,
        active_high=True,
        initial_value=0,
        frequency=1000,
    )
    strip_test_marker("after strip init - OFF", 0, 0, 0)

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
    # Keep the strip off while waiting for a badge.
    traffic_lights_off()
    strip_off()


def signal_processing():
    # Keep the existing traffic-light yellow processing indication.
    # Keep the strip off while the API request is running.
    traffic_lights_off()

    if USE_LIGHTS:
        yellow_led.value = LED_BRIGHTNESS

    strip_off()


def signal_success():
    traffic_lights_off()

    if USE_LIGHTS:
        green_led.value = LED_BRIGHTNESS

    # API confirmation must not interrupt an immediate capture flash.


def signal_saved():
    signal_success()
    strip_flash_green()


def signal_duplicate():
    # A locally seen badge may still be awaiting server confirmation.
    signal_saved()


def signal_failure():
    traffic_lights_off()

    if USE_LIGHTS:
        red_led.value = LED_BRIGHTNESS

    strip_off()


def play_tone(frequency=1000, duration=0.12):
    if not USE_BUZZER:
        return

    buzzer.frequency = frequency
    buzzer.value = BUZZER_VOLUME
    time.sleep(duration)
    buzzer.off()


def play_saved_sound():
    play_tone(BUZZER_SCAN_FREQUENCY, 0.08)
    time.sleep(0.02)
    play_tone(BUZZER_SCAN_FREQUENCY, 0.05)


def play_failure_sound():
    play_tone(BUZZER_SCAN_FREQUENCY, 0.35)


def play_duplicate_sound():
    play_tone(BUZZER_SCAN_FREQUENCY, 0.2)


def play_startup_sound():
    play_tone(1800, 0.355555)
    time.sleep(0.02)
    play_tone(2000, 0.35)


sound_queue = queue.Queue(maxsize=SOUND_QUEUE_SIZE)
sound_stop_event = threading.Event()


def sound_worker():
    sounds = {
        "startup": play_startup_sound,
        "saved": play_saved_sound,
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
            timeout=API_TIMEOUT,
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


def create_api_session():
    session = requests.Session()
    session.headers.update(
        {
            "X-Scanner-Token": API_TOKEN,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "OFG-QR-Scanner/1.0",
        }
    )
    return session


def show_status(text, subtext=""):
    print(f"STATUS: {text} {subtext}")


def present_checkin_result(scan_id, result, elapsed_seconds):
    status = result.get("status")

    print(
        f"Scan {scan_id} completed in {elapsed_seconds:.3f}s "
        f"with status {status!r}"
    )

    if status == "checked_in":
        print(f"Scan {scan_id} checked in")
        signal_success()
        attendee = str(result.get("attendee") or "")[:30]
        show_status("CHECKED IN", attendee)
        return SUCCESS_HOLD_SECONDS

    if status == "queued":
        retry_status = result.get("retry_status", "pending API delivery")
        print(f"Scan {scan_id} queued after {retry_status!r} API result")
        signal_processing()
        show_status("QUEUED", "Will sync")
        return RESULT_HOLD_SECONDS

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
        print(f"Scan {scan_id} was rejected because the persistence queue is full")
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
    global API_TIMEOUT

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    picam2 = None
    camera_capture = None
    outbox = None
    pipeline = None
    feedback = None
    exit_code = 0
    metrics = Metrics()
    state_events = queue.SimpleQueue()
    latest_feedback_id = None

    def ready():
        signal_ready()
        show_status("READY", "Scan next badge")

    def present_event(event):
        nonlocal latest_feedback_id

        kind = event["kind"]
        scan_label = event.get("scan_id")
        if scan_label is None and event.get("raw_payload") is not None:
            scan_label = payload_fingerprint(event["raw_payload"])
        if kind == "accepted":
            latest_feedback_id = event["scan_id"]
            signal_saved()
            queue_sound("saved")
            show_status("SAVED", "Scan next badge")
            print(f"Scan {scan_label} durably saved in {event['elapsed']:.3f}s")
            metrics.observe("decode_to_saved_feedback", time.monotonic() - event["detected_at"])
            return RESULT_HOLD_SECONDS
        if kind == "duplicate":
            latest_feedback_id = None
            signal_duplicate()
            queue_sound("duplicate")
            show_status("DUPLICATE", "Already scanned")
            return RESULT_HOLD_SECONDS
        if kind == "error":
            print(f"Scan worker error: {event['stage']} / {event['error']}")
            if event.get("scan_id") is None:
                return None
            latest_feedback_id = None
            # Disk failure before saving must never be presented as acceptance.
            result = {"status": "outbox_error", "success": False}
            return present_checkin_result(scan_label, result, 0)
        if kind == "result":
            result = event["result"]
            if event.get("scan_id") is None:
                print(f"Background QR {scan_label} status {result.get('status')!r}")
                return None
            if "raw_payload" not in event:
                # A new local validation/BUSY outcome supersedes older feedback.
                latest_feedback_id = event["scan_id"]
            if event["scan_id"] != latest_feedback_id and (
                result.get("status") in {"checked_in", "queued"}
                or is_retryable_result(result)
            ):
                print(f"Scan {scan_label} completed in background with status {result.get('status')!r}")
                return None
            if is_retryable_result(result):
                result = {"status": "queued", "retry_status": result.get("status")}
            hold = present_checkin_result(scan_label, result, event.get("elapsed", 0))
            detected_at = event.get("detected_at")
            if detected_at is not None:
                metrics.observe("decode_to_feedback", time.monotonic() - detected_at)
            return hold
        return None

    def pipeline_event(event):
        # Bookkeeping is independent of GPIO, logs, and the decoding workload.
        if event["kind"] == "accepted" or (
            event["kind"] == "error" and event["stage"] == "persistence"
        ):
            state_events.put(event)
        feedback.put(event)

    try:
        if not API_URL or not API_TOKEN:
            hold_startup_failure("STARTUP FAIL", "Missing API config")
        try:
            settings = ScannerSettings.from_env()
            API_TIMEOUT = (settings.connect_timeout, settings.read_timeout)
            decoder = QrDecoder(
                create_decoder(settings.decoder), settings.crop_size,
                settings.full_frame_interval,
            )
        except Exception as error:
            hold_startup_failure("STARTUP FAIL", "Scanner configuration", error)

        try:
            picam2 = Picamera2()
            configure_camera(picam2, settings)
            picam2.start()
            camera_capture = LatestFrameCapture(
                picam2, settings.width, settings.height, metrics=metrics,
                copy_mode=settings.copy_mode,
            )
            camera_capture.start()
            first_frame = camera_capture.get_frame(timeout=CAMERA_CAPTURE_TIMEOUT_SECONDS)
            print("Camera metadata:", {
                key: first_frame.metadata.get(key)
                for key in ("ExposureTime", "AnalogueGain", "LensPosition", "FrameDuration")
            })
            init_status_strip()
            strip_test_marker("after hardware init - OFF", 0, 0, 0)
        except Exception as error:
            hold_startup_failure("STARTUP FAIL", "Camera error", error)

        try:
            outbox = ScanOutbox(OUTBOX_PATH)
            print(f"Outbox ready with {outbox.count()} queued scans")
            feedback = FeedbackWorker(present_event, ready, metrics, settings.metrics_interval)
            feedback.start()
            pipeline = ScanPipeline(
                outbox, create_api_session, send_checkin,
                worker_count=settings.api_workers,
                queue_size=settings.persistence_queue,
                retry_base=OUTBOX_RETRY_BASE_SECONDS,
                retry_max=OUTBOX_RETRY_MAX_SECONDS,
                lease_seconds=OUTBOX_LEASE_SECONDS,
                on_event=pipeline_event, metrics=metrics,
            )
            ready()
            queue_sound("startup")
            pipeline.start()
        except Exception as error:
            hold_startup_failure("STARTUP FAIL", "Outbox error", error)

        print(f"Scanner started: {settings.decoder}, crop={settings.crop_size}, "
              f"full-frame interval={settings.full_frame_interval}")
        print("Capture feedback: local durable save; API confirmation runs in background")
        print(f"Scan illumination: off with green capture flashes at brightness {STRIP_BRIGHTNESS}/255")
        seen_payloads = SeenPayloadCache(SEEN_PAYLOAD_LIMIT, SEEN_PAYLOAD_TTL_SECONDS)
        pending_payloads = set()
        payload_last_seen = {}
        retry_after = {}
        scan_id = 0

        while not shutdown_event.is_set():
            while True:
                try:
                    event = state_events.get_nowait()
                except queue.Empty:
                    break
                payload = event["raw_payload"]
                pending_payloads.discard(payload)
                if event["kind"] == "accepted":
                    seen_payloads.add(payload, time.monotonic())
                else:
                    # Permit another presentation after a failed disk write.
                    payload_last_seen.pop(payload, None)
                    retry_after[payload] = time.monotonic() + QR_REARM_SECONDS

            try:
                frame = camera_capture.get_frame(timeout=CAMERA_CAPTURE_TIMEOUT_SECONDS)
            except Exception as error:
                hold_startup_failure("STARTUP FAIL", "Camera error", error)
            if shutdown_event.is_set():
                break
            started_at = time.monotonic()
            metrics.observe("frame_buffer_wait", started_at - frame.captured_at)
            if frame.sensor_timestamp is not None:
                # libcamera SensorTimestamp uses Linux CLOCK_BOOTTIME.
                sensor_now = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
                metrics.observe("sensor_frame_age", (sensor_now - frame.sensor_timestamp) / 1e9)
            detected_payloads = decoder.decode(frame.gray)
            now = time.monotonic()
            metrics.observe("decode", now - started_at)
            if frame.sensor_timestamp is not None:
                metrics.observe("sensor_to_decode", (
                    time.clock_gettime_ns(time.CLOCK_BOOTTIME) - frame.sensor_timestamp
                ) / 1e9)
            seen_payloads.prune(now)

            for raw_payload in detected_payloads:
                previous_seen_at = payload_last_seen.get(raw_payload)
                payload_last_seen[raw_payload] = now
                retry_at = retry_after.get(raw_payload)
                if retry_at is not None and now < retry_at:
                    continue
                if raw_payload in pending_payloads or (
                    retry_at is None and previous_seen_at is not None
                    and now - previous_seen_at < QR_REARM_SECONDS
                ):
                    continue
                retry_after.pop(raw_payload, None)
                if raw_payload in seen_payloads:
                    feedback.put({"kind": "duplicate", "raw_payload": raw_payload})
                    continue
                scan_id += 1
                data = raw_payload.decode("utf-8", errors="replace")
                qr = parse_qr_url(data)
                if not qr["company_id"] or not qr["attendee"]:
                    seen_payloads.add(raw_payload, now)
                    feedback.put({"kind": "result", "scan_id": scan_id,
                                  "result": {"status": "invalid"}})
                    continue
                if pipeline.submit(scan_id, raw_payload, now):
                    pending_payloads.add(raw_payload)
                else:
                    # BUSY means not saved; do not deduplicate an unaccepted scan.
                    feedback.put({"kind": "result", "scan_id": scan_id,
                                  "result": {"status": "busy"}})
                    retry_after[raw_payload] = now + QR_REARM_SECONDS

            stale_before = now - QR_REARM_SECONDS * 4
            payload_last_seen = {payload: timestamp for payload, timestamp in payload_last_seen.items()
                                 if timestamp >= stale_before}
            retry_after = {payload: timestamp for payload, timestamp in retry_after.items()
                           if payload in payload_last_seen or timestamp > now}

    except KeyboardInterrupt:
        shutdown_event.set()
    except StartupFailure as error:
        print(f"Scanner exiting for supervisor restart: {error}")
        exit_code = 1
    finally:
        print("Scanner stopping.")
        if camera_capture is not None:
            camera_capture.stop()
        if picam2 is not None:
            try:
                picam2.stop()
            except Exception as error:
                print("Camera shutdown error:", type(error).__name__)
        if camera_capture is not None:
            camera_capture.wait()
        pipeline_stopped = pipeline is None or pipeline.stop(timeout=15)
        if not pipeline_stopped:
            print("Shutdown timeout: workers still active; unsaved scans were not acknowledged")
            exit_code = 1
        if feedback is not None:
            if not feedback.stop():
                print("Feedback worker did not stop before shutdown timeout")
                exit_code = 1
        if outbox is not None and pipeline_stopped:
            outbox.close()
        sound_stop_event.set()
        replace_queued_item(sound_queue, None)
        sound_thread.join(timeout=1)
        lights_off()
        if USE_BUZZER and buzzer is not None:
            buzzer.off()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
