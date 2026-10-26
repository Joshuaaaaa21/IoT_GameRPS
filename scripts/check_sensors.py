"""Sensor-only diagnostic for Motion RPS IoT. Does NOT require an MQTT broker or .env.

Adapted from PocketGuard IoT (`scripts/check_sensors.py`, https://github.com/ruthhhs/iot-projek)
under the MIT License — Copyright (c) 2026 PocketGuard IoT contributors.

Run this on Android Termux:
    python -m scripts.check_sensors --readings 12
On a laptop (simulation check):
    python -m scripts.check_sensors --simulate --gesture LEFT
"""

import argparse
import math
import time
from typing import List

from src.edge.sensor_gesture import (
    GestureClassifier,
    SensorSample,
    TermuxSensorReader,
    extract_features,
)


def _print_summary_stats(samples: List[SensorSample]) -> None:
    if not samples:
        return
    axes = {
        "ax (m/s²)": [s.ax for s in samples],
        "ay (m/s²)": [s.ay for s in samples],
        "az (m/s²)": [s.az for s in samples],
        "gx (rad/s)": [s.gx for s in samples],
        "gy (rad/s)": [s.gy for s in samples],
        "gz (rad/s)": [s.gz for s in samples],
    }
    intervals = [
        max(0.0, cur.timestamp - prev.timestamp)
        for prev, cur in zip(samples, samples[1:])
    ]
    avg_dt_ms = (sum(intervals) / len(intervals) * 1000.0) if intervals else 0.0

    print("\n=== RINGKASAN STATISTIK SENSOR (MIN / MAX / RENTANG) ===", flush=True)
    print(f"Jumlah Sampel       : {len(samples)}")
    print(f"Interval Rata-rata  : {avg_dt_ms:.1f} ms")
    for label, vals in axes.items():
        vmin = min(vals)
        vmax = max(vals)
        vrange = vmax - vmin
        print(f"  {label:<12} -> min={vmin:7.3f} | max={vmax:7.3f} | range={vrange:7.3f}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Android motion sensors and RPS gesture classification")
    parser.add_argument("--readings", type=int, default=12, help="Number of live readings to collect")
    parser.add_argument("--simulate", action="store_true", help="Run using simulated gesture stream (for laptop test)")
    parser.add_argument(
        "--gesture",
        choices=["STILL", "WEAK", "LEFT", "RIGHT", "UP"],
        default="LEFT",
        help="Simulated gesture direction",
    )
    args = parser.parse_args()
    if args.readings < 2:
        parser.error("--readings must be >= 2")

    clf = GestureClassifier()

    if args.simulate or not TermuxSensorReader.is_termux_sensor_available():
        print("[CHECK] Mode: SIMULATION (tidak membutuhkan sensor fisik Android).", flush=True)
        clf.calibrate([SensorSample(0.0, 0.0, 9.81, timestamp=1.0)])
        samples = TermuxSensorReader.generate_simulated_gesture_stream(args.gesture, base_time=10.0)
        prev_t = samples[0].timestamp
        for idx, reading in enumerate(samples):
            dt_ms = (reading.timestamp - prev_t) * 1000.0 if idx > 0 else 0.0
            prev_t = reading.timestamp
            choice = clf.process_sample(reading)
            diag = clf.last_diagnostic
            print(
                f"[SIM] #{idx + 1}/{len(samples)} t={reading.timestamp:.2f}s (dt={dt_ms:.0f}ms) "
                f"| accel={tuple(round(v, 3) for v in reading.acceleration)} m/s² "
                f"| gyro={tuple(round(v, 3) for v in reading.gyroscope)} rad/s "
                f"| status={diag.status} | reason={diag.reason} "
                f"| choice={choice.value if choice else '-'}",
                flush=True,
            )
        _print_summary_stats(samples)
        features = extract_features(samples)
        print(
            f"[PROCESSING] motion_rms={features.motion_rms:.4f} m/s² "
            f"rotation_rad={features.rotation_rad:.4f} rad",
            flush=True,
        )
        return 0

    print("[CHECK] Memulai diagnostik sensor Termux:API; TIDAK memerlukan MQTT.", flush=True)
    accel_name, gyro_name = TermuxSensorReader.discover_sensor_names()
    print(f"[SENSOR] Accelerometer : {accel_name}")
    print(f"[SENSOR] Gyroscope     : {gyro_name}")

    samples: List[SensorSample] = []
    prev_t = 0.0
    for number in range(args.readings):
        reading = TermuxSensorReader.read_single_sample(accel_name, gyro_name, delay_ms=100)
        if number == 0:
            clf.calibrate([reading])
            prev_t = reading.timestamp
        dt_ms = (reading.timestamp - prev_t) * 1000.0 if number > 0 else 0.0
        prev_t = reading.timestamp
        samples.append(reading)
        choice = clf.process_sample(reading)
        diag = clf.last_diagnostic
        magnitude = math.sqrt(sum(axis * axis for axis in reading.acceleration))
        angular_speed = math.sqrt(sum(axis * axis for axis in reading.gyroscope))
        print(
            f"[CHECK] #{number + 1}/{args.readings} dt={dt_ms:.0f}ms "
            f"| accel={tuple(round(v, 3) for v in reading.acceleration)} m/s² "
            f"| gyro={tuple(round(v, 3) for v in reading.gyroscope)} rad/s "
            f"| |a|={magnitude:.2f} |w|={angular_speed:.2f} "
            f"| status={diag.status} ({diag.reason}) "
            f"| gesture={choice.value if choice else '-'}",
            flush=True,
        )
        if number < args.readings - 1:
            time.sleep(0.05)

    _print_summary_stats(samples)
    features = extract_features(samples)
    print(
        f"[PROCESSING] motion_rms={features.motion_rms:.4f} m/s² "
        f"rotation_rad={features.rotation_rad:.4f} rad",
        flush=True,
    )
    print("[CHECK] Jika rentang (range) berubah saat HP digerakkan, jalur sensor fisik berfungsi.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
