# PROJECT STATE — MOTION RPS IoT

## 1. Status Tahap
- **Tahap Selesai**:
  - **Tahap 1 — Audit Sebelum Implementasi**: Audit menyeluruh alur Sensor Smartphone → Edge Processing → Gesture Recognition → MQTT → Backend → Game Engine → Dashboard.
  - **Tahap 2 — Arsitektur QR Pairing & Mobile Web**: Generator QR ISO/IEC 18004 murni (`src/engine/qr_encoder.py`), `SessionPairingManager` dengan token sementara `pt` ber-TTL, halaman mobile `/pair` (`src/dashboard/pair.html`, `pair.js`).
  - **Tahap 3 — Integrasi MQTT & Auto-Pairing Termux**: Topik pairing dinamis `motion-rps/pairing/slot/{player_id}` & endpoint `/api/pair/assignment`, launcher satu perintah `scripts/start_edge.sh`.
  - **Tahap 4 — Proteksi Sesi & Slot Pemain**: Status bertingkat `WAITING` -> `PAIRED` -> `READY/CONNECTED`, proteksi `SLOT_ALREADY_OCCUPIED`, `SESSION_FULL`, dan isolasi sesi lama.
  - **Tahap 5 — Optimalisasi Termux & Background**: Script setup `scripts/setup_termux.sh`, persistent config `~/.motion_rps_edge.json`, `termux-wake-lock`, dan heartbeat 2.0s.
  - **Tahap 6 — Integrasi Machine Learning (ML Classifier Utama)**:
    - Menggantikan rule-based heuristic dengan **Random Forest Classifier (60 trees)** via JSON model (`models/gesture_model.json`).
    - Ekstraksi 44 fitur matematis (`src/edge/ml_features.py`) dengan strict parity antara pipeline training (`scripts/train_gesture_model.py`) dan runtime edge inference (`src/edge/ml_gesture.py`).
    - Evaluasi inference pure-Python (`PureForest`) tanpa dependency berat (`scikit-learn` / `numpy`), latensi < 1 ms di Android Termux.
    - Resolusi kegagalan rebound sumbu-X: akurasi replay dataset mencapai **99.50% (201/202)** dibanding rule-based lama yang hanya **75.25% (152/202)**.
    - Evaluasi Leave-One-Group-Out (cross-device/user validation) tercatat **75.74%**, membuktikan perlunya training multi-device untuk generalisasi hand-swing amplitude.
  - **Tahap 7 — Stabilitas MQTT & Persistensi Room ID**:
    - Mekanisme persistensi room ID melalui `resolve_stable_session_id()`, `load_persistent_session_id()`, dan `save_persistent_session_id()` dengan file `.active_session.json`.
    - Server restart dan crash recovery mempertahankan room ID yang sama (`rps_kelompok_07` secara default atau sesi aktif terakhir), mencegah regenerasi room ID acak.
    - Edge Client mempertahankan room ID melalui `~/.motion_rps_edge.json` dan otomatis reconnect ke room stabil.
  - **Tahap 8 — Otomasi Pengujian & Validasi**:
    - **48/48 Automated Tests PASS** pada Python unittest suite.
- **Tahap Aktif**: **SELESAI (Siap Pengujian Fisik 2 Smartphone Android)**

---

## 2. Arsitektur Machine Learning (Primary Gesture Recognition)

### A. Preprocessing & Windowing
- **Sensor Input**: Accelerometer ($a_x, a_y, a_z$ dalam $m/s^2$) dan Gyroscope ($g_x, g_y, g_z$ dalam $rad/s$ atau $deg/s$) dengan sampling rate rata-rata ~9–10 Hz.
- **Baseline Calibration**: Rata-rata 10–20 sampel saat smartphone diam untuk mengeliminasi komponen gravitasi statis dan orientasi awal.
- **Onset Detection**: Memicu windowing jika $\Delta a_{total} = |\Delta a_x| + |\Delta a_y| + |\Delta a_z| > 3.0\ m/s^2$.
- **Window Length (`seg_n`)**: Tepat 10 sampel (~1.0 detik durasi ayunan), mengunci buffer dan mengekstrak 44 fitur.
- **Post-Gesture Settling**: Sensor harus kembali diam ($RMS \le 1.0\ m/s^2$) selama minimal 3 sampel atau waktu cooldown (debounce 0.8–1.0 detik) agar ayunan balik (*rebound*) tidak memicu gesture ganda.

### B. Ekstraksi 44 Fitur (`src/edge/ml_features.py`)
1. **Fitur Accelerometer (30 fitur)**:
   - Sumbu $dx, dy, dz$ (deviasi dari baseline): Mean, Std, Min, Max, Energy ($\sum x_i^2 / N$).
   - Magnitudo $a_{mag} = \sqrt{a_x^2 + a_y^2 + a_z^2}$: Mean, Std, Min, Max, Energy.
   - Bidang horizontal $a_{horiz} = \sqrt{dx^2 + dy^2}$: Mean, Std, Min, Max, Energy.
   - Sumbu derivatif $a_{diff} = |\Delta a_i - \Delta a_{i-1}|$: Mean, Std, Min, Max, Energy.
2. **Fitur Gyroscope (14 fitur)**:
   - Sumbu $gx, gy$: Mean, Std, Min, Max, Energy.
   - Sumbu $gz$: Mean, Max.
   - Magnitudo $g_{mag} = \sqrt{gx^2 + gy^2 + gz^2}$: Mean, Max.

