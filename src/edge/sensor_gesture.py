"""Sensor reading (Termux:API + Simulation), feature extraction (RMS + Rotation),
and directional gesture classification for Motion RPS IoT.

Portions adapted from PocketGuard IoT (https://github.com/ruthhhs/iot-projek)
under the MIT License — Copyright (c) 2026 PocketGuard IoT contributors:
- `extract_features` & `Features` (from `shared/processing.py`)
- `discover_termux_sensor_names` (from `AndroidTermux` in `shared/sensors.py`)
- `vibrate_feedback` (from `shared/alarm.py`)
"""

from dataclasses import dataclass
import json
import math
import shutil
import subprocess
import time
from typing import Iterator, List, Optional, Tuple
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

    Default mapping assumes portrait orientation (screen facing user):
    - Shake Left  -> dominant spike along X axis (negative or positive depending on phone)
    - Shake Right -> dominant spike along X axis (opposite sign of Left)
    - Shake Up    -> dominant positive spike along Y (or Z) axis after baseline subtraction
    """
    left_axis: str = "ax"
    left_sign: int = -1       # Delta < -threshold on ax => LEFT (ROCK)
    right_axis: str = "ax"
    right_sign: int = 1       # Delta > +threshold on ax => RIGHT (SCISSORS)
    up_axis: str = "ay"
    up_sign: int = 1          # Delta > +threshold on ay => UP (PAPER)

    accel_threshold: float = 6.5      # Minimum net acceleration spike (m/s^2) above baseline
    gyro_min_magnitude: float = 0.8   # Minimum angular velocity magnitude (rad/s) to confirm deliberate motion
    require_gyro: bool = False        # If True, requires gyroscope magnitude >= gyro_min_magnitude
    settle_threshold: float = 2.5     # Acceleration delta must drop below this to re-arm detector
    debounce_seconds: float = 1.2     # Minimum cooldown between two valid gestures


class GestureClassifier:
    """
    Processes 3-axis accelerometer & gyroscope samples with baseline calibration,
    sliding window feature extraction (RMS + rotation), configurable axis mapping,
    debounce timer, and re-arm (settle) guard so a single swing never triggers multiple choices.
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
        Also updates `self.last_features` (`motion_rms` and `rotation_rad`).
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
            "ax": sample.ax - bx,
            "ay": sample.ay - by,
            "az": sample.az - bz,
        }

        total_delta = math.sqrt(deltas["ax"] ** 2 + deltas["ay"] ** 2 + deltas["az"] ** 2)

        # Re-arm check: require phone to return near rest before allowing another detection
        if not self._armed:
            if total_delta <= self.config.settle_threshold:
                self._armed = True
            else:
                return None

        # Cooldown / debounce check
        if (now - self._last_trigger_time) < self.config.debounce_seconds:
            return None

        # Optional gyroscope confirmation
        if self.config.require_gyro:
            gyro_mag = math.sqrt(sample.gx ** 2 + sample.gy ** 2 + sample.gz ** 2)
            if gyro_mag < self.config.gyro_min_magnitude:
                return None

        # Directional projections based on configured axis & sign
        score_left = deltas[self.config.left_axis] * self.config.left_sign
        score_right = deltas[self.config.right_axis] * self.config.right_sign
        score_up = deltas[self.config.up_axis] * self.config.up_sign

        candidates = [
            (score_left, Choice.ROCK),       # Goyang kiri = ROCK
            (score_right, Choice.SCISSORS),  # Goyang kanan = SCISSORS
            (score_up, Choice.PAPER),        # Goyang atas = PAPER
        ]
        best_score, best_choice = max(candidates, key=lambda item: item[0])

        if best_score >= self.config.accel_threshold:
            self._last_trigger_time = now
            self._armed = False  # Disarm until back-swing settles below settle_threshold
            return best_choice

        return None


