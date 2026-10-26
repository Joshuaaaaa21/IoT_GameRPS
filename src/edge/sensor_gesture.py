"""Sensor reading (Termux:API + Simulation), feature extraction (RMS + Rotation),
and directional gesture classification with full diagnostic trace for Motion RPS IoT.

Portions adapted from PocketGuard IoT (https://github.com/ruthhhs/iot-projek)
under the MIT License — Copyright (c) 2026 PocketGuard IoT contributors:
- `extract_features` & `Features` (from `shared/processing.py`)
- `discover_termux_sensor_names` (from `AndroidTermux` in `shared/sensors.py`)
- `vibrate_feedback` (from `shared/alarm.py`)
"""

from dataclasses import dataclass, field
import json
import math
import shutil
import subprocess
import time
from typing import Dict, Iterator, List, Optional, Tuple
from src.engine.game_engine import Choice

GRAVITY = 9.81


@dataclass
class SensorSample:
    """Single reading combining accelerometer (m/s^2) and gyroscope (rad/s) 3-axis vectors."""
    ax: float
    ay: float
    az: float
    gx: float = 0.0
    gy: float = 0.0
    gz: float = 0.0
    timestamp: float = 0.0

    @property
    def seconds(self) -> float:
        return self.timestamp

    @property
    def acceleration(self) -> Tuple[float, float, float]:
        return (self.ax, self.ay, self.az)

    @property
    def gyroscope(self) -> Tuple[float, float, float]:
        return (self.gx, self.gy, self.gz)


@dataclass(frozen=True)
class MotionFeatures:
    """
    Summary motion features over a sampling window (adapted from PocketGuard shared/processing.py).
    - motion_rms: Root-Mean-Square of net acceleration magnitude away from gravity (m/s^2).
    - rotation_rad: Integrated total angular movement over the window (rad).
    """
    motion_rms: float
    rotation_rad: float


@dataclass
class GestureDiagnostic:
    """Detailed evaluation trace of a single sensor sample for live debugging & dashboard telemetry."""
    status: str = "WAITING"           # "DETECTED", "REJECTED", or "WAITING"
    reason: str = "BELOW_THRESHOLD"   # Exact reason gesture was accepted or rejected
    detected_choice: Optional[str] = None
    triggered_axis: Optional[str] = None
    best_direction: Optional[str] = None
    best_score: float = 0.0
    threshold: float = 0.0
    total_delta: float = 0.0
    gyro_magnitude: float = 0.0
    deltas: Dict[str, float] = field(default_factory=lambda: {"ax": 0.0, "ay": 0.0, "az": 0.0})
    time_since_last_gesture: float = 999.0


def extract_features(samples: List[SensorSample]) -> MotionFeatures:
    """
    Compute acceleration RMS and integrated angular rotation from >= 2 samples.
    Adapted directly from PocketGuard IoT `shared/processing.py`.
    """
    if len(samples) < 2:
        raise ValueError("At least 2 sensor samples are required")

    acceleration_errors: List[float] = []
    for sample in samples:
        x, y, z = sample.ax, sample.ay, sample.az
        acceleration_magnitude = math.sqrt(x * x + y * y + z * z)
        acceleration_errors.append(acceleration_magnitude - GRAVITY)

    motion_rms = math.sqrt(sum(val ** 2 for val in acceleration_errors) / len(samples))

    rotation_rad = 0.0
    for previous, current in zip(samples, samples[1:]):
        delta_seconds = max(0.0, current.timestamp - previous.timestamp)
        gx, gy, gz = current.gx, current.gy, current.gz
        angular_speed = math.sqrt(gx * gx + gy * gy + gz * gz)
        rotation_rad += angular_speed * delta_seconds

    return MotionFeatures(
        motion_rms=round(motion_rms, 4),
        rotation_rad=round(rotation_rad, 4),
    )


