# OFG QR Code Scanner

A headless QR-badge check-in kiosk for the Ohio Furniture Market. It is designed for a Raspberry Pi Zero 2 W with a Raspberry Pi Camera Module 3, a three-LED traffic-light indicator, a 60-pixel WS281x status strip, and a passive buzzer.

The scanner continuously captures camera frames, decodes QR codes, sends the badge data to the configured OFG check-in API, and gives immediate visual and audible feedback. It can be started interactively or installed as a `systemd` service that restarts after a crash or reboot.

## Contents

- [What it does](#what-it-does)
- [Hardware](#hardware)
- [Wiring](#wiring)
- [Software and dependencies](#software-and-dependencies)
- [How a scan works](#how-a-scan-works)
- [API contract](#api-contract)
- [Provisioning a bare Raspberry Pi](#provisioning-a-bare-raspberry-pi)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running the scanner](#running-the-scanner)
- [Status indicators](#status-indicators)
- [Operations and troubleshooting](#operations-and-troubleshooting)
- [Project layout](#project-layout)
- [Current implementation notes](#current-implementation-notes)

## What it does

1. Starts the Pi camera at 640 x 480 pixels and 30 FPS.
2. Uses a fixed manual camera lens position of `20.0`.
3. Continuously captures frames into a one-frame latest-value buffer and decodes QR codes from every frame the processor can consume.
4. Accepts QR payloads that are URLs containing `company_id` and `attendee` query-string parameters.
5. Sends those values, plus the configured scanner identifier, to the OFG API using an authenticated JSON `POST` request.
6. Signals the result through the traffic lights, addressable strip, buzzer, and service logs without pausing camera capture or QR decoding.
7. Keeps successful and definitive badge outcomes in a bounded 24-hour in-memory history so the same QR payload is not repeatedly submitted.
8. Saves valid scans in a local SQLite outbox before submission and retries them after an API outage.

There is no browser UI or camera preview. The LEDs, buzzer, and service logs are the operating interface.

## Hardware

| Component | Required | Purpose |
| --- | --- | --- |
| Raspberry Pi Zero 2 W | Yes | Runs the scanner and drives GPIO hardware. |
| Raspberry Pi Camera Module 3 | Yes | Captures badge QR codes through `picamera2`/libcamera. |
| Red LED | Expected | Failure/startup-failure indicator. |
| Yellow LED | Expected | QR processing indicator. |
| Green LED | Expected | Successful check-in and duplicate indicator. |
| 60-pixel WS281x LED strip | Expected | Startup test colors and brief success/duplicate or steady failure feedback. |
| Passive buzzer | Expected | Audible startup, success, duplicate, and failure feedback. |
| Appropriate power supply, current-limiting resistors, and wiring | Yes | Required for the LED strip, discrete LEDs, and safe GPIO connection. |

The hardware outputs are optional at runtime: initialization failures for the LEDs, strip, or buzzer are caught and logged, and scanning continues without that device. Camera and API configuration failures instead put the process into a visible startup-failure state.

## Wiring

GPIO names below are Broadcom (BCM) GPIO numbers. Physical header pin numbers are included to prevent mixing BCM and board numbering.

| Function | BCM GPIO | Physical pin | Notes |
| --- | ---: | ---: | --- |
| Red LED | GPIO 5 | 29 | PWM output through `gpiozero.PWMLED`. |
| Yellow LED | GPIO 6 | 31 | PWM output through `gpiozero.PWMLED`. |
| Green LED | GPIO 16 | 36 | PWM output through `gpiozero.PWMLED`. |
| Passive buzzer | GPIO 26 | 37 | PWM output at configurable frequencies. |
| WS281x strip data | GPIO 18 | 12 | Data signal for the 60-pixel status strip. Use a suitable external 5 V supply and a common ground. |

The pin values are declared near the top of [`qr_code_scanner.py`](qr_code_scanner.py). If the wiring changes, update the corresponding BCM constants there.

## Software and dependencies

The target operating system is Raspberry Pi OS with the camera stack available. The setup script installs these system packages:

```text
git
python3
python3-venv
python3-pip
python3-picamera2
python3-numpy
python3-gpiozero
python3-lgpio
libzbar0
rpicam-apps
```

It creates a virtual environment that reuses system site packages, then installs:

```text
requests
python-dotenv
pyzbar
rpi-ws281x
```

## How a scan works

```text
Camera frame
    -> YUV luminance plane
    -> latest-frame buffer
    -> pyzbar QR-only decode
    -> parse company_id and attendee from the QR URL
    -> one of two authenticated API workers
    -> non-blocking traffic-light + LED-strip + buzzer result
```

Camera capture runs continuously in one persistent worker instead of creating a thread for every frame. Its queue holds only one frame: if QR decoding is temporarily slower than the camera, an old unprocessed frame is replaced by the newest frame instead of building a latency-producing backlog. Resolution remains 640 x 480 and the decoder remains restricted to QR codes.

API requests and buzzer patterns have independent workers. A slow network response, sound, or five-second success hold therefore does not pause detection of the next badge. Two API workers can process separate badges concurrently, and each worker reuses its HTTP session and underlying connection where the server permits it.

The program allows five seconds for the camera worker to supply a frame. If the camera does not return a usable frame, it shows a red LED, failure tone, and `STARTUP FAIL / Camera error` for ten seconds before exiting with an error. The installed `systemd` service can then restart it automatically.

### Accepted QR payload format

The payload is parsed as a URL. Only the first value for each query parameter is used. The host and URL path are not validated by the scanner.

```text
https://example.invalid/check-in?company_id=123&attendee=456
```

This produces the following API payload:

```json
{
  "company_id": "123",
  "attendee": "456",
  "scanner_id": "scanner-1"
}
```

Missing either parameter produces a local `invalid` result without making an API request.

### Duplicate behavior

Every queued QR payload is added to a bounded in-memory history. A code that remains in the camera view is ignored after its first detection rather than repeatedly generating duplicate sounds and indicator updates. After it has been absent for at least `QR_REARM_SECONDS`, presenting it again shows one `DUPLICATE` result and does not call the API.

Definitive outcomes such as `checked_in`, `not_found`, and `invalid` remain deduplicated for up to 24 hours, with a maximum history of 10,000 payloads. Every valid scan is first stored in `scanner_outbox.sqlite3`, which is private to the scanner account and survives a restart or power loss. If the API is unavailable or returns an unusable response, the scanner keeps the badge in that outbox and retries in the background with capped exponential backoff (5 seconds through 5 minutes). The entry is removed only after a definitive API response. The outbox is intentionally ignored by Git and must be treated as sensitive badge data.

## API contract

Set the endpoint and authentication secret in `.env`:

```dotenv
OFG_URL=https://your-api.example/check-in
OFG_API_KEY=replace-with-scanner-secret
# Optional: defaults to scanner-1
OFG_SCANNER_ID=scanner-1
```

For each valid QR URL, the scanner sends:

```http
POST $OFG_URL
Accept: application/json
Content-Type: application/json
User-Agent: OFG-QR-Scanner/1.0
X-Scanner-Token: $OFG_API_KEY
```

The request has a 10-second timeout. The response body must be JSON. The scanner selects its user-visible result from the response `status` field:

| API/result status | Kiosk result |
| --- | --- |
| `checked_in` | Green LED, two rising beeps, `CHECKED IN` with the response `attendee` value. This result is scheduled for up to five seconds without blocking the next scan. |
| `not_found` | Red LED, low failure tone, `NOT FOUND / See kiosk`. |
| `invalid` | Red LED, low failure tone, `INVALID QR / Missing data`. |
| `queued` | Yellow LED, no failure tone, `QUEUED / Will sync`. This is shown when a valid scan cannot be sent immediately; it remains in the local outbox for background retry. |
| `offline` | Internal retryable result produced locally for request failures, including timeout. It is stored in the outbox rather than presented as a final badge outcome. |
| `bad_response` | Internal retryable result produced when a response is unusable. It is stored in the outbox rather than presented as a final badge outcome. |
| `busy` | Red LED, low failure tone, `BUSY / Try badge again`. This is produced locally if the bounded request queue is full. |
| Any other or absent status | Red LED, low failure tone, `ERROR / See kiosk`. |

For successful check-ins, the API should return the attendee name/value in an `attendee` property for operational logs.

## Provisioning a bare Raspberry Pi

Follow this sequence for a new Pi before installing the scanner. Keep the Pi powered off while attaching the camera and all GPIO wiring.

### 1. Prepare Raspberry Pi OS

1. On another computer, use [Raspberry Pi Imager](https://www.raspberrypi.com/software/) to write the current **Raspberry Pi OS Lite (64-bit)** image to a reliable microSD card.
2. In Imager customisation, set a hostname (for example, `qrscanner`), create the `viztech` user, configure Wi-Fi if Ethernet is unavailable, set the correct time zone, and enable SSH with public-key authentication where possible.
3. Insert the card, connect Ethernet if available, then power the Pi from its normal Pi-rated USB power supply. Find its address from your router or connect with `ssh viztech@qrscanner.local`.
4. Update the base OS, then reboot:

   ```bash
   sudo apt update
   sudo apt full-upgrade -y
   sudo reboot
   ```

Raspberry Pi Imager can preconfigure the hostname, user, network, and SSH access for a headless first boot. Raspberry Pi OS uses the current `libcamera` stack required by `picamera2`; do not enable the legacy camera stack.

### 2. Verify the camera before installing the app

With power disconnected, attach the Camera Module 3 Wide ribbon cable in the correct orientation for the Pi's camera connector. Reconnect power, then run:

```bash
rpicam-hello --list-cameras
```

The output should identify `imx708_wide`. If it does not, power down and recheck the ribbon-cable seating and orientation. The scanner uses manual focus with `LensPosition = 20.0`; validate focus at the final badge distance after installation.

### 3. Wire the low-current indicators

Use BCM numbering, not physical pin numbers. For each discrete LED, wire **GPIO -> 220–330 ohm resistor -> LED anode**, then LED cathode to a Pi ground pin. Wire a passive piezo buzzer's positive lead to GPIO 26 and its negative lead to ground. If a buzzer requires more than a few milliamps or is not a piezo element, drive it through a transistor/MOSFET circuit instead of directly from GPIO.

| Device | GPIO | Physical pin |
| --- | ---: | ---: |
| Red LED | GPIO 5 | 29 |
| Yellow LED | GPIO 6 | 31 |
| Green LED | GPIO 16 | 36 |
| Passive buzzer | GPIO 26 | 37 |

### 4. Wire the WS281x strip to an external supply

For 60 pixels, use a listed, regulated **5 V / 5 A** DC supply, 18–20 AWG power leads, and a 5 A inline fuse close to the supply's positive terminal. A 60-pixel strip can draw up to 3.6 A at full-white brightness. Do **not** power this strip from the Pi's 5 V header.

At the strip input (`DIN`; arrows point away from the input), connect:

```text
5 V supply +  -> inline fuse -> strip +5V
5 V supply - ---------------> strip GND
                              -> Pi GND (for example, physical pin 6)
Pi GPIO 18 (pin 12) -> 74AHCT125 level shifter -> 330–470 ohm resistor -> strip DIN
```

Power the 74AHCT125 from the same external 5 V supply: pin 14 to +5 V, pin 7 to ground, pin 1 (`1OE`) to ground, pin 2 (`1A`) to Pi GPIO 18, and pin 3 (`1Y`) through the resistor to strip `DIN`. Place a 0.1 µF ceramic capacitor across the level shifter's power pins and a 500–1000 µF electrolytic capacitor across the strip's `+5V` and `GND` at its input (observe capacitor polarity). For long strips or visible dimming at the far end, inject the same supply's +5 V and ground at the far end as well.

The shared ground is required. Never connect the external supply's +5 V lead to a Pi GPIO or the Pi's 3.3 V rail. The 74AHCT125 is recommended because Pi GPIO data is 3.3 V and a 5 V WS281x strip may not reliably accept that level directly. Keep the code's `STRIP_BRIGHTNESS = 32` until external power and the level shifter are installed and tested; only then raise it toward 255.

GPIO 18 uses the Pi's PWM hardware. Disable onboard analogue audio before using it for the strip: edit `/boot/firmware/config.txt`, change `dtparam=audio=on` to `dtparam=audio=off`, and reboot. This scanner's GPIO 26 piezo buzzer does not use the analogue audio output.

### 5. Install, configure, and bring up the scanner

Clone and run the project installer as `viztech`:

```bash
cd /home/viztech
git clone https://github.com/rocketbunny22/qr-code-scanner-raspi-zero.git
cd qr-code-scanner-raspi-zero
chmod +x scanner_init.sh
./scanner_init.sh
```

The installer prompts for `OFG_URL` and `OFG_API_KEY`, creates `.venv`, and installs/enables `qrscanner.service`. Reboot after installation, then verify the service and follow its logs:

```bash
sudo reboot
sudo systemctl status qrscanner.service --no-pager
sudo journalctl -u qrscanner.service -f
```

Before increasing strip brightness, confirm that startup produces the blue and green strip tests without Pi undervoltage warnings, rebooting, camera failures, or LED glitches.

## Installation

### 1. Place the project

The setup script derives the project path from its own location, so the checkout can live under any non-root deployment account. For example:

```text
/home/viztech/qr-code-scanner-raspi-zero/
```

For a first-time installation:

```bash
cd /home/viztech
git clone https://github.com/rocketbunny22/qr-code-scanner-raspi-zero.git qr-code-scanner-raspi-zero
cd qr-code-scanner-raspi-zero
chmod +x scanner_init.sh
./scanner_init.sh
```

Run the script as the intended non-root account (for example, `viztech`). It uses `sudo` only for system packages, service installation, and ownership changes. It prompts for `OFG_URL` and `OFG_API_KEY` only when `.env` does not already exist. SSH is left unchanged by default; run `ENABLE_SSH=1 ./scanner_init.sh` if the installer should enable it.

### 2. What `scanner_init.sh` changes

The setup script:

1. Updates APT package metadata and installs the required OS packages.
2. Optionally enables and starts SSH when `ENABLE_SSH=1` is supplied.
3. Creates `.venv` with `--system-site-packages`.
4. Installs the Python-only dependencies into that environment.
5. Creates `.env` with mode `0600` if it does not already exist.
6. Writes `/etc/systemd/system/qrscanner.service`.
7. Enables the service, but does not start it.
8. Runs an import check for the camera, GPIO, QR, and HTTP libraries.

Reboot after installation so camera configuration is cleanly initialized:

```bash
sudo reboot
```

## Configuration

### Environment file

`.env` is loaded from the directory containing `qr_code_scanner.py`, so it works for both an interactive launch and the service. Keep this file private and do not commit it.

```dotenv
# Required: complete endpoint that receives check-in requests
OFG_URL=https://your-api.example/check-in

# Required: value sent in the X-Scanner-Token request header
OFG_API_KEY=replace-with-a-secret

# Optional: unique identifier for this physical kiosk
OFG_SCANNER_ID=scanner-1
```

On boot, missing either value produces `STARTUP FAIL / Missing API config` for ten seconds and then exits with an error. The installed service retries automatically; correct the file and restart the service to apply it immediately.

### Code-level settings

These values live in [`qr_code_scanner.py`](qr_code_scanner.py):

| Setting | Current value | Effect |
| --- | ---: | --- |
| `SCANNER_ID` | `scanner-1` | Included in every API request. Assign a distinct value per physical kiosk if the API uses it for attribution. |
| `WIDTH` / `HEIGHT` | `640` / `480` | Camera capture resolution. |
| Frame rate | `30` | Requested video configuration rate. |
| `LensPosition` | `20.0` | Fixed manual focus position for Camera Module 3 Wide. |
| `LED_BRIGHTNESS` | `1.0` | PWM LED duty-cycle value. |
| `BUZZER_VOLUME` | `0.5` | PWM buzzer duty-cycle value. |
| `STRIP_LED_COUNT` | `60` | Number of addressable LEDs driven on GPIO 18. |
| `STRIP_BRIGHTNESS` | `32` | WS281x brightness from 0 through 255. |
| `STRIP_FLASH_SECONDS` | `1.0` | Duration of the green success/duplicate strip flash. |
| `SUCCESS_HOLD_SECONDS` | `5` | Maximum success-feedback hold when a newer scan does not replace it. |
| `RESULT_HOLD_SECONDS` | `0.8` | Maximum non-success feedback hold when a newer scan does not replace it. |
| `CAMERA_CAPTURE_TIMEOUT_SECONDS` | `5` | Camera-frame timeout before a startup failure is shown. |
| `QR_REARM_SECONDS` | `0.4` | Minimum absence interval before the same visible payload counts as a new presentation. |
| `API_WORKER_COUNT` | `2` | Maximum number of different badge requests processed concurrently. |
| `API_QUEUE_SIZE` | `20` | Maximum number of requests waiting behind the API workers. |
| `OUTBOX_RETRY_BASE_SECONDS` / `OUTBOX_RETRY_MAX_SECONDS` | `5` / `300` | Initial and maximum delay for replaying queued scans after a retryable API failure. |
| `SEEN_PAYLOAD_LIMIT` | `10000` | Maximum number of definitive badge outcomes retained. |
| `SEEN_PAYLOAD_TTL_SECONDS` | `86400` | How long definitive badge outcomes remain deduplicated. |

`SCANNER_ID` can be overridden without changing source by setting `OFG_SCANNER_ID` in `.env`.

The result hold values control feedback duration only. They no longer suspend camera capture or QR decoding.

## Running the scanner

### Interactive run

Use an interactive run when checking camera alignment, wiring, or API connectivity:

```bash
cd /home/viztech/qr-code-scanner-raspi-zero
source .venv/bin/activate
python qr_code_scanner.py
```

The program logs its `.env` path, whether the API URL and key were loaded, non-reversible QR fingerprints, API outcomes, and per-request completion time. Press `Ctrl+C` to exit when a keyboard and terminal are attached. The process turns off LEDs/buzzer and stops the camera and background workers during normal shutdown.

### `systemd` service

The installer creates this service conceptually:

```ini
[Unit]
Description=OFG QR Code Scanner
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=viztech
WorkingDirectory=/home/viztech/qr-code-scanner-raspi-zero/
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/viztech/qr-code-scanner-raspi-zero/.venv/bin/python -u /home/viztech/qr-code-scanner-raspi-zero/qr_code_scanner.py
Restart=always
RestartSec=5
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
```

Useful service commands:

```bash
# Start now
sudo systemctl start qrscanner.service

# Stop the scanner
sudo systemctl stop qrscanner.service

# Restart after changing .env or code
sudo systemctl restart qrscanner.service

# Confirm startup state
sudo systemctl status qrscanner.service

# Follow live logs
sudo journalctl -u qrscanner.service -f

# Show logs from this boot
sudo journalctl -u qrscanner.service -b
```

After changing `scanner_init.sh` or any generated unit value, run `sudo systemctl daemon-reload` before restarting the service. A source-only edit to `qr_code_scanner.py` needs only `sudo systemctl restart qrscanner.service`.

## Status indicators

| Situation | Light state | Sound |
| --- | --- | --- |
| Ready | Traffic lights and strip off | Two rising startup tones only at launch |
| Processing a new QR | Yellow traffic light; strip off | None before API result |
| Checked in | Green traffic light; one-second green strip | Two short rising tones |
| Duplicate payload | Green traffic light; one-second green strip | One medium tone |
| Badge not found or invalid QR | Red traffic light and strip | One low long tone |
| Queued while offline or busy | Yellow traffic light; strip off | None |
| Unexpected API status or startup failure | Red traffic light and strip | One low long tone |

## Operations and troubleshooting

### Camera does not start or scan

1. Verify the ribbon cable orientation and Camera Module 3 connection.
2. Confirm the camera stack can see hardware:

   ```bash
   rpicam-hello
   ```

3. Check the service logs for `STARTUP ERROR`:

   ```bash
   sudo journalctl -u qrscanner.service -b --no-pager
   ```

4. Confirm the installed system has `python3-picamera2` and that the virtual environment uses system site packages.
5. Adjust `LensPosition` only after validating the physical scan distance and lighting. The configured manual value is `20.0`; it does not continuously autofocus.

### LEDs or buzzer do not work

GPIO setup errors are printed as `LEDs disabled:` or `Buzzer disabled:`. Confirm BCM numbering, physical wiring, ground, resistors, and that no other process owns the pins. The scanner can still read badges without these indicators.

### The API reports offline or unexpected errors

1. Confirm `.env` exists next to the Python script and contains both required non-empty values.
2. Inspect service logs for request exceptions, API outcomes, and elapsed request time.
3. Test network/DNS connectivity from the Pi using the actual configured endpoint.
4. Verify the API accepts `application/json`, `X-Scanner-Token`, and the three JSON fields described in [API contract](#api-contract).
5. Confirm `scanner_outbox.sqlite3` is writable by the service user. Valid scans remain there until a definitive API response, and the scanner retries automatically after connectivity is restored.

### A badge should be retried

The scanner deliberately suppresses definitive outcomes for up to 24 hours. Restart the service when an operator must clear its in-memory duplicate history immediately:

```bash
sudo systemctl restart qrscanner.service
```

### Service repeatedly restarts

`Restart=always` restarts the program after every exit, including an interactive `Ctrl+C` exit. Review recent logs and service state:

```bash
sudo systemctl status qrscanner.service
sudo journalctl -u qrscanner.service -n 100 --no-pager
```

For hardware troubleshooting, stop the service before running the script interactively so both processes do not contend for the camera or GPIO:

```bash
sudo systemctl stop qrscanner.service
```

## Project layout

```text
.
├── qr_code_scanner.py  # Scanner application: camera, decoding, API, and GPIO
├── scanner_core.py     # Hardware-independent parsing and duplicate-history helpers
├── scanner_init.sh     # Raspberry Pi provisioning and systemd installation
├── requirements.txt    # Python dependency constraints
├── tests/              # Hardware-independent unit tests
└── README.md           # Deployment and operations documentation
```

## Current implementation notes

- The program decodes `ZBarSymbol.QRCODE` only; barcodes and other ZBar symbologies are intentionally ignored.
- Camera capture and QR decoding run concurrently. The decoder examines the freshest full-resolution frame available and stale unprocessed frames are discarded.
- API requests use two persistent-session workers, allowing the next QR to be detected and submitted while another request is still in flight.
- The client sends one HTTP request per newly seen payload and does not inspect the HTTP status itself if the server returned JSON; its visible outcome is selected from the JSON `status` field.
- The scanner logs a short SHA-256 fingerprint instead of raw QR contents or complete API results.
- Buzzer sequences run outside the scanner loop so sound cannot delay badge detection.
- API credentials and scanner ID come from `.env`; camera settings, hardware pins, timing, sound, brightness, and focus remain source configuration.
- Hardware-independent helpers have a standard-library unit test suite. Run it on a development machine with:

  ```bash
  python3 -m unittest discover -s tests -v
  ```

  A safe syntax-only check for the complete hardware runtime is `python3 -m py_compile qr_code_scanner.py scanner_core.py`.

  Running the scanner itself requires Raspberry Pi camera/GPIO dependencies and attached hardware.
