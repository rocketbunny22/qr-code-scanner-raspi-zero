#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/home/viztech/qr-code-scanner-raspi-zero/"
PROJECT_USER="${SUDO_USER:-$USER}"
PROJECT_HOME="$(getent passwd "$PROJECT_USER" | cut -d: -f6)"

VENV_DIR="$PROJECT_DIR/.venv"
SCANNER_SCRIPT="$PROJECT_DIR/qr_code_scanner.py"
SERVICE_FILE="/etc/systemd/system/qrscanner.service"

EPAPER_DIR="$PROJECT_HOME/e-Paper"
EPAPER_LIB="$EPAPER_DIR/RaspberryPi_JetsonNano/python/lib"

echo "Project: $PROJECT_DIR"
echo "User: $PROJECT_USER"

if [[ ! -f "$SCANNER_SCRIPT" ]]; then
    echo "ERROR: qr_code_scanner.py not found in $PROJECT_DIR"
    exit 1
fi

echo "Installing packages..."
sudo apt update
sudo apt install -y \
    git \
    python3 \
    python3-venv \
    python3-pip \
    python3-picamera2 \
    python3-opencv \
    python3-pil \
    python3-numpy \
    python3-gpiozero \
    python3-lgpio \
    libzbar0 \
    fonts-dejavu-core \
    rpicam-apps

echo "Enabling SPI..."
sudo raspi-config nonint do_spi 0 || true
echo "Enabling SSH..."
sudo systemctl enable ssh
sudo systemctl start ssh

echo "Installing Waveshare e-Paper library..."
if [[ ! -d "$EPAPER_DIR" ]]; then
    git clone https://github.com/waveshareteam/e-Paper.git "$EPAPER_DIR"
else
    git -C "$EPAPER_DIR" pull --ff-only || true
fi

sudo chown -R "$PROJECT_USER:$PROJECT_USER" "$EPAPER_DIR"

if [[ ! -d "$EPAPER_LIB/waveshare_epd" ]]; then
    echo "ERROR: waveshare_epd not found at $EPAPER_LIB/waveshare_epd"
    exit 1
fi

echo "Creating venv..."
python3 -m venv --system-site-packages "$VENV_DIR"

echo "Installing Python packages..."
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install \
    requests \
    python-dotenv \
    pyzbar

if [[ ! -f "$PROJECT_DIR/.env" ]]; then
    echo "Creating .env..."
    read -r -p "OFG_URL: " OFG_URL
    read -r -s -p "OFG_API_KEY: " OFG_API_KEY
    echo

    cat > "$PROJECT_DIR/.env" <<EOF
OFG_URL=$OFG_URL
OFG_API_KEY=$OFG_API_KEY
EOF

    chmod 600 "$PROJECT_DIR/.env"
    chown "$PROJECT_USER:$PROJECT_USER" "$PROJECT_DIR/.env"
else
    echo ".env already exists; leaving unchanged."
fi

echo "Creating systemd service..."
sudo tee "$SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=OFG QR Code Scanner
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$PROJECT_USER
WorkingDirectory=$PROJECT_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$VENV_DIR/bin/python -u $SCANNER_SCRIPT
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable qrscanner.service

echo "Verifying Python imports..."
"$VENV_DIR/bin/python" - <<EOF
import sys
sys.path.insert(0, "$EPAPER_LIB")

import requests
import cv2
from PIL import Image
from pyzbar.pyzbar import decode
from gpiozero import LED, PWMOutputDevice
from picamera2 import Picamera2
from waveshare_epd import epd2in13_V4

print("Import check OK")
EOF

echo
echo "Setup complete."
echo
echo "Recommended next commands:"
echo "  sudo reboot"
echo
echo "After reboot:"
echo "  cd $PROJECT_DIR"
echo "  source .venv/bin/activate"
echo "  python qr_code_scanner.py"
echo
echo "Start as service:"
echo "  sudo systemctl start qrscanner.service"
echo
echo "View logs:"
echo "  sudo journalctl -u qrscanner.service -f"