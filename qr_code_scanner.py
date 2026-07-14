import sys
import time
import queue
import threading
from urllib.parse import parse_qs, urlparse
from dotenv import load_dotenv
import os
import cv2
from PIL import Image, ImageDraw, ImageFont
from picamera2 import Picamera2
from pyzbar.pyzbar import decode, ZBarSymbol
import requests
from pathlib import Path

# ----------------------------
# Project / env setup
# ----------------------------
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

API_URL = os.getenv("OFG_URL")
API_TOKEN = os.getenv("OFG_API_KEY")
SCANNER_ID = "scanner-1"

print("ENV path:", BASE_DIR / ".env")
print("URL:", repr(API_URL))
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
BUZZER_PIN = 13       # physical pin 12

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
USE_BUZZER = True
LED_BRIGHTNESS = 1.0
SUCCESS_HOLD_SECONDS = 5
RESULT_HOLD_SECONDS = 0.8
CAMERA_CAPTURE_TIMEOUT_SECONDS = 5

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


try:
    from gpiozero import PWMOutputDevice

    buzzer = PWMOutputDevice(
        BUZZER_PIN,
        active_high=True,
        initial_value=0,
        frequency=1000,
    )

except Exception as e:
    USE_BUZZER = False
    buzzer = None
    print("Buzzer disabled:", e)


def lights_off():
    if not USE_LIGHTS:
        return

    red_led.off()
    yellow_led.off()
    green_led.off()


def signal_ready():
    lights_off()


def signal_processing():
    if not USE_LIGHTS:
        return

    lights_off()
    yellow_led.value = LED_BRIGHTNESS


def signal_success():
    if not USE_LIGHTS:
        return

    lights_off()
    green_led.value = LED_BRIGHTNESS


def signal_failure():
    if not USE_LIGHTS:
        return

    lights_off()
    red_led.value = LED_BRIGHTNESS


def beep(frequency=1000, duration=0.12):
    if not USE_BUZZER:
        return

    buzzer.frequency = frequency
    buzzer.value = 0.5
    time.sleep(duration)
    buzzer.off()


def beep_success():
    beep(1200, 0.08)
    time.sleep(0.02)
    beep(1600, 0.05)


def beep_failure():
    beep(350, 0.35)


def beep_duplicate():
    beep(900, 0.2)


def hold_startup_failure(text, subtext="", error=None):
    print(f"STARTUP FAILURE: {text} {subtext}")

    if error is not None:
        print("STARTUP ERROR:", repr(error))

    signal_failure()
    beep_failure()
    show_status(text, subtext)

    while True:
        time.sleep(60)


# ----------------------------
# QR/API helpers
# ----------------------------
def parse_qr_url(qr_data):
    parsed = urlparse(qr_data)
    params = parse_qs(parsed.query)

    return {
        "company_id": params.get("company_id", [""])[0],
        "attendee": params.get("attendee", [""])[0],
    }


def send_checkin(qr_data):
    qr = parse_qr_url(qr_data)

    if not qr["company_id"] or not qr["attendee"]:
        return {
            "success": False,
            "status": "invalid",
            "message": "Missing company_id or attendee",
        }

    try:
        response = requests.post(
            API_URL,
            json={
                "company_id": qr["company_id"],
                "attendee": qr["attendee"],
                "scanner_id": SCANNER_ID,
            },
            headers={
                "X-Scanner-Token": API_TOKEN,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "OFG-QR-Scanner/1.0",
            },
            timeout=10,
        )

        print("API URL:", API_URL)
        print("API status:", response.status_code)
        print("API content-type:", response.headers.get("content-type"))
        print("API body:", response.text[:500])

        try:
            return response.json()
        except ValueError:
            return {
                "success": False,
                "status": "bad_response",
                "message": "Server did not return JSON",
                "http_status": response.status_code,
                "body": response.text[:500],
            }

    except requests.RequestException as e:
        print("REQUEST ERROR:", repr(e))

        return {
            "success": False,
            "status": "offline",
            "message": str(e),
        }


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

EPAPER_LIB = "/home/viztech/e-Paper/RaspberryPi_JetsonNano/python/lib"

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


