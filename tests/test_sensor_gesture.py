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
            accel_threshold=6.5,
            settle_threshold=2.5,
            debounce_seconds=1.2,
            require_gyro=True,
            gyro_min_magnitude=0.8,
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
        # 1. Stationary phone -> RMS ~ 0, rotation ~ 0
        still_samples = [
            SensorSample(0.0, 0.0, 9.81, 0.0, 0.0, 0.0, timestamp=i * 0.5)
            for i in range(5)
        ]
        still_res = extract_features(still_samples)
        self.assertAlmostEqual(still_res.motion_rms, 0.0, places=3)
        self.assertAlmostEqual(still_res.rotation_rad, 0.0, places=3)

        # 2. Moving phone -> RMS > 2.0, rotation > 0.9
        moving_samples = [
            SensorSample(0.0, 0.0, 12.0, 0.5, 0.0, 0.0, timestamp=i * 0.5)
            for i in range(5)
        ]
        move_res = extract_features(moving_samples)
        self.assertGreater(move_res.motion_rms, 2.0)
        self.assertGreater(move_res.rotation_rad, 0.9)

        # 3. Single sample raises ValueError
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

    def test_left_right_up_classification_and_rebound_protection(self) -> None:
        cases = [
            ("LEFT", Choice.ROCK),
            ("RIGHT", Choice.SCISSORS),
            ("UP", Choice.PAPER),
        ]
        for idx, (gesture_name, expected_choice) in enumerate(cases):
            with self.subTest(gesture=gesture_name):
                self.classifier.reset_lock()
                stream = TermuxSensorReader.generate_simulated_gesture_stream(
                    gesture_name, base_time=10.0 + idx * 10.0
                )
                detected = [
                    res
                    for sample in stream
                    if (res := self.classifier.process_sample(sample)) is not None
                ]
                self.assertEqual(detected, [expected_choice])
                self.assertGreater(self.classifier.last_features.motion_rms, 1.0)

    def test_debounce_blocks_rapid_double_gestures(self) -> None:
        s1 = SensorSample(-9.0, 0.0, 9.81, 0.0, 2.0, 0.0, timestamp=10.0)
        self.assertEqual(self.classifier.process_sample(s1), Choice.ROCK)

        s_settle = SensorSample(0.0, 0.0, 9.81, 0.0, 0.0, 0.0, timestamp=10.3)
        self.assertIsNone(self.classifier.process_sample(s_settle))

        s2 = SensorSample(9.0, 0.0, 9.81, 0.0, 2.0, 0.0, timestamp=10.6)
        self.assertIsNone(self.classifier.process_sample(s2))

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

    def test_parse_termux_json_chunk(self) -> None:
        raw = """
        {
          "ICM20600 Accelerometer": {
            "values": [-8.4, 0.5, 9.7]
          },
          "ICM20600 Gyroscope": {
            "values": [0.1, 1.9, -0.2]
          }
        }
        """
        sample = TermuxSensorReader.parse_termux_json_chunk(raw)
        self.assertIsNotNone(sample)
        assert sample is not None
        self.assertAlmostEqual(sample.ax, -8.4)
        self.assertAlmostEqual(sample.gy, 1.9)


if __name__ == "__main__":
    unittest.main()
