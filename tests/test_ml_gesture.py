"""Tes logika ml_gesture memakai model mini buatan tangan (bukan model Kaggle).

Pohon mini: dx_min <= -3 -> ROCK ; selain itu dx_max > 3 -> SCISSORS ; selain itu -> PAPER.
Jalankan: python -m unittest discover -s tests -v
"""
import json
import math
import os
import tempfile
import unittest
from unittest.mock import patch

from src.edge.ml_features import FEATURE_NAMES, PureForest
from src.edge.ml_gesture import HybridGestureClassifier, MLGestureEngine, load_ml_engine
from src.edge.sensor_gesture import SensorSample
from src.engine.game_engine import Choice

DT = 0.11  # ~9 Hz
G = 9.81


def tiny_model(leaf_conf: float = 1.0) -> dict:
    def leaf(i):
        v = [(1 - leaf_conf) / 2] * 3
        v[i] = leaf_conf
        return v

    return {
        "version": 1,
        "classes": ["PAPER", "ROCK", "SCISSORS"],
        "feature_names": FEATURE_NAMES,
        "seg_n": 10,
        "onset_delta": 3.0,
        "trees": [{
            "left": [1, -1, 3, -1, -1],
            "right": [2, -1, 4, -1, -1],
            "feature": [FEATURE_NAMES.index("dx_min"), -2, FEATURE_NAMES.index("dx_max"), -2, -2],
            "threshold": [-3.0, -2.0, 3.0, -2.0, -2.0],
            "value": [[1 / 3] * 3, leaf(1), [1 / 3] * 3, leaf(0), leaf(2)],
        }],
    }


def rest(n, ax=0.0, ay=0.0, az=G):
    return [(ax, ay, az, 0.0, 0.0, 0.0)] * n


def shake(kind):
    xs, ys = {
        "ROCK": ([-9, -7, 6, 2, 0.5, 0, 0, 0, 0, 0], [0] * 10),
        "SCISSORS": ([9, 8, -1, -0.5, 0, 0, 0, 0, 0, 0], [0] * 10),
        "PAPER": ([0.3, 0.2, -0.2, 0, 0, 0, 0, 0, 0, 0], [9, 8, -1, -0.5, 0, 0, 0, 0, 0, 0]),
    }[kind]
    return [(x, y, G, 0.0, 0.0, 0.0) for x, y in zip(xs, ys)]


def run(engine, samples, t0=100.0):
    return [engine.feed(s, t0 + i * DT) for i, s in enumerate(samples)]


def make_engine(**kw):
    eng = MLGestureEngine(PureForest(tiny_model(kw.pop("leaf_conf", 1.0))), **kw)
    eng.set_baseline((0.0, 0.0, G))
    return eng


def detections(results):
    return [r for r in results if r.status == "DETECTED"]


