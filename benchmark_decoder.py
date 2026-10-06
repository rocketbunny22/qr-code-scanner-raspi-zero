"""Compare decoders on local .npy uint8 grayscale captures without logging QR data."""

import argparse
import json
import math
from pathlib import Path
import statistics
import time

from scanner_decode import QrDecoder, create_decoder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--backends", nargs="+", choices=("pyzbar", "zxingcpp"),
                        default=["pyzbar", "zxingcpp"])
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--crop-size", type=int, default=0)
    parser.add_argument("--full-frame-interval", type=int, default=5)
    args = parser.parse_args()
    if args.repeats < 1 or args.crop_size < 0 or args.full_frame_interval < 1:
        parser.error("repeats and full-frame-interval must be positive; crop-size must be nonnegative")

    import numpy as np

    paths = sorted(args.directory.glob("*.npy"))
    if not paths:
        parser.error("directory contains no .npy frames")
    # Load identical inputs outside timed sections. Pickle is deliberately disabled.
    frames = [np.load(path, allow_pickle=False) for path in paths]
    if any(frame.ndim != 2 or frame.dtype != np.uint8 or not frame.size for frame in frames):
        parser.error("every frame must be a nonempty two-dimensional uint8 grayscale array")

    report = {"frames": len(frames), "repeats": args.repeats,
              "crop_size": args.crop_size, "full_frame_interval": args.full_frame_interval,
              "backends": {}}
    results = {}
    for backend in dict.fromkeys(args.backends):
        try:
            adapter = create_decoder(backend)
        except (ImportError, OSError):
            report["backends"][backend] = {"available": False}
            continue
        adapter(frames[0])  # Warm up outside the measured iterations.
        timings = []
        reads = []
        for _ in range(args.repeats):
            decoder = QrDecoder(adapter, args.crop_size, args.full_frame_interval)
            for frame in frames:
                started = time.perf_counter()
                payloads = decoder.decode(frame)
                timings.append((time.perf_counter() - started) * 1000)
                reads.append(payloads)
        results[backend] = reads
        report["backends"][backend] = {
            "available": True,
            "median_ms": round(statistics.median(timings), 3),
            "p95_ms": round(sorted(timings)[math.ceil(len(timings) * 0.95) - 1], 3),
            "frames_with_reads": sum(bool(read) for read in reads),
            "payload_reads": sum(len(read) for read in reads),
            "measured_frames": len(reads),
        }
    if len(results) == 2:
        first, second = results.values()
        report["agreement"] = {
            "identical_payload_sets": sum(a == b for a, b in zip(first, second)),
            "compared_frames": len(first),
            "note": "Agreement is not accuracy; no labelled ground truth is supplied.",
        }
    print(json.dumps(report, indent=2))
    return 0 if results else 1


if __name__ == "__main__":
    raise SystemExit(main())