class TermuxSensorReader:
    """
    Interfaces with Android hardware sensors via `termux-sensor` CLI from Termux:API,
    using automatic calibrated sensor name discovery adapted from PocketGuard `shared/sensors.py`.
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
        Adapted from `AndroidTermux.__init__` in PocketGuard `shared/sensors.py`.
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
    def parse_termux_json_chunk(raw_json: str) -> Optional[SensorSample]:
        """
        Parse a JSON object emitted by `termux-sensor` into a SensorSample.
        Handles varying vendor sensor keys (e.g. 'Accelerometer', 'ICM20600 Accelerometer').
        """
        try:
            data = json.loads(raw_json)
            if not isinstance(data, dict):
                return None
        except json.JSONDecodeError:
            return None

        ax = ay = az = 0.0
        gx = gy = gz = 0.0
        found_accel = False

        for key, val in data.items():
            if not isinstance(val, dict):
                continue
            values = val.get("values")
            if not isinstance(values, list) or len(values) < 3:
                continue
            k_lower = key.lower()
            if "accel" in k_lower:
                ax, ay, az = float(values[0]), float(values[1]), float(values[2])
                found_accel = True
            elif "gyro" in k_lower:
                gx, gy, gz = float(values[0]), float(values[1]), float(values[2])

        if not found_accel:
            return None
        return SensorSample(ax=ax, ay=ay, az=az, gx=gx, gy=gy, gz=gz, timestamp=time.monotonic())

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
        sample = cls.parse_termux_json_chunk(output)
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
        for testing LEFT (ROCK), RIGHT (SCISSORS), or UP (PAPER) without a physical phone.
        """
        g = gesture.strip().upper()
        samples = [
            SensorSample(0.0, 0.0, 9.8, 0.0, 0.0, 0.0, base_time + 0.0),
            SensorSample(0.1, -0.1, 9.8, 0.0, 0.0, 0.0, base_time + 0.1),
        ]
        if g in ("LEFT", "ROCK"):
            samples.extend([
                SensorSample(-9.5, 0.4, 9.8, 0.2, 2.1, 0.1, base_time + 0.2),
                SensorSample(7.2, -0.3, 9.8, -0.1, -1.5, 0.0, base_time + 0.35),
                SensorSample(0.2, 0.1, 9.8, 0.0, 0.0, 0.0, base_time + 0.6),
            ])
        elif g in ("RIGHT", "SCISSORS"):
            samples.extend([
                SensorSample(9.5, 0.3, 9.8, 0.1, -2.2, 0.1, base_time + 0.2),
                SensorSample(-7.0, -0.2, 9.8, 0.1, 1.4, 0.0, base_time + 0.35),
                SensorSample(0.1, 0.0, 9.8, 0.0, 0.0, 0.0, base_time + 0.6),
            ])
        elif g in ("UP", "PAPER"):
            samples.extend([
                SensorSample(0.3, 9.8, 9.8, 2.3, 0.2, 0.1, base_time + 0.2),
                SensorSample(-0.2, -6.8, 9.8, -1.5, 0.1, 0.0, base_time + 0.35),
                SensorSample(0.0, 0.1, 9.8, 0.0, 0.0, 0.0, base_time + 0.6),
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
        """Stream live sensor samples from Termux:API `termux-sensor` command."""
        if not cls.is_termux_sensor_available():
            raise RuntimeError(
                "termux-sensor is not available on this device/OS. "
                "Run on Android Termux with Termux:API installed, or use --simulate mode."
            )
        if not sensor_names:
            accel_name, gyro_name = cls.discover_sensor_names()
            sensor_names = f"{accel_name},{gyro_name}"

        cmd = ["termux-sensor", "-s", sensor_names, "-d", str(delay_ms)]
        if limit is not None:
            cmd.extend(["-n", str(limit)])

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        buffer: List[str] = []
        brace_depth = 0
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
                    sample = cls.parse_termux_json_chunk(chunk)
                    if sample is not None:
                        yield sample
        finally:
            proc.terminate()