class MLEngineTests(unittest.TestCase):
    def test_each_gesture_detected_after_full_segment(self):
        for kind in ("ROCK", "SCISSORS", "PAPER"):
            with self.subTest(kind=kind):
                res = run(make_engine(), rest(6) + shake(kind) + rest(4))
                det = detections(res)
                self.assertEqual([d.label for d in det], [kind])
                # onset di indeks 6 -> keputusan tepat di sampel ke-10 segmen (indeks 15)
                self.assertEqual(res.index(det[0]), 15)

    def test_no_double_trigger_on_backswing(self):
        backswing = [(7.0, 0, G, 0, 0, 0), (-6.0, 0, G, 0, 0, 0), (5.0, 0, G, 0, 0, 0)]
        res = run(make_engine(), rest(6) + shake("ROCK") + backswing + rest(2))
        self.assertEqual(len(detections(res)), 1)

    def test_second_gesture_after_settling(self):
        res = run(make_engine(), rest(6) + shake("ROCK") + rest(30) + shake("SCISSORS") + rest(3))
        self.assertEqual([d.label for d in detections(res)], ["ROCK", "SCISSORS"])

    def test_low_confidence_rejected(self):
        res = run(make_engine(leaf_conf=0.4, min_confidence=0.5), rest(6) + shake("ROCK") + rest(3))
        self.assertEqual(detections(res), [])
        self.assertTrue(any("LOW_CONFIDENCE" in r.reason for r in res))

    def test_no_false_trigger_when_phone_held_in_new_orientation(self):
        eng = make_engine()  # dikalibrasi datar, lalu dipegang tegak
        res = run(eng, rest(8, ax=0.0, ay=G, az=0.0) + shake_upright("ROCK") + rest(3, ay=G, az=0.0))
        self.assertEqual([d.label for d in detections(res)], ["ROCK"])

    def test_no_trigger_before_phone_is_still(self):
        res = run(make_engine(), shake("ROCK"))  # langsung goyang tanpa diam dulu
        self.assertEqual(detections(res), [])

    def test_rearm_timeout_when_hand_never_still(self):
        eng = make_engine(rearm_timeout=1.0)
        jitter = [((1.5 if i % 2 else -1.5), 0.0, G, 0.0, 0.0, 0.0) for i in range(30)]
        res = run(eng, jitter + shake("SCISSORS") + rest(3))
        self.assertEqual([d.label for d in detections(res)], ["SCISSORS"])

    def test_reset_discards_partial_segment(self):
        eng = make_engine()
        res = run(eng, rest(6) + shake("ROCK")[:5])
        self.assertTrue(any(r.status == "COLLECTING" for r in res))
        eng.reset()
        res = run(eng, rest(10), t0=200.0)
        self.assertEqual(detections(res), [])


def shake_upright(kind):
    """Gerakan pada HP yang dipegang tegak (gravitasi di sumbu y), goyangan tetap di sumbu x."""
    return [(x, G, 0.0, 0.0, 0.0, 0.0) for x, *_ in shake(kind)]


class HybridTests(unittest.TestCase):
    def _clf(self):
        clf = HybridGestureClassifier(ml_engine=MLGestureEngine(PureForest(tiny_model())))
        clf.calibrate([SensorSample(0.0, 0.0, G, timestamp=1.0)] * 3)
        return clf

    def test_returns_choice_enum_and_diagnostic(self):
        clf = self._clf()
        out = []
        for i, s in enumerate(rest(6) + shake("PAPER") + rest(2)):
            out.append(clf.process_sample(SensorSample(*s, timestamp=10.0 + i * DT)))
            if out[-1] is not None:
                self.assertEqual(clf.last_diagnostic.status, "DETECTED")
                self.assertEqual(clf.last_diagnostic.triggered_axis, "ML")
        self.assertEqual([c for c in out if c is not None], [Choice.PAPER])
        self.assertEqual(clf.mode, "ML")

    def test_without_engine_is_rule_mode(self):
        self.assertEqual(HybridGestureClassifier(ml_engine=None).mode, "RULE")


class LoaderTests(unittest.TestCase):
    def test_load_ok_and_missing_and_bad_features(self):
        with tempfile.TemporaryDirectory() as d:
            good = os.path.join(d, "m.json")
            with open(good, "w", encoding="utf-8") as f:
                json.dump(tiny_model(), f)
            eng, msg = load_ml_engine(good)
            self.assertIsNotNone(eng, msg)

            bad = os.path.join(d, "bad.json")
            data = tiny_model()
            data["feature_names"] = data["feature_names"][:-1]
            with open(bad, "w", encoding="utf-8") as f:
                json.dump(data, f)
            eng, msg = load_ml_engine(bad)
            self.assertIsNone(eng)
            self.assertIn("rule-based", msg)

        eng, msg = load_ml_engine(os.path.join(tempfile.gettempdir(), "tidak_ada_xyz.json"))
        # bila repo tidak punya models/gesture_model.json -> None; bila punya -> engine; keduanya valid
        self.assertTrue(eng is None or isinstance(eng, MLGestureEngine))


