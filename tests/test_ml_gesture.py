"""Tes logika ml_gesture memakai model mini buatan tangan (bukan model Kaggle).

Pohon mini: dx_min <= -3 -> ROCK ; selain itu dx_max > 3 -> SCISSORS ; selain itu -> PAPER.
Jalankan: python -m unittest discover -s tests -v
"""
import json
import os
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()