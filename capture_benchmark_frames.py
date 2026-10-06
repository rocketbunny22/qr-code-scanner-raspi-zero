"""Capture a finite private set of grayscale frames for decoder benchmarking."""

import argparse
import os
from pathlib import Path

from scanner_config import ScannerSettings


def save_frames(camera, settings, directory, count, mapped_array_factory, numpy):
    """Release each camera buffer before writing its copied luma plane."""
    for index in range(count):
        request = camera.capture_request()
        try:
            with mapped_array_factory(request, "main") as mapped:
                gray = mapped.array[:settings.height, :settings.width].copy(order="C")
            if gray.shape != (settings.height, settings.width):
                raise RuntimeError("Camera returned an unexpected grayscale shape")
        finally:
            request.release()
        path = directory / f"frame-{index:05d}.npy"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            numpy.save(output, gray, allow_pickle=False)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Stop qrscanner.service before capturing. Frames may contain private badge data.",
    )
    parser.add_argument("directory", type=Path, help="new output directory (must not already exist)")
    parser.add_argument("--frames", type=int, default=60)
    args = parser.parse_args()
    if args.frames < 1:
        parser.error("--frames must be positive")

    from dotenv import dotenv_values
    import numpy as np
    from picamera2 import MappedArray, Picamera2
    from scanner_camera import configure_camera

    env = {
        key: value
        for key, value in dotenv_values(Path(__file__).resolve().parent / ".env").items()
        if key.startswith("SCANNER_") and value is not None
    }
    env.update({key: value for key, value in os.environ.items() if key.startswith("SCANNER_")})
    settings = ScannerSettings.from_env(env)
    args.directory.mkdir(mode=0o700)
    camera = Picamera2()
    started = False
    try:
        configure_camera(camera, settings)
        camera.start()
        started = True
        save_frames(camera, settings, args.directory, args.frames, MappedArray, np)
    finally:
        try:
            if started:
                camera.stop()
        finally:
            camera.close()
    print(f"Saved {args.frames} frames to {args.directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