class MLParityAndCSVReplayTests(unittest.TestCase):
    """Test feature extraction parity and replay real phone CSV dataset against the exported model."""

    def test_feature_names_parity(self):
        self.assertEqual(len(FEATURE_NAMES), 44)
        self.assertIn("dx_abs_sum", FEATURE_NAMES)
        self.assertIn("first_dx", FEATURE_NAMES)
        self.assertIn("gz_min", FEATURE_NAMES)
        self.assertIn("dmag_max", FEATURE_NAMES)

    def test_csv_replay_accuracy_and_all_classes(self):
        import glob
        import pandas as pd
        from src.edge.ml_features import find_onset, segment_from_onset, features_from_segment

        model_path = os.path.join(os.path.dirname(__file__), "..", "models", "gesture_model.json")
        if not os.path.isfile(model_path):
            self.skipTest("models/gesture_model.json tidak ditemukan")
        forest = PureForest.load(model_path)

        csvs = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "..", "data", "*.csv")))
        self.assertGreaterEqual(len(csvs), 2)

        correct = 0
        total = 0
        per_class_counts = {"ROCK": 0, "PAPER": 0, "SCISSORS": 0}
        per_class_correct = {"ROCK": 0, "PAPER": 0, "SCISSORS": 0}

        for path in csvs:
            df = pd.read_csv(path)
            for _, g in df.groupby("trial_id"):
                g = g.sort_values("t_ms")
                label = str(g.iloc[0]["label"]).upper()
                if label not in per_class_counts:
                    continue
                bx, by, bz = float(g.iloc[0].bx), float(g.iloc[0].by), float(g.iloc[0].bz)
                samples = [tuple(float(v) for v in row) for row in g[["ax", "ay", "az", "gx", "gy", "gz"]].itertuples(index=False)]
                onset = find_onset(samples, (bx, by, bz))
                if onset is None:
                    continue
                seg = segment_from_onset(samples, (bx, by, bz), onset)
                if seg is None:
                    continue
                feats = features_from_segment(seg)
                self.assertEqual(len(feats), 44)
                for fv in feats:
                    self.assertTrue(math.isfinite(fv))
                pred, conf = forest.predict(feats)
                total += 1
                per_class_counts[label] += 1
                if pred == label:
                    correct += 1
                    per_class_correct[label] += 1

        self.assertGreaterEqual(total, 180)
        overall_acc = correct / total
        self.assertGreaterEqual(overall_acc, 0.95, f"Akurasi model deployed {overall_acc:.4f} di bawah 95%")
        for lbl in ("ROCK", "PAPER", "SCISSORS"):
            self.assertGreaterEqual(per_class_counts[lbl], 50)
            class_acc = per_class_correct[lbl] / per_class_counts[lbl]
            self.assertGreaterEqual(class_acc, 0.95, f"Akurasi kelas {lbl} ({class_acc:.4f}) di bawah 95%")


