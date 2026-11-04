"""
ml_features.py - ekstraksi fitur + inferensi model gesture (Python murni, tanpa numpy/sklearn).

Dipakai di DUA tempat dengan kode yang SAMA:
  1. Kaggle (training)   -> train_gesture_model.py meng-import file ini
  2. HP / Termux (game)  -> taruh di src/edge/ml_features.py

Satu kode untuk keduanya mencegah "train/serve skew" (fitur saat training beda dari saat main).

Konsep:
  - Sampel dinyatakan sebagai DELTA terhadap baseline diam (accel - baseline), gyro apa adanya.
  - ONSET = sampel pertama yang |delta accel| >= ONSET_DELTA. Dari situ diambil SEG_N sampel.
  - Dari segmen itu dihitung fitur statistik, lalu forest (di-export ke JSON) memilih label.
"""
import json
import math
from typing import Dict, List, Optional, Sequence, Tuple

ONSET_DELTA = 3.0   # m/s^2: |accel - baseline| minimal agar dianggap mulai bergerak
SEG_N = 10          # jumlah sampel per segmen (~1.1 dtk pada ~9 Hz)
MIN_SEG = 6         # segmen yang tersedia kurang dari ini -> ditolak
CHANNELS = ["dx", "dy", "dz", "gx", "gy", "gz"]

Sample = Tuple[float, float, float, float, float, float]  # ax, ay, az, gx, gy, gz


def delta_norm(sample: Sample, baseline: Tuple[float, float, float]) -> float:
    return math.sqrt(
        (sample[0] - baseline[0]) ** 2
        + (sample[1] - baseline[1]) ** 2
        + (sample[2] - baseline[2]) ** 2
    )


def find_onset(samples: Sequence[Sample], baseline, onset_delta: float = ONSET_DELTA) -> Optional[int]:
    for i, s in enumerate(samples):
        if delta_norm(s, baseline) >= onset_delta:
            return i
    return None


def segment_from_onset(samples: Sequence[Sample], baseline, onset_idx: int,
                       seg_n: int = SEG_N, min_seg: int = MIN_SEG) -> Optional[List[List[float]]]:
    """Ambil seg_n sampel mulai onset -> list [dx,dy,dz,gx,gy,gz]. Kurang dari seg_n di-pad nol."""
    chunk = samples[onset_idx:onset_idx + seg_n]
    if len(chunk) < min_seg:
        return None
    seg = [[s[0] - baseline[0], s[1] - baseline[1], s[2] - baseline[2], s[3], s[4], s[5]]
           for s in chunk]
    while len(seg) < seg_n:
        seg.append([0.0] * 6)
    return seg


def _feature_names() -> List[str]:
    names = []
    for ch in CHANNELS:
        for stat in ("mean", "std", "min", "max", "abs_sum"):
            names.append(f"{ch}_{stat}")
    names += ["dmag_max", "dmag_mean", "dmag_peak_idx", "gmag_max", "gmag_mean"]
    names += ["first_dx", "first_dy", "first_dz"]
    for ch in ("dx", "dy", "dz"):
        names += [f"{ch}_argmin", f"{ch}_argmax"]
    return names


FEATURE_NAMES = _feature_names()


def features_from_segment(seg: Sequence[Sequence[float]]) -> List[float]:
    n = len(seg)
    feats: List[float] = []
    cols = [[row[c] for row in seg] for c in range(6)]
    for col in cols:
        mean = sum(col) / n
        var = sum((v - mean) ** 2 for v in col) / n
        feats += [mean, math.sqrt(var), min(col), max(col), sum(abs(v) for v in col)]

    dmag = [math.sqrt(r[0] ** 2 + r[1] ** 2 + r[2] ** 2) for r in seg]
    gmag = [math.sqrt(r[3] ** 2 + r[4] ** 2 + r[5] ** 2) for r in seg]
    feats += [max(dmag), sum(dmag) / n, float(dmag.index(max(dmag))),
              max(gmag), sum(gmag) / n]
    feats += [seg[0][0], seg[0][1], seg[0][2]]
    for col in cols[:3]:
        feats += [float(col.index(min(col))), float(col.index(max(col)))]
    return feats


def features_from_trial(samples: Sequence[Sample], baseline) -> Optional[List[float]]:
    """Dari seluruh sampel satu trial -> vektor fitur, atau None bila tidak ada gerakan/terlalu pendek."""
    onset = find_onset(samples, baseline)
    if onset is None:
        return None
    seg = segment_from_onset(samples, baseline, onset)
    if seg is None:
        return None
    return features_from_segment(seg)


class PureForest:
    """Random Forest hasil export sklearn, prediksi tanpa library apa pun."""

    def __init__(self, data: Dict) -> None:
        self.classes: List[str] = data["classes"]
        self.feature_names: List[str] = data["feature_names"]
        self.trees: List[Dict] = data["trees"]
        self.seg_n: int = data.get("seg_n", SEG_N)
        self.onset_delta: float = data.get("onset_delta", ONSET_DELTA)
        if self.feature_names != FEATURE_NAMES:
            raise ValueError("Daftar fitur model tidak cocok dengan ml_features.py ini. "
                             "Latih ulang model memakai file ml_features.py yang sama.")

    @classmethod
    def load(cls, path: str) -> "PureForest":
        with open(path, "r", encoding="utf-8") as f:
            return cls(json.load(f))

    def predict_proba(self, x: Sequence[float]) -> List[float]:
        k = len(self.classes)
        total = [0.0] * k
        for tree in self.trees:
            left, right = tree["left"], tree["right"]
            feat, thr, val = tree["feature"], tree["threshold"], tree["value"]
            node = 0
            while left[node] != -1:
                node = left[node] if x[feat[node]] <= thr[node] else right[node]
            for i in range(k):
                total[i] += val[node][i]
        return [t / len(self.trees) for t in total]

    def predict(self, x: Sequence[float]) -> Tuple[str, float]:
        p = self.predict_proba(x)
        best = max(range(len(p)), key=lambda i: p[i])
        return self.classes[best], p[best]
