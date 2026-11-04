"""ml_gesture.py - Pengenalan gerakan RPS berbasis Machine Learning untuk edge client (Termux).

Menggantikan keputusan rule-based (ambang akselerasi per sumbu) dengan Random Forest
`models/gesture_model.json`. Memakai `ml_features.py` yang SAMA dengan saat training,
sehingga fitur saat training dan saat bermain identik (tanpa train/serve skew).

Alur per sampel sensor (dipanggil dari EdgeDeviceClient.feed_sensor_sample):
  1. HP diam      -> baseline diperbarui dari 4 sampel terakhir yang tenang.
  2. Onset        -> sampel pertama dengan |accel - baseline| >= onset_delta (3.0 m/s^2).
  3. Segmen       -> kumpulkan `seg_n` (10) sampel mulai onset (~1.1 dtk pada ~9 Hz).
  4. Inferensi    -> fitur statistik -> RandomForest -> label + confidence.
  5. Re-arm       -> tunggu HP diam lagi sebelum gerakan berikutnya (anti gerakan balik).

Semua dalam Python murni (tanpa numpy/sklearn), cocok untuk Termux.
"""

import dataclasses
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Sequence, Tuple

from src.edge.ml_features import (
    PureForest,
    Sample,
    delta_norm,
    features_from_segment,
    segment_from_onset,
)
from src.edge.sensor_gesture import GestureClassifier, SensorSample
from src.engine.game_engine import Choice

DEFAULT_MODEL_RELPATH = os.path.join("models", "gesture_model.json")


@dataclass
class MLResult:
    """Hasil satu langkah mesin ML. status: WAITING | COLLECTING | DETECTED | REJECTED."""
    status: str
    reason: str
    label: Optional[str] = None
    confidence: float = 0.0
    probabilities: Dict[str, float] = field(default_factory=dict)


class MLGestureEngine:
    """Mesin segmentasi onset + inferensi. Tidak bergantung pada Termux, mudah diuji."""

    STILL_WINDOW = 4  # jumlah sampel tenang berurutan (~0.4 dtk) untuk dianggap HP diam

    def __init__(
        self,
        model: PureForest,
        min_confidence: float = 0.5,
        still_eps: float = 1.0,
        debounce_seconds: float = 1.2,
        rearm_timeout: float = 2.5,
    ) -> None:
        self.model = model
        self.min_confidence = float(min_confidence)
        self.still_eps = float(still_eps)          # m/s^2: sebaran maksimal agar dianggap diam
        self.debounce_seconds = float(debounce_seconds)
        self.rearm_timeout = float(rearm_timeout)  # jaga-jaga bila tangan tidak pernah benar-benar diam
        self.baseline: Tuple[float, float, float] = (0.0, 0.0, 9.81)
        self._ring: Deque[Sample] = deque(maxlen=self.STILL_WINDOW)
        self._collecting: List[Sample] = []
        self._seg_baseline: Tuple[float, float, float] = self.baseline
        self._armed = False
        self._cooldown_until = 0.0
        self._disarmed_at: Optional[float] = None

    # ---- kontrol state ----
    def set_baseline(self, baseline: Sequence[float]) -> None:
        """Baseline awal (dari kalibrasi). Tetap harus melihat HP diam dulu sebelum onset diterima."""
        self.baseline = (float(baseline[0]), float(baseline[1]), float(baseline[2]))
        self.reset()

    def reset(self) -> None:
        """Dipanggil tiap ronde baru: buang segmen berjalan, tunggu HP diam lagi."""
        self._collecting = []
        self._ring.clear()
        self._armed = False
        self._cooldown_until = 0.0
        self._disarmed_at = None

    # ---- util ----
    def _ring_mean(self) -> Tuple[float, float, float]:
        n = len(self._ring)
        return (
            sum(s[0] for s in self._ring) / n,
            sum(s[1] for s in self._ring) / n,
            sum(s[2] for s in self._ring) / n,
        )

    def _is_still(self) -> bool:
        if len(self._ring) < self._ring.maxlen:
            return False
        mean = self._ring_mean()
        return all(delta_norm(s, mean) < self.still_eps for s in self._ring)

    # ---- inti ----
    def feed(self, sample: Sample, now: float) -> MLResult:
        n_seg = self.model.seg_n

        if self._collecting:
            self._collecting.append(sample)
            if len(self._collecting) >= n_seg:
                return self._decide(now)
            return MLResult("COLLECTING", f"ML_COLLECTING ({len(self._collecting)}/{n_seg})")

        self._ring.append(sample)
        if self._is_still():
            self.baseline = self._ring_mean()
            self._armed = True
            self._disarmed_at = None
        elif not self._armed:
            if self._disarmed_at is None:
                self._disarmed_at = now
            elif now - self._disarmed_at >= self.rearm_timeout and len(self._ring) == self._ring.maxlen:
                self.baseline = self._ring_mean()
                self._armed = True
                self._disarmed_at = None

        d = delta_norm(sample, self.baseline)
        if not self._armed:
            return MLResult("REJECTED", f"ML_WAITING_SETTLE (delta={d:.2f})")
        if now < self._cooldown_until:
            return MLResult("REJECTED", f"ML_DEBOUNCE_COOLDOWN ({self._cooldown_until - now:.2f}s left)")
        if d >= self.model.onset_delta:
            self._seg_baseline = self.baseline
            self._collecting = [sample]
            self._ring.clear()
            return MLResult("COLLECTING", f"ML_ONSET (delta={d:.2f}) (1/{n_seg})")
        return MLResult("WAITING", f"ML_STATIONARY (delta={d:.2f})")

    def _decide(self, now: float) -> MLResult:
        seg = segment_from_onset(self._collecting, self._seg_baseline, 0, seg_n=self.model.seg_n)
        self._collecting = []
        self._ring.clear()
        self._armed = False
        self._disarmed_at = now
        self._cooldown_until = now + self.debounce_seconds
        if seg is None:
            return MLResult("REJECTED", "ML_SEGMENT_TOO_SHORT")

        probs = self.model.predict_proba(features_from_segment(seg))
        best = max(range(len(probs)), key=probs.__getitem__)
        label, conf = self.model.classes[best], probs[best]
        pdict = {c: round(p, 3) for c, p in zip(self.model.classes, probs)}
        if conf < self.min_confidence:
            return MLResult(
                "REJECTED",
                f"ML_LOW_CONFIDENCE ({label}={conf:.2f} < {self.min_confidence:.2f})",
                label, conf, pdict,
            )
        return MLResult("DETECTED", f"ML_DETECTED_{label} (conf={conf:.2f})", label, conf, pdict)


