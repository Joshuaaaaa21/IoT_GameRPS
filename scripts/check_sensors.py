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

from src.edge.sensor_gesture import (
    GestureClassifier,
    SensorSample,
    TermuxSensorReader,
    extract_features,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Android motion sensors and RPS gesture classification")
    parser.add_argument("--readings", type=int, default=12, help="Number of live readings to collect")
    parser.add_argument("--simulate", action="store_true", help="Run using simulated gesture stream (for laptop test)")
    parser.add_argument("--gesture", choices=["LEFT", "RIGHT", "UP"], default="LEFT", help="Simulated gesture direction")
    args = parser.parse_args()
    if args.readings < 2:
        parser.error("--readings must be >= 2")

    clf = GestureClassifier()

    if args.simulate or not TermuxSensorReader.is_termux_sensor_available():
        print("[CHECK] Running in SIMULATION mode (no physical Android sensor required).", flush=True)
        clf.calibrate([SensorSample(0.0, 0.0, 9.81, timestamp=1.0)])
        samples = TermuxSensorReader.generate_simulated_gesture_stream(args.gesture, base_time=10.0)
        for idx, reading in enumerate(samples):
            choice = clf.process_sample(reading)
            print(
                f"[SIM] {idx + 1}/{len(samples)} "
                f"accel={tuple(round(v, 3) for v in reading.acceleration)} m/s² "
                f"|gyro={tuple(round(v, 3) for v in reading.gyroscope)} rad/s "
                f"|detected={choice.value if choice else '-'}",
                flush=True,
            )
        features = extract_features(samples)
        print(
            f"[PROCESSING] motion_rms={features.motion_rms:.4f} m/s² "
            f"rotation_rad={features.rotation_rad:.4f} rad"
        )
        return 0

    print("[CHECK] Starting Termux:API sensor test; NO MQTT required.", flush=True)
    accel_name, gyro_name = TermuxSensorReader.discover_sensor_names()
    print(f"[SENSOR] Accelerometer: {accel_name}")
    print(f"[SENSOR] Gyroscope: {gyro_name}")

    samples = []
    for number in range(args.readings):
        reading = TermuxSensorReader.read_single_sample(accel_name, gyro_name, delay_ms=100)
        if number == 0:
            clf.calibrate([reading])
        samples.append(reading)
        choice = clf.process_sample(reading)
        magnitude = math.sqrt(sum(axis * axis for axis in reading.acceleration))
        angular_speed = math.sqrt(sum(axis * axis for axis in reading.gyroscope))
        print(
            f"[CHECK] {number + 1}/{args.readings} "
            f"accel={tuple(round(v, 3) for v in reading.acceleration)} m/s² "
            f"|gyro={tuple(round(v, 3) for v in reading.gyroscope)} rad/s "
            f"|a_magnitude={magnitude:.3f} |angular_speed={angular_speed:.3f} "
            f"|gesture={choice.value if choice else '-'}",
            flush=True,
        )
        if number < args.readings - 1:
            time.sleep(0.1)

    features = extract_features(samples)
    print(
        f"[PROCESSING] motion_rms={features.motion_rms:.4f} m/s² "
        f"rotation_rad={features.rotation_rad:.4f} rad"
    )
    print("[CHECK] If values reacted to motion, the Android sensor path works.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
