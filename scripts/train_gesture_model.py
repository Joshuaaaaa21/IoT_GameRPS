#!/usr/bin/env python3
"""
train_gesture_model.py - latih model gesture RPS dari CSV hasil record_gestures.py.

Di Kaggle (setelah upload dataset berisi CSV + ml_features.py + file ini):
    !python /kaggle/input/<nama-dataset>/train_gesture_model.py

Lokal:
    python train_gesture_model.py --data data/

Output (folder /kaggle/working atau ./out):
    gesture_model.json   <- model untuk dipakai di HP (Python murni)
    report.txt           <- ringkasan akurasi & confusion matrix untuk laporan
"""
import argparse
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
ROOT = os.path.dirname(HERE)
for p in (HERE, os.path.join(ROOT, "src", "edge"), ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)
for p in glob.glob("/kaggle/input/**/ml_features.py", recursive=True):
    sys.path.insert(0, os.path.dirname(p))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sklearn.ensemble import RandomForestClassifier  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.metrics import confusion_matrix  # noqa: E402
from sklearn.model_selection import (LeaveOneGroupOut, RepeatedStratifiedKFold,  # noqa: E402
                                     cross_val_predict)
from sklearn.pipeline import make_pipeline  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402

try:
    import ml_features as mf  # noqa: E402
except ImportError:
    from src.edge import ml_features as mf  # noqa: E402

LABELS = ["ROCK", "PAPER", "SCISSORS"]
REQUIRED = ["trial_id", "label", "person", "device", "t_ms",
            "ax", "ay", "az", "gx", "gy", "gz", "bx", "by", "bz", "rule_pred", "delay_ms"]

lines = []


def out(text=""):
    print(text)
    lines.append(text)


def find_csvs(data_dir):
    files = sorted(glob.glob(os.path.join(data_dir, "**", "*.csv"), recursive=True))
    return [f for f in files if os.path.basename(f) != "report.csv"]


def load_trials(files):
    trials = []
    dropped = {"tanpa_gerakan": 0, "terlalu_pendek": 0}
    for path in files:
        df = pd.read_csv(path)
        missing = [c for c in REQUIRED if c not in df.columns]
        if missing:
            out(f"[LEWATI] {os.path.basename(path)}: kolom hilang {missing}")
            continue
        src = os.path.basename(path)
        for tid, g in df.groupby("trial_id"):
            g = g.sort_values("t_ms")
            first = g.iloc[0]
            baseline = (float(first.bx), float(first.by), float(first.bz))
            samples = [tuple(float(v) for v in row)
                       for row in g[["ax", "ay", "az", "gx", "gy", "gz"]].itertuples(index=False)]
            onset = mf.find_onset(samples, baseline)
            if onset is None:
                dropped["tanpa_gerakan"] += 1
                trials.append(dict(src=src, tid=tid, label=first.label, rule=first.rule_pred,
                                   person=first.person, device=first.device, x=None))
                continue
            seg = mf.segment_from_onset(samples, baseline, onset)
            if seg is None:
                dropped["terlalu_pendek"] += 1
                trials.append(dict(src=src, tid=tid, label=first.label, rule=first.rule_pred,
                                   person=first.person, device=first.device, x=None))
                continue
            trials.append(dict(src=src, tid=tid, label=first.label, rule=first.rule_pred,
                               person=first.person, device=first.device,
                               x=mf.features_from_segment(seg)))
    return trials, dropped


def acc(y_true, y_pred):
    y_true, y_pred = np.asarray(y_true), np.asarray(y_pred)
    return float((y_true == y_pred).mean()) if len(y_true) else float("nan")


def export_forest(rf, path):
    trees = []
    for est in rf.estimators_:
        t = est.tree_
        val = t.value[:, 0, :]
        val = val / val.sum(axis=1, keepdims=True)
        trees.append(dict(
            left=t.children_left.tolist(),
            right=t.children_right.tolist(),
            feature=[int(f) for f in t.feature.tolist()],
            threshold=[float(v) for v in t.threshold.tolist()],
            value=[[float(v) for v in row] for row in val.tolist()],
        ))
    model = dict(version=1, classes=[str(c) for c in rf.classes_],
                 feature_names=mf.FEATURE_NAMES, seg_n=mf.SEG_N,
                 onset_delta=mf.ONSET_DELTA, delay_ms=100, trees=trees)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(model, f)
    return os.path.getsize(path)