def vibrate_feedback(duration_ms: int = 200) -> bool:
    """
    Trigger tactile haptic feedback on Android via `termux-vibrate` when a gesture locks in.
    Safely no-ops on laptops/PCs where `termux-vibrate` is absent (adapted from `shared/alarm.py`).
    """
    if not shutil.which("termux-vibrate"):
        return False
    try:
        subprocess.run(
            ["termux-vibrate", "-d", str(int(duration_ms))],
            capture_output=True,
            timeout=5,
            check=False,
        )
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


@dataclass
class GestureConfig:
    """
    Configurable axis mapping, thresholds, and debounce settings so different Android
    phones can adjust axis orientation without modifying code.

    Default mapping assumes portrait orientation (screen facing user) or flat on hand:
    - Shake Left  -> dominant spike along X axis (ROCK / BATU)
    - Shake Right -> dominant spike along X axis opposite sign (SCISSORS / GUNTING)
    - Shake Up    -> dominant positive spike along Y or Z axis (PAPER / KERTAS)
    """
    left_axis: str = "ax"
    left_sign: int = -1       # Delta < -threshold on ax => LEFT (ROCK)
    right_axis: str = "ax"
    right_sign: int = 1       # Delta > +threshold on ax => RIGHT (SCISSORS)
    up_axis: str = "ay"
    up_sign: int = 1          # Delta > +threshold on ay => UP (PAPER)
    allow_z_for_up: bool = True  # Also allow positive az spike when phone is held flat and lifted UP

    accel_threshold: float = 5.0      # Net acceleration spike (m/s^2) above baseline (responsive to normal hand swings)
    gyro_min_magnitude: float = 0.8   # Minimum angular velocity magnitude (rad/s) when require_gyro=True
    require_gyro: bool = False        # Gyroscope measures rotation, not linear translation; keep False by default
    settle_threshold: float = 2.5     # Acceleration delta must drop below this to re-arm detector
    debounce_seconds: float = 1.2     # Minimum cooldown between two valid gestures