### C. Inference Engine (`PureForest` di `src/edge/ml_features.py`)
- Struktur pohon keputusan diekspor ke JSON standar (`models/gesture_model.json`).
- Traversal pohon dilakukan dengan pure-Python recursions/loops:
  - If `feature_value <= threshold`, ke anak kiri (`left`), selain itu ke anak kanan (`right`).
  - Akumulasi vektor probabilitas daun dari seluruh 60 pohon.
  - Threshold probabilitas minimum (`min_confidence = 0.50`). Jika probabilitas tertinggi $< 0.50$, output ditolak sebagai `NO-GESTURE` untuk mencegah false-positive.

---

## 3. Arsitektur MQTT & Stabilitas Room ID

1. **Konsistensi Broker & Topik**:
   - Broker MQTT dikonfigurasi terpusat via environment variable `MQTT_BROKER_HOST`, `MQTT_BROKER_PORT`, atau config edge.
   - Format topik terisolasi berdasarkan sesi dan slot:
     - Telemetri Sensor: `motion-rps/{session_id}/sensor/{player_id}`
     - Pilihan Gesture: `motion-rps/{session_id}/choice/{player_id}`
     - Kontrol Game: `motion-rps/{session_id}/control`
     - Status Game: `motion-rps/{session_id}/status`
     - Penugasan Pairing: `motion-rps/pairing/slot/{player_id}`
2. **Persistensi Room ID**:
   - `src/engine/pairing.py` menyediakan fungsi `resolve_stable_session_id()`.
   - Urutan prioritas resolusi room ID:
     1. Parameter eksplisit / argumen CLI (`--session`).
     2. Environment variable `RPS_SESSION_ID` atau `SESSION_ID`.
     3. File status persisten lokal `.active_session.json`.
     4. Default fallback stabil: `rps_kelompok_07`.
   - Dengan mekanisme ini, restart server atau crash tidak akan mengubah topic MQTT atau memutus koneksi kedua smartphone.

---

## 4. Hasil Pengujian Otomatis

Pemeriksaan unit test dan integrasi dijalankan dengan perintah:
`python -m unittest discover -s tests -v`

| Test Case Suite | Kasus Uji | Hasil | Deskripsi |
|---|---|:---:|---|
| `test_game_engine.py` | 8 pengujian | **PASS** | Validasi aturan RPS, draw, timeout forfeit, duplikasi pilihan, penolakan input di luar ronde, proteksi score update tunggal. |
| `test_sensor_gesture.py` | 9 pengujian | **PASS** | Kalibrasi baseline, orientasi sumbu dinamis, debounce ayunan balik, parsing payload sensor Termux, ekstraksi RMS. |
| `test_mqtt_simulation.py` | 7 pengujian | **PASS** | Simulasi 2 pemain virtual MQTT, isolasi topik, penolakan data non-finite/NaN, anti-replay seq+boot_id, TLS policy. |
| `test_pairing_and_integration.py` | 6 pengujian | **PASS** | QR pairing ISO 18004, penugasan sesi dinamis ke Termux, isolasi sesi lama, keamanan kredensial browser, endpoint HTTP `/pair`. |
| `test_ml_gesture.py` (Hybrid, Loader, Engine) | 11 pengujian | **PASS** | PureForest traversal, loading model JSON, onset detection, rejection low-confidence, settling requirement, rearm timeout. |
| `test_ml_gesture.py` (Parity & CSV Replay) | 2 pengujian | **PASS** | Replay dataset aktual CSV 202 sampel (akurasi 99.50%), validasi urutan dan nama 44 fitur identik. |
| `test_ml_gesture.py` (Edge Client ML Integration) | 3 pengujian | **PASS** | Edge client memuat ML default, penolakan sampel non-finite/NaN, penguncian pilihan tunggal per ronde. |
| `test_ml_gesture.py` (MQTT Stability & Room Persistence) | 2 pengujian | **PASS** | Room ID stabil di berbagai runtime restart, pembacaan dan penulisan `.active_session.json`. |
| **TOTAL** | **48 pengujian** | **48/48 PASS** | **100% Hijau (Ran 48 tests in ~4s)** |

---

## 5. Ringkasan File Proyek
- `models/gesture_model.json`: Model Random Forest (60 pohon, 44 fitur, kelas `["PAPER", "ROCK", "SCISSORS"]`).
- `scripts/train_gesture_model.py`: Script training scikit-learn dengan ekspor JSON pure-python parser.
- `src/edge/ml_features.py`: Implementasi pure-python ekstraksi 44 fitur matematis dan kelas `PureForest`.
- `src/edge/ml_gesture.py`: Engine inference runtime, windowing onset, settling detector, dan `HybridGestureClassifier`.
- `src/edge/edge_client.py`: Client sensor Termux dengan integrasi ML otomatis, auto-pairing QR, dan proteksi wake-lock.
- `src/engine/pairing.py`: Manajemen sesi, token pairing ber-TTL, dan persistensi room ID (`resolve_stable_session_id`).
- `src/engine/server.py`: Server web backend HTTP/WebSocket, dashboard server, dan sinkronisasi MQTT game loop.
- `tests/test_ml_gesture.py`: Automated test suite ML (parity, replay, edge client, room stability).
