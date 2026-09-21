#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/"
PROJECT_USER="${SUDO_USER:-$USER}"
ENABLE_SSH="${ENABLE_SSH:-0}"

VENV_DIR="$PROJECT_DIR/.venv"
SCANNER_SCRIPT="$PROJECT_DIR/qr_code_scanner.py"
SERVICE_FILE="/etc/systemd/system/qrscanner.service"

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
    python3-numpy \
    python3-gpiozero \
    python3-lgpio \
    libzbar0 \
    rpicam-apps

if [[ "$ENABLE_SSH" == "1" ]]; then
    echo "Enabling SSH..."
    sudo systemctl enable ssh
    sudo systemctl start ssh
else
    echo "Leaving SSH unchanged. Set ENABLE_SSH=1 to enable it."
fi

echo "Creating venv..."
python3 -m venv --system-site-packages "$VENV_DIR"

echo "Installing Python packages..."
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r "$PROJECT_DIR/requirements.txt"

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
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable qrscanner.service

echo "Verifying Python imports..."
"$VENV_DIR/bin/python" - <<EOF
import requests
from pyzbar.pyzbar import decode
from gpiozero import LED, PWMOutputDevice
from picamera2 import Picamera2
from rpi_ws281x import PixelStrip, Color

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