class GestureClassifier:
    """
    Processes 3-axis accelerometer & gyroscope samples with baseline calibration,
    sliding window feature extraction (RMS + rotation), configurable axis mapping,
    debounce timer, re-arm (settle) guard, and detailed diagnostic trace.
    """

    def __init__(self, config: Optional[GestureConfig] = None, window_size: int = 5) -> None:
        self.config = config or GestureConfig()
        self.baseline: Tuple[float, float, float] = (0.0, 0.0, GRAVITY)
        self.is_calibrated: bool = False
        self._last_trigger_time: float = -999.0
        self._armed: bool = True
        self._window_size = max(2, int(window_size))
        self._recent_samples: List[SensorSample] = []
        self.last_features: MotionFeatures = MotionFeatures(0.0, 0.0)
        self.last_diagnostic: GestureDiagnostic = GestureDiagnostic(threshold=self.config.accel_threshold)

    def calibrate(self, samples: List[SensorSample]) -> Tuple[float, float, float]:
        """Compute static gravity/offset baseline from a list of stationary samples."""
        if not samples:
            raise ValueError("Cannot calibrate with an empty sample list.")
        n = len(samples)
        bx = sum(s.ax for s in samples) / n
        by = sum(s.ay for s in samples) / n
        bz = sum(s.az for s in samples) / n
        self.baseline = (bx, by, bz)
        self.is_calibrated = True
        return self.baseline

    def reset_lock(self) -> None:
        """Reset debounce and re-arm state (useful between rounds)."""
        self._last_trigger_time = -999.0
        self._armed = True
        self._recent_samples.clear()

    def process_sample(self, sample: SensorSample) -> Optional[Choice]:
        """
        Evaluate a single sensor sample and return Choice.ROCK (LEFT),
        Choice.SCISSORS (RIGHT), Choice.PAPER (UP), or None.
        Populates `self.last_features` and `self.last_diagnostic` on every call.
        """
        now = sample.timestamp if sample.timestamp > 0 else time.time()
        normalized_sample = SensorSample(
            ax=sample.ax,
            ay=sample.ay,
            az=sample.az,
            gx=sample.gx,
            gy=sample.gy,
            gz=sample.gz,
            timestamp=now,
        )
        self._recent_samples.append(normalized_sample)
        if len(self._recent_samples) > self._window_size:
            self._recent_samples.pop(0)
        if len(self._recent_samples) >= 2:
            self.last_features = extract_features(self._recent_samples)

        bx, by, bz = self.baseline
        deltas = {
            "ax": round(sample.ax - bx, 4),
            "ay": round(sample.ay - by, 4),
            "az": round(sample.az - bz, 4),
        }

        total_delta = round(math.sqrt(deltas["ax"] ** 2 + deltas["ay"] ** 2 + deltas["az"] ** 2), 4)
        gyro_mag = round(math.sqrt(sample.gx ** 2 + sample.gy ** 2 + sample.gz ** 2), 4)
        since_last = round(now - self._last_trigger_time, 3) if self._last_trigger_time > 0 else 999.0

        # Directional projections based on configured axis & sign
        score_left = deltas[self.config.left_axis] * self.config.left_sign
        score_right = deltas[self.config.right_axis] * self.config.right_sign
        score_up = deltas[self.config.up_axis] * self.config.up_sign
        up_axis_used = f"{'+' if self.config.up_sign > 0 else '-'}{self.config.up_axis}"

        if self.config.allow_z_for_up and self.config.up_axis == "ay":
            z_up = deltas["az"] * self.config.up_sign
            if z_up > score_up:
                score_up = z_up
                up_axis_used = f"{'+' if self.config.up_sign > 0 else '-'}az"

        candidates = [
            (score_left, Choice.ROCK, "LEFT", f"{'+' if self.config.left_sign > 0 else '-'}{self.config.left_axis}"),
            (score_right, Choice.SCISSORS, "RIGHT", f"{'+' if self.config.right_sign > 0 else '-'}{self.config.right_axis}"),
            (score_up, Choice.PAPER, "UP", up_axis_used),
        ]
        best_score, best_choice, best_dir, best_axis = max(candidates, key=lambda item: item[0])
        best_score = round(best_score, 4)

        # 1. Re-arm check: require phone to return near rest before allowing another detection
        if not self._armed:
            if total_delta <= self.config.settle_threshold:
                self._armed = True
            else:
                self.last_diagnostic = GestureDiagnostic(
                    status="REJECTED",
                    reason=f"WAITING_SETTLE (delta={total_delta:.2f} > settle={self.config.settle_threshold})",
                    detected_choice=None,
                    triggered_axis=best_axis,
                    best_direction=best_dir,
                    best_score=best_score,
                    threshold=self.config.accel_threshold,
                    total_delta=total_delta,
                    gyro_magnitude=gyro_mag,
                    deltas=deltas,
                    time_since_last_gesture=since_last,
                )
                return None

        # 2. Cooldown / debounce check
        if (now - self._last_trigger_time) < self.config.debounce_seconds:
            self.last_diagnostic = GestureDiagnostic(
                status="REJECTED",
                reason=f"DEBOUNCE_COOLDOWN ({since_last:.2f}s < {self.config.debounce_seconds}s)",
                detected_choice=None,
                triggered_axis=best_axis,
                best_direction=best_dir,
                best_score=best_score,
                threshold=self.config.accel_threshold,
                total_delta=total_delta,
                gyro_magnitude=gyro_mag,
                deltas=deltas,
                time_since_last_gesture=since_last,
            )
            return None

        # 3. Check acceleration threshold first
        if best_score < self.config.accel_threshold:
            status = "WAITING" if total_delta < 1.5 else "REJECTED"
            reason = (
                f"STATIONARY (peak={best_score:.2f} < thr={self.config.accel_threshold})"
                if status == "WAITING"
                else f"BELOW_ACCEL_THRESHOLD ({best_dir} on {best_axis}={best_score:.2f} < {self.config.accel_threshold})"
            )
            self.last_diagnostic = GestureDiagnostic(
                status=status,
                reason=reason,
                detected_choice=None,
                triggered_axis=best_axis,
                best_direction=best_dir,
                best_score=best_score,
                threshold=self.config.accel_threshold,
                total_delta=total_delta,
                gyro_magnitude=gyro_mag,
                deltas=deltas,
                time_since_last_gesture=since_last,
            )
            return None

        # 4. Optional gyroscope confirmation
        if self.config.require_gyro and gyro_mag < self.config.gyro_min_magnitude:
            self.last_diagnostic = GestureDiagnostic(
                status="REJECTED",
                reason=f"BELOW_GYRO_MIN (|gyro|={gyro_mag:.2f} < {self.config.gyro_min_magnitude})",
                detected_choice=None,
                triggered_axis=best_axis,
                best_direction=best_dir,
                best_score=best_score,
                threshold=self.config.accel_threshold,
                total_delta=total_delta,
                gyro_magnitude=gyro_mag,
                deltas=deltas,
                time_since_last_gesture=since_last,
            )
            return None

        # 5. Gesture Accepted!
        self._last_trigger_time = now
        self._armed = False  # Disarm until back-swing settles below settle_threshold
        self.last_diagnostic = GestureDiagnostic(
            status="DETECTED",
            reason=f"DETECTED_{best_dir}_{best_choice.value} ({best_axis}={best_score:.2f} >= {self.config.accel_threshold})",
            detected_choice=best_choice.value,
            triggered_axis=best_axis,
            best_direction=best_dir,
            best_score=best_score,
            threshold=self.config.accel_threshold,
            total_delta=total_delta,
            gyro_magnitude=gyro_mag,
            deltas=deltas,
            time_since_last_gesture=0.0,
        )
        return best_choice


