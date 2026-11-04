#!/usr/bin/env python3
"""
record_gestures.py - perekam dataset gesture untuk Motion RPS IoT.

Taruh di folder `scripts/` repo (atau root repo), lalu jalankan DARI ROOT REPO di Termux:

    python scripts/record_gestures.py --person josh --device HP_A

Memakai TermuxSensorReader & SensorSample bawaan proyek, jadi sensor, satuan, dan
parsing persis sama dengan saat game berjalan. Setiap trial juga dijalankan lewat
GestureClassifier (rule-based) dan prediksinya dicatat di kolom `rule_pred`,
sehingga akurasi rule-based vs ML bisa dibandingkan pada data yang sama.

Kontrol (ketik lalu Enter):
    (kosong)  rekam gesture berikutnya, urutan ROCK -> PAPER -> SCISSORS
    r / p / s paksa label ROCK / PAPER / SCISSORS
    u         hapus trial terakhir
    q         keluar

Gesture sesuai game:  LEFT = ROCK,  UP = PAPER,  RIGHT = SCISSORS
Cara tiap trial: pegang HP DIAM saat hitung mundur, mulai gerak tepat saat "GO!",
lalu kembalikan HP ke posisi diam.

Format CSV (1 baris = 1 sampel sensor):
    trial_id,label,person,device,t_ms,ax,ay,az,gx,gy,gz,bx,by,bz,rule_pred,delay_ms
  - ax..gz   : data mentah (accel m/s^2, gyro rad/s)
  - bx,by,bz : baseline diam = rata-rata accel 0.6 dtk sebelum GO (dipakai utk delta)
  - rule_pred: hasil classifier rule-based utk trial ini (ROCK/PAPER/SCISSORS/NONE)
"""
import argparse
import csv
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(_HERE), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from src.edge.sensor_gesture import (  # noqa: E402
    GestureClassifier,
    GestureConfig,
    SensorSample,
    TermuxSensorReader,
    vibrate_feedback,
)

FIELDS = ["trial_id", "label", "person", "device", "t_ms",
          "ax", "ay", "az", "gx", "gy", "gz",
          "bx", "by", "bz", "rule_pred", "delay_ms"]
CYCLE = ["ROCK", "PAPER", "SCISSORS"]
KEYS = {"r": "ROCK", "p": "PAPER", "s": "SCISSORS"}
BASELINE_WINDOW_SEC = 0.6


class SampleBuffer:
    """Membaca stream sensor proyek di background thread."""

    def __init__(self, delay_ms):
        self.delay_ms = delay_ms
        self.samples = deque(maxlen=20000)
        self.lock = threading.Lock()
        self.error = None
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        try:
            for s in TermuxSensorReader.stream_termux_sensors(delay_ms=self.delay_ms):
                with self.lock:
                    self.samples.append(s)
        except Exception as exc:  # noqa: BLE001
            self.error = exc

    def window(self, t0, t1):
        with self.lock:
            return [s for s in self.samples if t0 <= s.timestamp <= t1]

    def total(self):
        with self.lock:
            return len(self.samples)


def read_existing(path):
    """Return (next_trial_id, counts per label)."""
    counts = {l: 0 for l in CYCLE}
    if not os.path.exists(path):
        return 1, counts
    max_id = 0
    seen = set()
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != FIELDS:
            sys.exit(f"Header {path} beda dengan format script ini. "
                     "Pindahkan/hapus file lama atau pakai --out lain.")
        for row in reader:
            try:
                tid = int(row["trial_id"])
            except ValueError:
                continue
            max_id = max(max_id, tid)
            if tid not in seen:
                seen.add(tid)
                if row["label"] in counts:
                    counts[row["label"]] += 1
    return max_id + 1, counts


def remove_trial(path, trial_id):
    with open(path, newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["trial_id"] != str(trial_id)]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def append_trial(path, trial_id, label, person, device, samples, t0,
                 baseline, rule_pred, delay_ms):
    new_file = not os.path.exists(path)
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    bx, by, bz = baseline
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(FIELDS)
        for s in samples:
            t_ms = int(round((s.timestamp - t0) * 1000))
            w.writerow([trial_id, label, person, device, t_ms,
                        round(s.ax, 5), round(s.ay, 5), round(s.az, 5),
                        round(s.gx, 5), round(s.gy, 5), round(s.gz, 5),
                        round(bx, 5), round(by, 5), round(bz, 5),
                        rule_pred, delay_ms])