def main():
    ap = argparse.ArgumentParser()
    default_dir = "/kaggle/input" if os.path.isdir("/kaggle/input") else "data"
    ap.add_argument("--data", default=default_dir, help="folder berisi CSV (dicari rekursif)")
    ap.add_argument("--out", default="/kaggle/working" if os.path.isdir("/kaggle/working") else "out")
    ap.add_argument("--trees", type=int, default=60)
    ap.add_argument("--depth", type=int, default=6)
    args, _ = ap.parse_known_args()
    os.makedirs(args.out, exist_ok=True)

    files = find_csvs(args.data)
    if not files:
        sys.exit(f"Tidak ada CSV di {args.data}")
    out("File data:")
    for f in files:
        out(f"  - {f}")

    trials, dropped = load_trials(files)
    valid = [t for t in trials if t["x"] is not None]
    out(f"\nTotal trial: {len(trials)} | dipakai: {len(valid)} | "
        f"dibuang: {dropped} (tanpa gerakan = tidak ada lonjakan >= {mf.ONSET_DELTA} m/s2)")
    if len(valid) < 15:
        sys.exit("Data terlalu sedikit untuk dilatih.")

    X = np.array([t["x"] for t in valid])
    y = np.array([t["label"] for t in valid])
    groups = np.array([t["src"] for t in valid])
    out("\nJumlah trial per label (dipakai):")
    for lab in LABELS:
        out(f"  {lab:9s}: {int((y == lab).sum())}")
    out("Jumlah trial per file:")
    for g in sorted(set(groups)):
        out(f"  {g}: {int((groups == g).sum())}")

    # --- baseline rule-based (dari kolom rule_pred hasil perekaman) ---
    all_true = [t["label"] for t in trials]
    all_rule = [t["rule"] for t in trials]
    out("\n=== BASELINE RULE-BASED (GestureClassifier asli) ===")
    out(f"Akurasi semua trial : {acc(all_true, all_rule):.3f}  (n={len(trials)})")
    out(f"Akurasi pada trial yg dipakai ML: {acc(y, [t['rule'] for t in valid]):.3f}")
    cm = confusion_matrix(all_true, all_rule, labels=LABELS + ["NONE"])
    out("Confusion (baris=asli, kolom=prediksi) urutan " + str(LABELS + ["NONE"]))
    out(str(cm))

    # --- evaluasi ML ---
    models = {
        "LogReg": make_pipeline(StandardScaler(), LogisticRegression(max_iter=3000)),
        "RandomForest": RandomForestClassifier(n_estimators=args.trees, max_depth=args.depth,
                                               min_samples_leaf=2, class_weight="balanced",
                                               random_state=0),
    }
    out("\n=== EVALUASI ML ===")
    n_groups = len(set(groups))
    min_class = min(int((y == lab).sum()) for lab in LABELS)
    for name, model in models.items():
        if n_groups >= 2:
            pred = cross_val_predict(model, X, y, groups=groups, cv=LeaveOneGroupOut())
            out(f"{name:13s} latih-di-file-lain, uji-di-file-ini : {acc(y, pred):.3f}   <- paling jujur")
            for g in sorted(set(groups)):
                m = groups == g
                out(f"    uji di {g}: {acc(y[m], pred[m]):.3f} (n={int(m.sum())})")
            if name == "RandomForest":
                out("    Confusion (baris=asli, kolom=prediksi) " + str(LABELS))
                out(str(confusion_matrix(y, pred, labels=LABELS)))
        splits = min(5, min_class)
        if splits >= 2:
            cv = RepeatedStratifiedKFold(n_splits=splits, n_repeats=3, random_state=0)
            scores = []
            for tr, te in cv.split(X, y):
                model.fit(X[tr], y[tr])
                scores.append(acc(y[te], model.predict(X[te])))
            out(f"{name:13s} acak (data dicampur, cenderung lebih optimis): "
                f"{np.mean(scores):.3f} +/- {np.std(scores):.3f}")

    # --- latih final & export ---
    rf = models["RandomForest"]
    rf.fit(X, y)
    model_path = os.path.join(args.out, "gesture_model.json")
    size = export_forest(rf, model_path)
    out(f"\nModel diexport: {model_path} ({size / 1024:.1f} KB)")

    # verifikasi: prediksi Python murni harus sama dengan sklearn
    pure = mf.PureForest.load(model_path)
    mismatch = 0
    max_diff = 0.0
    sk_proba = rf.predict_proba(X)
    for i in range(len(X)):
        p = pure.predict_proba(X[i].tolist())
        max_diff = max(max_diff, max(abs(a - b) for a, b in zip(p, sk_proba[i])))
        if pure.classes[int(np.argmax(p))] != rf.predict(X[i:i + 1])[0]:
            mismatch += 1
    out(f"Verifikasi Python-murni vs sklearn: selisih proba maks {max_diff:.2e}, "
        f"prediksi beda {mismatch}/{len(X)}")

    imp = sorted(zip(mf.FEATURE_NAMES, rf.feature_importances_), key=lambda kv: -kv[1])[:8]
    out("\nFitur paling berpengaruh:")
    for n, v in imp:
        out(f"  {n:14s} {v:.3f}")

    with open(os.path.join(args.out, "report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    out(f"\nLaporan: {os.path.join(args.out, 'report.txt')}")


if __name__ == "__main__":
    main()