class TermuxSensorReader:
    """
    Interfaces with Android hardware sensors via `termux-sensor` CLI from Termux:API,
    using automatic calibrated sensor name discovery and stateful chunk merging so
    split Accelerometer/Gyroscope JSON chunks from Termux never lose gyroscope readings.
    """

    @staticmethod
    def is_termux_sensor_available() -> bool:
        """Return True only if `termux-sensor` binary exists in PATH."""
        return shutil.which("termux-sensor") is not None

    @staticmethod
    def discover_sensor_names(sensor_list_output: Optional[str] = None) -> Tuple[str, str]:
        """
        Discover the exact calibrated Accelerometer and Gyroscope sensor names on an Android
        device via `termux-sensor -l` (excluding 'uncalibrated' variants).
        """
        if sensor_list_output is None:
            if not TermuxSensorReader.is_termux_sensor_available():
                raise RuntimeError("termux-sensor is missing. Install Termux:API and run: pkg install termux-api")
            sensor_list_output = subprocess.run(
                ["termux-sensor", "-l"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            ).stdout

        names = json.loads(sensor_list_output)
        if isinstance(names, dict):
            names = names.get("sensors", [])
        if not isinstance(names, list):
            raise RuntimeError("Unexpected sensor listing from termux-sensor -l")

        accel_name = next(
            (
                name for name in names
                if ("accelerometer" in name.lower() or "acceleration sensor" in name.lower())
                and "uncalibrated" not in name.lower()
            ),
            None,
        )
        gyro_name = next(
            (
                name for name in names
                if "gyroscope" in name.lower() and "uncalibrated" not in name.lower()
            ),
            None,
        )
        if not accel_name or not gyro_name:
            raise RuntimeError(
                f"Accelerometer + gyroscope belum terdeteksi. Daftar: {names}. "
                "Pastikan kedua sensor ada di HP dan Termux:API terpasang."
            )
        return accel_name, gyro_name

    @staticmethod
    def parse_termux_json_chunk(
        raw_json: str,
        last_accel: Optional[Tuple[float, float, float]] = None,
        last_gyro: Optional[Tuple[float, float, float]] = None,
    ) -> Tuple[Optional[SensorSample], Tuple[float, float, float], Tuple[float, float, float]]:
        """
        Parse a JSON object emitted by `termux-sensor` and merge with cached `(last_accel, last_gyro)`
        state so split JSON chunks (where Android emits Gyroscope in one JSON block and Accelerometer
        in the next) preserve both sensors accurately.
        Also remains backward-compatible when called as `parse_termux_json_chunk(raw_json)` via wrapper.
        """
        cur_accel = last_accel if last_accel is not None else (0.0, 0.0, 0.0)
        cur_gyro = last_gyro if last_gyro is not None else (0.0, 0.0, 0.0)

        try:
            data = json.loads(raw_json)
            if not isinstance(data, dict):
                return None, cur_accel, cur_gyro
        except json.JSONDecodeError:
            return None, cur_accel, cur_gyro

        found_accel = False
        found_gyro = False

        for key, val in data.items():
            if not isinstance(val, dict):
                continue
            values = val.get("values")
            if not isinstance(values, list) or len(values) < 3:
                continue
            k_lower = key.lower()
            if "accel" in k_lower:
                cur_accel = (float(values[0]), float(values[1]), float(values[2]))
                found_accel = True
            elif "gyro" in k_lower:
                cur_gyro = (float(values[0]), float(values[1]), float(values[2]))
                found_gyro = True

        if not found_accel and not found_gyro:
            return None, cur_accel, cur_gyro

        # Emit a combined sample whenever accelerometer is present (or if we already have cached accel and gyro arrives)
        if found_accel or (found_gyro and last_accel is not None):
            sample = SensorSample(
                ax=cur_accel[0],
                ay=cur_accel[1],
                az=cur_accel[2],
                gx=cur_gyro[0],
                gy=cur_gyro[1],
                gz=cur_gyro[2],
                timestamp=time.monotonic(),
            )
            return sample, cur_accel, cur_gyro

        return None, cur_accel, cur_gyro

    @classmethod
    def parse_single_chunk(cls, raw_json: str) -> Optional[SensorSample]:
        """Convenience helper returning only the parsed SensorSample."""
        sample, _, _ = cls.parse_termux_json_chunk(raw_json)
        return sample

    @classmethod
    def read_single_sample(cls, accel_name: str, gyro_name: str, delay_ms: int = 100) -> SensorSample:
        """Read a single combined sample (`-d 100 -n 1`), adapted from PocketGuard `AndroidTermux.read`."""
        try:
            output = subprocess.run(
                ["termux-sensor", "-s", f"{accel_name},{gyro_name}", "-d", str(delay_ms), "-n", "1"],
                capture_output=True,
                text=True,
                timeout=12,
                check=True,
            ).stdout
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(
                "Termux:API gagal membaca sensor. Uji: termux-sensor -l dan termux-sensor -a -d 100 -n 1"
            ) from exc
        sample, _, _ = cls.parse_termux_json_chunk(output)
        if sample is None:
            raise RuntimeError(f"Cannot parse Termux sensor output: {output[:200]}")
        return sample

    @staticmethod
    def generate_simulated_gesture_stream(
        gesture: str,
        base_time: float = 100.0,
    ) -> List[SensorSample]:
        """
        Generate a sequence of stationary, peak swing, back-swing, and settle samples
        for testing STILL, WEAK, NOISE, LEFT (ROCK), RIGHT (SCISSORS), or UP (PAPER) without a physical phone.
        """
        g = gesture.strip().upper()
        samples = [
            SensorSample(0.0, 0.0, 9.81, 0.0, 0.0, 0.0, base_time + 0.0),
            SensorSample(0.05, -0.05, 9.81, 0.01, -0.01, 0.0, base_time + 0.1),
        ]
        if g == "STILL":
            samples.extend([
                SensorSample(0.02, 0.01, 9.81, 0.0, 0.01, 0.0, base_time + 0.2),
                SensorSample(-0.01, 0.02, 9.80, 0.01, 0.0, 0.0, base_time + 0.3),
                SensorSample(0.0, 0.0, 9.81, 0.0, 0.0, 0.0, base_time + 0.4),
            ])
        elif g in ("WEAK", "NOISE"):
            # Small tremor/movement below threshold (peak ~ 2.2 m/s^2 < 5.0 threshold)
            samples.extend([
                SensorSample(-2.2, 0.8, 9.81, 0.15, 0.25, 0.05, base_time + 0.2),
                SensorSample(1.8, -0.6, 9.81, -0.1, -0.2, 0.0, base_time + 0.3),
                SensorSample(0.1, 0.0, 9.81, 0.0, 0.0, 0.0, base_time + 0.5),
            ])
        elif g in ("LEFT", "ROCK"):
            samples.extend([
                SensorSample(-9.5, 0.4, 9.81, 0.2, 2.1, 0.1, base_time + 0.2),
                SensorSample(-7.8, 0.3, 9.81, 0.15, 1.8, 0.05, base_time + 0.28),  # Multi-sample peak
                SensorSample(7.2, -0.3, 9.81, -0.1, -1.5, 0.0, base_time + 0.4),   # Rebound swing
                SensorSample(0.2, 0.1, 9.81, 0.0, 0.0, 0.0, base_time + 0.65),
            ])
        elif g in ("RIGHT", "SCISSORS"):
            samples.extend([
                SensorSample(9.5, 0.3, 9.81, 0.1, -2.2, 0.1, base_time + 0.2),
                SensorSample(8.1, 0.2, 9.81, 0.1, -1.7, 0.05, base_time + 0.28),
                SensorSample(-7.0, -0.2, 9.81, 0.1, 1.4, 0.0, base_time + 0.4),
                SensorSample(0.1, 0.0, 9.81, 0.0, 0.0, 0.0, base_time + 0.65),
            ])
        elif g in ("UP", "PAPER"):
            samples.extend([
                SensorSample(0.3, 9.8, 9.81, 2.3, 0.2, 0.1, base_time + 0.2),
                SensorSample(0.2, 8.4, 9.81, 1.9, 0.1, 0.05, base_time + 0.28),
                SensorSample(-0.2, -6.8, 9.81, -1.5, 0.1, 0.0, base_time + 0.4),
                SensorSample(0.0, 0.1, 9.81, 0.0, 0.0, 0.0, base_time + 0.65),
            ])
        else:
            raise ValueError(f"Unsupported simulated gesture '{gesture}'.")
        return samples

    @classmethod
    def stream_termux_sensors(
        cls,
        sensor_names: Optional[str] = None,
        delay_ms: int = 100,
        limit: Optional[int] = None,
    ) -> Iterator[SensorSample]:
        """
        Stream live sensor samples from Termux:API.
        Initially attempts continuous streaming (`termux-sensor -s ... -d 100`), and if a device
        fails to flush stdout or stalls, falls back cleanly to single-shot polling (`-n 1`).
        """
        if not cls.is_termux_sensor_available():
            raise RuntimeError(
                "termux-sensor is not available on this device/OS. "
                "Run on Android Termux with Termux:API installed, or use --simulate mode."
            )
        if not sensor_names:
            accel_name, gyro_name = cls.discover_sensor_names()
            sensor_names = f"{accel_name},{gyro_name}"
        else:
            parts = [p.strip() for p in sensor_names.split(",") if p.strip()]
            accel_name = parts[0]
            gyro_name = parts[1] if len(parts) > 1 else parts[0]

        # If limit is small (e.g. calibration), use reliable single-shot reads like PocketGuard reference
        if limit is not None and limit <= 15:
            for _ in range(limit):
                yield cls.read_single_sample(accel_name, gyro_name, delay_ms=delay_ms)
            return

        cmd = ["termux-sensor", "-s", sensor_names, "-d", str(delay_ms)]
        if limit is not None:
            cmd.extend(["-n", str(limit)])

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        buffer: List[str] = []
        brace_depth = 0
        last_accel: Optional[Tuple[float, float, float]] = None
        last_gyro: Optional[Tuple[float, float, float]] = None
        emitted = 0
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                stripped = line.strip()
                if not stripped:
                    continue
                brace_depth += stripped.count("{") - stripped.count("}")
                buffer.append(stripped)
                if brace_depth == 0 and buffer:
                    chunk = "\n".join(buffer)
                    buffer.clear()
                    sample, last_accel, last_gyro = cls.parse_termux_json_chunk(
                        chunk, last_accel=last_accel, last_gyro=last_gyro
                    )
                    if sample is not None:
                        emitted += 1
                        yield sample
                        if limit is not None and emitted >= limit:
                            break
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                proc.kill()
