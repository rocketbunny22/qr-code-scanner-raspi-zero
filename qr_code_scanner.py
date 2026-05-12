import sys
import time
from urllib.parse import parse_qs, urlparse

import cv2
from PIL import Image, ImageDraw, ImageFont
from picamera2 import Picamera2
from pyzbar.pyzbar import decode
import requests

sys.path.append("/home/viztech/e-Paper/RaspberryPi_JetsonNano/python/lib")

API_URL = "https://staging.ohiofurnitureguild.com/wp-json/ofg-scanner/v1/checkin"
API_TOKEN = "ohfm_sfd6g7sd6fv76sdfv76s7d8fv678v7ds7v76s8s89d7f87967678d6f76s89789789"
SCANNER_ID = "scanner-1"


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
            },
            timeout=5,
        )

        return response.json()

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
# E-ink settings
# Update these for your display
# ----------------------------
EINK_WIDTH = 250
EINK_HEIGHT = 122

USE_EINK = True

try:
    # Example Waveshare import.
    # Change this to match your exact display driver.
    #
    # Common examples:
    # from waveshare_epd import epd2in13_V4
    # epd = epd2in13_V4.EPD()
    #
    from waveshare_epd import epd2in13_V4

    epd = epd2in13_V4.EPD()
    epd.init()
    epd.Clear()

except Exception as e:
    USE_EINK = False
    print("E-ink disabled:", e)


def show_status(text, subtext=""):
    print(f"STATUS: {text} {subtext}")

    if not USE_EINK:
        return

    image = Image.new("1", (EINK_WIDTH, EINK_HEIGHT), 255)
    draw = ImageDraw.Draw(image)

    font_big = ImageFont.truetype(
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 24
    )

    font_small = ImageFont.truetype(
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14
    )

    draw.text((10, 25), text, font=font_big, fill=0)

    if subtext:
        draw.text((10, 65), subtext[:28], font=font_small, fill=0)

    epd.display(epd.getbuffer(image))


# ----------------------------
# Camera setup
# ----------------------------
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
time.sleep(2)

# Read current focused lens position
metadata = picam2.capture_metadata()
#lens_position = metadata.get("LensPosition")

#print("Locked lens position:", lens_position)

# Lock focus manually at current position
#if lens_position is not None:
#   picam2.set_controls({"AfMode": 0, "LensPosition": lens_position})
#else:
#    print("Could not read lens position; staying in continuous autofocus")

print("Scanner started. Press q to quit.")
show_status("READY", "Scan badge QR")

seen = set()
frame_count = 0
last_status = "READY"

while True:
    frame = picam2.capture_array("main")
    frame_count += 1

    # Extract grayscale plane from YUV420
    gray = frame[:HEIGHT, :WIDTH]

    codes = []

    # Decode every 3rd frame for performance
    if frame_count % 3 == 0:
        codes = decode(gray)

    # Convert grayscale to BGR only for display overlays
    display = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)

    for code in codes:
        data = code.data.decode("utf-8")

        x, y, w, h = code.rect

        cv2.rectangle(display, (x, y), (x + w, y + h), (0, 255, 0), 2)

        cv2.putText(
            display,
            data[:40],
            (x, max(y - 10, 20)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (0, 255, 0),
            2,
        )
        if data not in seen:
            seen.add(data)

            print("QR:", data)

            result = send_checkin(data)
            status = result.get("status")

            if status == "checked_in":
                print("Checked in:", result)
                show_status("CHECKED IN", result.get("attendee", "")[:28])

            elif status == "not_found":
                print("Not found:", result)
                show_status("NOT FOUND", "See kiosk")

            elif status == "invalid":
                print("Invalid:", result)
                show_status("INVALID QR", "Missing data")

            elif status == "offline":
                print("Offline:", result)
                show_status("OFFLINE", "Network error")

            else:
                print("Error:", result)
                show_status("ERROR", "See kiosk")

            time.sleep(1.5)
            show_status("READY", "Scan next badge")

        else:
            print("Duplicate:", data)
            show_status("DUPLICATE", data[:28])

            time.sleep(1.5)
            show_status("READY", "Scan next badge")

    cv2.imshow("QR Scanner Preview", display)

    if cv2.waitKey(1) & 0xFF == ord("q"):
        break

picam2.stop()
cv2.destroyAllWindows()

if USE_EINK:
    epd.sleep()