def show_status(text, subtext=""):
    global _last_eink_message

    print(f"STATUS: {text} {subtext}")

    if not USE_EINK or epd is None:
        return

    message_key = (text, subtext)

    # Prevent unnecessary full e-paper refreshes
    if message_key == _last_eink_message:
        return

    _last_eink_message = message_key

    image = Image.new("1", (EINK_WIDTH, EINK_HEIGHT), 255)
    draw = ImageDraw.Draw(image)

    draw.text((10, 25), text, font=font_big, fill=0)

    if subtext:
        draw.text((10, 65), subtext[:30], font=font_small, fill=0)

    epd.display(epd.getbuffer(image))


def capture_camera_frame(timeout=CAMERA_CAPTURE_TIMEOUT_SECONDS):
    result_queue = queue.Queue(maxsize=1)

    def capture():
        try:
            result_queue.put(("frame", picam2.capture_array("main")), block=False)
        except Exception as e:
            result_queue.put(("error", e), block=False)

    thread = threading.Thread(target=capture, daemon=True)
    thread.start()

    try:
        result_type, result = result_queue.get(timeout=timeout)
    except queue.Empty as e:
        raise TimeoutError("Timed out waiting for camera frame") from e

    if result_type == "error":
        raise result

    if result is None or result.size == 0:
        raise RuntimeError("Camera returned an empty frame")

    return result


# ----------------------------
# Camera setup
# ----------------------------
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

    # Start continuous autofocus
    picam2.set_controls({"AfMode": 2})

    capture_camera_frame()

except Exception as e:
    hold_startup_failure("STARTUP FAIL", "Camera error", e)

print("Scanner started. Press q to quit.")
signal_ready()
beep(1800, 0.355555)
time.sleep(0.02)
beep(2000, 0.35)
show_status("READY", "Scan badge QR")

seen = set()
frame_count = 0

try:
    while True:
        try:
            frame = capture_camera_frame()
        except Exception as e:
            hold_startup_failure("STARTUP FAIL", "Camera error", e)

        frame_count += 1

        # Extract grayscale plane from YUV420
        gray = frame[:HEIGHT, :WIDTH]

        codes = []

        # Decode every 3rd frame for performance
        if frame_count % 3 == 0:
            codes = decode(gray, symbols=[ZBarSymbol.QRCODE])

        for code in codes:
            data = code.data.decode("utf-8")

            if data in seen:
                print("Duplicate:", data)
                signal_processing()
                beep_duplicate()
                show_status("DUPLICATE", "Already scanned")
                time.sleep(RESULT_HOLD_SECONDS)
                signal_ready()
                show_status("READY", "Scan next badge")
                continue

            seen.add(data)

            print("QR:", data)

            signal_processing()

            result = send_checkin(data)
            status = result.get("status")
            result_hold_seconds = RESULT_HOLD_SECONDS

            if status == "checked_in":
                print("Checked in:", result)
                signal_success()
                beep_success()
                show_status("CHECKED IN", result.get("attendee", "")[:30])
                result_hold_seconds = SUCCESS_HOLD_SECONDS

            elif status == "not_found":
                print("Not found:", result)
                signal_failure()
                beep_failure()
                show_status("NOT FOUND", "See kiosk")

            elif status == "invalid":
                print("Invalid:", result)
                signal_failure()
                beep_failure()
                show_status("INVALID QR", "Missing data")

            elif status == "offline":
                print("Offline:", result)
                signal_failure()
                beep_failure()
                show_status("OFFLINE", "Network error")

            elif status == "bad_response":
                print("Bad response:", result)
                signal_failure()
                beep_failure()
                show_status("BAD RESPONSE", str(result.get("http_status", "")))

            else:
                print("Error:", result)
                signal_failure()
                beep_failure()
                show_status("ERROR", "See kiosk")

            time.sleep(result_hold_seconds)

            signal_ready()
            show_status("READY", "Scan next badge")

        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

finally:
    lights_off()

    if USE_BUZZER and buzzer is not None:
        buzzer.off()

    picam2.stop()
    cv2.destroyAllWindows()

    if USE_EINK and epd is not None:
        epd.sleep()
