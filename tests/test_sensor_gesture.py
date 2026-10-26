"""Unit tests for Android Termux sensor parsing, RMS/rotation processing, and gesture classification."""

import json
import unittest
from unittest import mock
from src.edge.sensor_gesture import (
    GestureClassifier,
    GestureConfig,
    SensorSample,
    TermuxSensorReader,
    extract_features,
    vibrate_feedback,
)
from src.engine.game_engine import Choice


class TestSensorGesture(unittest.TestCase):
    def setUp(self) -> None:
        self.config = GestureConfig(
            accel_threshold=5.0,
            settle_threshold=2.5,
            debounce_seconds=1.2,
            require_gyro=False,
        )
        self.classifier = GestureClassifier(config=self.config)
        stationary = [
            SensorSample(0.0, 0.0, 9.81, 0.0, 0.0, 0.0, timestamp=1.0),
            SensorSample(0.0, 0.0, 9.81, 0.0, 0.0, 0.0, timestamp=1.1),
        ]
        self.classifier.calibrate(stationary)

    def test_calibration_computes_average_baseline(self) -> None:
        bx, by, bz = self.classifier.baseline
        self.assertAlmostEqual(bx, 0.0)
        self.assertAlmostEqual(by, 0.0)
        self.assertAlmostEqual(bz, 9.81)
        self.assertTrue(self.classifier.is_calibrated)

    def test_extract_features_rms_and_rotation_adapted_from_reference(self) -> None:
        still_samples = [
            SensorSample(0.0, 0.0, 9.81, 0.0, 0.0, 0.0, timestamp=i * 0.5)
            for i in range(5)
        ]
        still_res = extract_features(still_samples)
        self.assertAlmostEqual(still_res.motion_rms, 0.0, places=3)
        self.assertAlmostEqual(still_res.rotation_rad, 0.0, places=3)

        moving_samples = [
            SensorSample(0.0, 0.0, 12.0, 0.5, 0.0, 0.0, timestamp=i * 0.5)
            for i in range(5)
        ]
        move_res = extract_features(moving_samples)
        self.assertGreater(move_res.motion_rms, 2.0)
        self.assertGreater(move_res.rotation_rad, 0.9)

        with self.assertRaises(ValueError):
            extract_features([SensorSample(0.0, 0.0, 9.81, timestamp=0.0)])

    def test_discover_sensor_names_excludes_uncalibrated(self) -> None:
        fake_listing = json.dumps({
            "sensors": [
                "icm42605 Accelerometer Uncalibrated",
                "icm42605 Accelerometer",
                "icm42605 Gyroscope Uncalibrated",
                "icm42605 Gyroscope",
            ]
        })
        accel, gyro = TermuxSensorReader.discover_sensor_names(fake_listing)
        self.assertEqual(accel, "icm42605 Accelerometer")
        self.assertEqual(gyro, "icm42605 Gyroscope")

    def test_vibrate_feedback_mock(self) -> None:
        with mock.patch("src.edge.sensor_gesture.shutil.which", return_value=None):
            self.assertFalse(vibrate_feedback(200))
        with mock.patch("src.edge.sensor_gesture.shutil.which", return_value="/data/data/com.termux/files/usr/bin/termux-vibrate"), \
             mock.patch("src.edge.sensor_gesture.subprocess.run") as run_mock:
            self.assertTrue(vibrate_feedback(200))
            run_mock.assert_called_once()

    def test_still_and_weak_motion_rejected_with_diagnostic_reason(self) -> None:
        # 1. Stationary stream -> WAITING / STATIONARY
        for s in TermuxSensorReader.generate_simulated_gesture_stream("STILL", base_time=10.0):
            res = self.classifier.process_sample(s)
            self.assertIsNone(res)
        self.assertEqual(self.classifier.last_diagnostic.status, "WAITING")
        self.assertIn("STATIONARY", self.classifier.last_diagnostic.reason)

        # 2. Weak motion below threshold -> REJECTED / BELOW_ACCEL_THRESHOLD
        rejected_seen = False
        for s in TermuxSensorReader.generate_simulated_gesture_stream("WEAK", base_time=20.0):
            res = self.classifier.process_sample(s)
            self.assertIsNone(res)
            if self.classifier.last_diagnostic.status == "REJECTED":
                rejected_seen = True
                self.assertIn("BELOW_ACCEL_THRESHOLD", self.classifier.last_diagnostic.reason)
        self.assertTrue(rejected_seen)

    def test_left_right_up_classification_and_multi_sample_rebound_protection(self) -> None:
        cases = [
            ("LEFT", Choice.ROCK, "-ax"),
            ("RIGHT", Choice.SCISSORS, "+ax"),
            ("UP", Choice.PAPER, "+ay"),
        ]
        for idx, (gesture_name, expected_choice, expected_axis) in enumerate(cases):
            with self.subTest(gesture=gesture_name):
                self.classifier.reset_lock()
                stream = TermuxSensorReader.generate_simulated_gesture_stream(
                    gesture_name, base_time=10.0 + idx * 10.0
                )
                detected = []
                detected_axis = None
                for sample in stream:
                    res = self.classifier.process_sample(sample)
                    if res is not None:
                        detected.append(res)
                        detected_axis = self.classifier.last_diagnostic.triggered_axis
                # Multi-sample peak + rebound must emit EXACTLY ONE choice
                self.assertEqual(detected, [expected_choice])
                self.assertEqual(detected_axis, expected_axis)
                self.assertGreater(self.classifier.last_features.motion_rms, 1.0)

    def test_debounce_blocks_rapid_double_gestures_and_allows_sequential_after_cooldown(self) -> None:
        s1 = SensorSample(-9.0, 0.0, 9.81, 0.0, 2.0, 0.0, timestamp=10.0)
        self.assertEqual(self.classifier.process_sample(s1), Choice.ROCK)
        self.assertEqual(self.classifier.last_diagnostic.status, "DETECTED")

        s_settle = SensorSample(0.0, 0.0, 9.81, 0.0, 0.0, 0.0, timestamp=10.3)
        self.assertIsNone(self.classifier.process_sample(s_settle))

        # Within 1.2s debounce window -> rejected with DEBOUNCE_COOLDOWN
        s2 = SensorSample(9.0, 0.0, 9.81, 0.0, 2.0, 0.0, timestamp=10.6)
        self.assertIsNone(self.classifier.process_sample(s2))
        self.assertEqual(self.classifier.last_diagnostic.status, "REJECTED")
        self.assertIn("DEBOUNCE_COOLDOWN", self.classifier.last_diagnostic.reason)

        # After 1.2s cooldown -> next gesture accepted
        self.classifier.process_sample(SensorSample(0.0, 0.0, 9.81, 0.0, 0.0, 0.0, timestamp=11.0))
        s3 = SensorSample(9.0, 0.0, 9.81, 0.0, 2.0, 0.0, timestamp=11.5)
        self.assertEqual(self.classifier.process_sample(s3), Choice.SCISSORS)

    def test_custom_axis_orientation_configuration(self) -> None:
        custom_cfg = GestureConfig(
            left_axis="ax",
            left_sign=1,
            right_axis="ax",
            right_sign=-1,
            up_axis="az",
            up_sign=1,
            accel_threshold=6.0,
            require_gyro=False,
        )
        clf = GestureClassifier(config=custom_cfg)
        clf.calibrate([SensorSample(0.0, 0.0, 9.81, timestamp=1.0)])

        self.assertEqual(
            clf.process_sample(SensorSample(8.0, 0.0, 9.81, timestamp=5.0)),
            Choice.ROCK,
        )
        clf.reset_lock()
        self.assertEqual(
            clf.process_sample(SensorSample(0.0, 0.0, 17.5, timestamp=10.0)),
            Choice.PAPER,
        )

    def test_parse_combined_and_split_termux_json_chunks(self) -> None:
        # 1. Combined JSON chunk
        raw_combined = """
        {
          "ICM20600 Accelerometer": {
            "values": [-8.4, 0.5, 9.7]
          },
          "ICM20600 Gyroscope": {
            "values": [0.1, 1.9, -0.2]
          }
        }
        """
        sample = TermuxSensorReader.parse_single_chunk(raw_combined)
        self.assertIsNotNone(sample)
        assert sample is not None
        self.assertAlmostEqual(sample.ax, -8.4)
        self.assertAlmostEqual(sample.gy, 1.9)

        # 2. Split JSON chunks (Gyro arrives first, Accel arrives second)
        raw_gyro_only = '{"ICM20600 Gyroscope": {"values": [0.4, 2.5, -0.3]}}'
        raw_accel_only = '{"ICM20600 Accelerometer": {"values": [9.1, -0.2, 9.8]}}'

        s_first, last_acc, last_gyr = TermuxSensorReader.parse_termux_json_chunk(raw_gyro_only)
        self.assertIsNone(s_first)  # Waiting for first accelerometer reading
        self.assertEqual(last_gyr, (0.4, 2.5, -0.3))

        s_merged, last_acc, last_gyr = TermuxSensorReader.parse_termux_json_chunk(
            raw_accel_only, last_accel=last_acc, last_gyro=last_gyr
        )
        self.assertIsNotNone(s_merged)
        assert s_merged is not None
        # Merged sample preserves the gyro values from the preceding chunk!
        self.assertAlmostEqual(s_merged.ax, 9.1)
        self.assertAlmostEqual(s_merged.gy, 2.5)


if __name__ == "__main__":
    unittest.main()