def rule_based_prediction(samples, baseline_samples, config):
    """Jalankan GestureClassifier asli proyek pada sampel trial."""
    clf = GestureClassifier(config=config)
    clf.calibrate(baseline_samples)
    clf.reset_lock()
    for s in samples:
        res = clf.process_sample(s)
        if res is not None:
            return res.value
    return "NONE"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--person", required=True, help="nama pemegang HP, mis. josh")
    ap.add_argument("--device", required=True, help="nama HP, mis. HP_A")
    ap.add_argument("--duration", type=float, default=2.0,
                    help="durasi rekam per gesture (detik), default 2.0")
    ap.add_argument("--delay-ms", type=int, default=100,
                    help="interval sensor ms, default 100 (sama dengan game)")
    ap.add_argument("--out", default="data/gestures.csv")
    ap.add_argument("--target", type=int, default=50,
                    help="target trial per label (info progres)")
    ap.add_argument("--min-samples", type=int, default=8,
                    help="trial dengan sampel lebih sedikit ditolak")
    ap.add_argument("--threshold", type=float, default=5.0,
                    help="threshold rule-based (samakan dengan edge_client)")
    ap.add_argument("--invert-x", action="store_true",
                    help="samakan dengan flag --invert-x di edge_client")
    args = ap.parse_args()

    if not TermuxSensorReader.is_termux_sensor_available():
        sys.exit("termux-sensor tidak ada. Jalankan di Termux dengan Termux:API "
                 "terpasang (pkg install termux-api).")

    cfg = GestureConfig(accel_threshold=args.threshold)
    if args.invert_x:
        cfg.left_sign = 1
        cfg.right_sign = -1

    buf = SampleBuffer(args.delay_ms)
    buf.start()
    print("Menyalakan sensor...")
    waited = 0.0
    while buf.total() < 3 and waited < 8.0 and buf.error is None:
        time.sleep(0.25)
        waited += 0.25
    if buf.error is not None or buf.total() < 3:
        sys.exit(f"Sensor tidak mengirim data. Error: {buf.error}. "
                 "Coba: termux-sensor -l")
    time.sleep(1.0)
    print(f"Sensor OK. Sampling awal ~{buf.total() / (waited + 1.0):.0f} Hz "
          f"(diminta {1000 / args.delay_ms:.0f} Hz).")

    next_id, counts = read_existing(args.out)
    last_trial = None
    cycle_idx = min(range(3), key=lambda i: counts[CYCLE[i]])
    rule_hits = 0
    rule_total = 0

    print(f"Output: {args.out} | orang: {args.person} | HP: {args.device}")
    print("LEFT=ROCK, UP=PAPER, RIGHT=SCISSORS. Pegang diam saat hitung mundur, "
          "gerak saat GO.\n"
          "Enter = lanjut | r/p/s = pilih label | u = undo | q = keluar\n")

    try:
        while True:
            prog = " | ".join(f"{l[0]}:{counts[l]}/{args.target}" for l in CYCLE)
            suggested = CYCLE[cycle_idx]
            cmd = input(f"[{prog}] Berikutnya: {suggested} > ").strip().lower()

            if cmd == "q":
                break
            if cmd == "u":
                if last_trial is None:
                    print("  Tidak ada trial untuk di-undo.")
                    continue
                tid, lbl = last_trial
                remove_trial(args.out, tid)
                counts[lbl] -= 1
                last_trial = None
                print(f"  Trial {tid} ({lbl}) dihapus.")
                continue
            if cmd and cmd not in KEYS:
                print("  Perintah tidak dikenal.")
                continue
            label = KEYS.get(cmd, suggested)

            print("  Pegang HP diam... ", end="", flush=True)
            for n in (3, 2, 1):
                print(f"{n} ", end="", flush=True)
                time.sleep(0.7)
            print("GO!", flush=True)
            t_go = time.monotonic()
            vibrate_feedback(100)
            time.sleep(args.duration)
            t_end = time.monotonic()

            base_samples = buf.window(t_go - BASELINE_WINDOW_SEC, t_go)
            samples = buf.window(t_go, t_end)
            hz = len(samples) / args.duration

            if len(samples) < args.min_samples or len(base_samples) < 1:
                print(f"  Data kurang (gerak={len(samples)}, diam={len(base_samples)} "
                      "sampel). Tidak disimpan, ulangi.")
                continue

            n = len(base_samples)
            baseline = (sum(s.ax for s in base_samples) / n,
                        sum(s.ay for s in base_samples) / n,
                        sum(s.az for s in base_samples) / n)
            pred = rule_based_prediction(samples, base_samples, cfg)

            append_trial(args.out, next_id, label, args.person, args.device,
                         samples, t_go, baseline, pred, args.delay_ms)
            counts[label] += 1
            last_trial = (next_id, label)
            rule_total += 1
            ok = pred == label
            rule_hits += int(ok)
            mark = "BENAR" if ok else "SALAH"
            print(f"  Tersimpan trial {next_id} {label}: {len(samples)} sampel "
                  f"(~{hz:.0f} Hz). Rule-based: {pred} [{mark}] "
                  f"| akurasi rule sesi ini {rule_hits}/{rule_total}")
            next_id += 1
            if not cmd:
                cycle_idx = (cycle_idx + 1) % 3
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        if shutil.which("termux-sensor"):
            subprocess.run(["termux-sensor", "-c"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        print("Selesai. Data:", args.out)


if __name__ == "__main__":
    main()