class HybridGestureClassifier(GestureClassifier):
    """
    Pengganti GestureClassifier dengan antarmuka yang sama (process_sample, calibrate, reset_lock,
    last_features, last_diagnostic). Bila `ml_engine` ada -> keputusan pilihan dari ML;
    bila None -> perilaku rule-based asli (fallback).
    last_features (RMS & rotasi) tetap dihitung oleh kelas induk untuk telemetri dashboard.
    """

    def __init__(self, config=None, window_size: int = 5, ml_engine: Optional[MLGestureEngine] = None) -> None:
        super().__init__(config=config, window_size=window_size)
        self.ml = ml_engine

    @property
    def mode(self) -> str:
        return "ML" if self.ml is not None else "RULE"

    def calibrate(self, samples: List[SensorSample]) -> Tuple[float, float, float]:
        baseline = super().calibrate(samples)
        if self.ml is not None:
            self.ml.set_baseline(baseline)
        return baseline

    def reset_lock(self) -> None:
        super().reset_lock()
        if self.ml is not None:
            self.ml.reset()

    def process_sample(self, sample: SensorSample) -> Optional[Choice]:
        rule_choice = super().process_sample(sample)
        if self.ml is None:
            return rule_choice

        now = sample.timestamp if sample.timestamp > 0 else time.time()
        res = self.ml.feed((sample.ax, sample.ay, sample.az, sample.gx, sample.gy, sample.gz), now)
        detected = res.status == "DETECTED" and res.label is not None
        self.last_diagnostic = dataclasses.replace(
            self.last_diagnostic,
            status="DETECTED" if detected else ("REJECTED" if res.status == "REJECTED" else "WAITING"),
            reason=res.reason,
            detected_choice=res.label if detected else None,
            triggered_axis="ML",
            best_direction=res.label,
            best_score=round(res.confidence, 4),
            threshold=self.ml.min_confidence,
        )
        return Choice[res.label] if detected else None


def load_ml_engine(
    path: Optional[str] = None,
    min_confidence: float = 0.5,
) -> Tuple[Optional[MLGestureEngine], str]:
    """Muat model JSON. Mengembalikan (engine, pesan); engine=None bila gagal (-> fallback rule-based)."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        path,
        os.getenv("GESTURE_MODEL_PATH"),
        DEFAULT_MODEL_RELPATH,
        os.path.join(here, "..", "..", DEFAULT_MODEL_RELPATH),
    ]
    for cand in candidates:
        if cand and os.path.isfile(cand):
            try:
                model = PureForest.load(cand)
            except Exception as exc:  # JSON rusak / fitur tidak cocok
                return None, f"[ML] Gagal memuat model {cand}: {exc}. Pakai rule-based."
            engine = MLGestureEngine(model, min_confidence=min_confidence)
            return engine, (
                f"[ML] Model dimuat: {cand} ({len(model.trees)} pohon, kelas={model.classes}, "
                f"min_conf={min_confidence})"
            )
    return None, f"[ML] File model tidak ditemukan ({DEFAULT_MODEL_RELPATH}). Pakai rule-based."