class EdgeClientMLTests(unittest.TestCase):
    """Test EdgeDeviceClient with Machine Learning as primary classifier."""

    def test_edge_device_client_ml_defaults(self):
        from src.edge.edge_client import EdgeDeviceClient
        from src.engine.pairing import PairingToken

        tok = PairingToken(session_id="test-ml-session", player_id="PLAYER_A")
        client = EdgeDeviceClient(token=tok, use_ml=True)
        self.assertEqual(client.classifier.mode, "ML")
        self.assertIsInstance(client.classifier, HybridGestureClassifier)

    def test_non_finite_sensor_samples_safely_rejected(self):
        from src.edge.edge_client import EdgeDeviceClient
        from src.engine.pairing import PairingToken

        tok = PairingToken(session_id="test-nan-session", player_id="PLAYER_A")
        client = EdgeDeviceClient(token=tok, use_ml=True)
        client.classifier.calibrate([SensorSample(0.0, 0.0, 9.81, timestamp=1.0)])

        nan_sample = SensorSample(float("nan"), 0.0, 9.81, timestamp=2.0)
        res = client.feed_sensor_sample(nan_sample, publish_live_telemetry=False)
        self.assertIsNone(res)
        self.assertEqual(client.classifier.last_diagnostic.status, "REJECTED")

        inf_sample = SensorSample(0.0, float("inf"), 9.81, timestamp=2.1)
        res2 = client.feed_sensor_sample(inf_sample, publish_live_telemetry=False)
        self.assertIsNone(res2)
        self.assertEqual(client.classifier.last_diagnostic.status, "REJECTED")

    def test_single_choice_lock_per_round_with_ml(self):
        from src.edge.edge_client import EdgeDeviceClient
        from src.engine.pairing import PairingToken

        published_choices = []
        def fake_publish(topic, payload, qos, retain):
            if "choice" in topic:
                published_choices.append(json.loads(payload))

        tok = PairingToken(session_id="test-round-session", player_id="PLAYER_A")
        client = EdgeDeviceClient(token=tok, use_ml=True, publish_fn=fake_publish)
        client.classifier.calibrate([SensorSample(0.0, 0.0, 9.81, timestamp=1.0)])
        client.round_active = True
        client.active_round_id = 7

        # Feed gesture stream from tiny_model (ROCK)
        t0 = 100.0
        for i, s in enumerate(rest(6) + shake("ROCK") + rest(4)):
            sample = SensorSample(*s, timestamp=t0 + i * DT)
            client.feed_sensor_sample(sample, publish_live_telemetry=False)

        # Feeding more samples in same round must not emit duplicate choices
        for i, s in enumerate(shake("ROCK") + rest(4)):
            sample = SensorSample(*s, timestamp=t0 + 50.0 + i * DT)
            client.feed_sensor_sample(sample, publish_live_telemetry=False)

        self.assertEqual(len(published_choices), 1)
        self.assertEqual(published_choices[0]["choice"], "ROCK")
        self.assertEqual(published_choices[0]["round_id"], 7)


class MQTTStabilityAndRoomPersistenceTests(unittest.TestCase):
    """Test MQTT room identifier and topic persistence across server restarts."""

    def setUp(self):
        from src.engine.pairing import load_persistent_session_id
        self._orig_session = load_persistent_session_id() or "rps-kelompok12"

    def tearDown(self):
        from src.engine.pairing import save_persistent_session_id
        save_persistent_session_id(self._orig_session)

    def test_resolve_stable_session_id_persistence(self):
        from src.engine.pairing import (
            DEFAULT_SESSION_FILE,
            load_persistent_session_id,
            resolve_stable_session_id,
            save_persistent_session_id,
        )

        with tempfile.TemporaryDirectory() as d:
            fpath = os.path.join(d, "test_session.json")
            save_persistent_session_id("room_persisten_123", filepath=fpath)
            loaded = load_persistent_session_id(filepath=fpath)
            self.assertEqual(loaded, "room_persisten_123")

    def test_room_id_unchanged_across_runtime_contexts(self):
        from src.engine.server import GameRuntimeContext

        ctx1 = GameRuntimeContext(session_id="room_tetap_456")
        self.assertEqual(ctx1.engine.session_id, "room_tetap_456")

        # Simulate restart without passing session_id
        with patch.dict(os.environ, {}, clear=True):
            ctx2 = GameRuntimeContext()
            self.assertEqual(ctx2.engine.session_id, "room_tetap_456")
            self.assertEqual(ctx1.bridge.topics.game_state(), ctx2.bridge.topics.game_state())
            self.assertEqual(
                ctx1.bridge.topics.player_choice("PLAYER_A"),
                ctx2.bridge.topics.player_choice("PLAYER_A"),
            )


if __name__ == "__main__":
    unittest.main()