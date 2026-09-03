# Repository Guidelines

## Project Structure & Module Organization

- `qr_code_scanner.py` is the hardware-facing kiosk runtime. It owns camera capture, GPIO, e-paper, API workers, and operator feedback.
- `scanner_core.py` contains hardware-independent QR parsing, payload fingerprinting, retry classification, and the bounded seen-payload cache. Put logic that can run without Raspberry Pi hardware here when practical.
- `tests/test_scanner_core.py` contains unit tests for `scanner_core.py`.
- `scanner_init.sh` provisions a Raspberry Pi, creates `.venv`, installs dependencies, and writes the `qrscanner.service` unit.
- `requirements.txt` lists Python packages installed into the virtual environment. Keep secrets only in the untracked `.env` file.

## Build, Test, and Development Commands

Run the hardware-independent checks from the repository root:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile qr_code_scanner.py scanner_core.py
```

The first command runs the unit suite; the second catches syntax errors without importing Raspberry Pi-specific dependencies. On a provisioned Pi, activate `.venv` and run `python qr_code_scanner.py` for an interactive scanner session. Use `./scanner_init.sh` only for device setup; it installs OS packages, configures SPI, and creates a systemd service.

## Coding Style & Naming Conventions

Use Python with four-space indentation and standard-library-first imports. Prefer `snake_case` for functions, variables, and test methods; use `UPPER_SNAKE_CASE` for module configuration and GPIO constants; use `PascalCase` for classes. Keep runtime behavior explicit: hardware initialization failures should be handled and logged only where continued scanning is intended. Avoid logging raw QR payloads or credentials; use `payload_fingerprint()` for operational identifiers.

## Testing Guidelines

Add focused `unittest` cases in `tests/test_scanner_core.py` for every change to reusable core logic. Name test classes after the unit under test and test methods as `test_<behavior>`, for example `test_expires_entries_after_ttl`. Use subtests for equivalent status variants. Hardware changes should retain the syntax check and be validated on a Pi when hardware is available.

## Commit & Pull Request Guidelines

Follow the existing concise, imperative commit style, such as `Harden scanner reliability and deployment` or `Extend QR result strip flash`. Keep each commit scoped to one behavior. Pull requests should explain the operator-visible effect, configuration or wiring changes, tests run, and any Raspberry Pi validation. Include photos or logs when a display, LED, buzzer, or service behavior changes, and never include `.env` values.
