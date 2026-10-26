# PROJECT STATE — MOTION RPS IoT (AUDIT & INTEGRASI REFERENSI SELESAI)

## 1. Status Tahap
- **Tahap Selesai**:
  - **Tahap 0 — Audit dan Keputusan Integrasi Repository Referensi (`ruthhhs/iot-projek`)**
  - **Tahap 1 — Integrasi Komunikasi** (`mqtt_protocol.py`, `mqtt_bridge.py`, `.env.example`, `LICENSE`)
  - **Tahap 2 — Game Engine** (`game_engine.py` + validasi anti-replay, stale timestamp, dan skor tunggal per ronde)
  - **Tahap 3 — Sensor HP** (`sensor_gesture.py`, `edge_client.py`, `scripts/check_sensors.py`)
  - **Tahap 4 — Dashboard dan Pairing** (`server.py` `/config.json`, `pairing.py`, `src/dashboard/`)
  - **Tahap 5 — Integrasi Akhir & Pengujian Menyeluruh** (`tests/` — 27/27 PASS)
- **Tahap Aktif**: **SELESAI (100%)**

## 2. File yang Diperiksa dan Diubah
- **Dibuat / Diperbarui**:
  - `LICENSE` (dibuat: MIT License dengan atribusi penuh untuk `Copyright (c) 2026 PocketGuard IoT contributors`)
  - `.env.example` (diperbarui: menyelaraskan `PROJECT_ID`, `MQTT_HOST`, `MQTT_PORT`, `MQTT_TLS`, `MQTT_WS_URL`, `ALLOW_INSECURE_LOCAL`, `DASHBOARD_PORT`)
  - `src/engine/mqtt_protocol.py` (diperbarui: kebijakan `validate_tls_policy()`, `ALLOW_INSECURE_LOCAL`, dan `validate_finite_metrics()` dengan `math.isfinite`)
  - `src/engine/mqtt_bridge.py` (diperbarui: proteksi replay `(boot_id, sequence)` -> `REJECTED_REPLAY`, cek umur pesan `sent_at_ms` 30 detik -> `REJECTED_STALE`, dan LWT status `OFFLINE`)
  - `src/edge/sensor_gesture.py` (diperbarui: integrasi `extract_features()` `motion_rms` & `rotation_rad`, auto-discovery `discover_sensor_names()` via `termux-sensor -l` tanpa `uncalibrated`, serta `vibrate_feedback()` via `termux-vibrate`)
  - `src/edge/edge_client.py` (diperbarui: flag `--debug-sensors`, pengiriman metrik `motion_rms`/`rotation_rad`, dan getar haptik saat pilihan terkunci)
  - `src/edge/simulator.py` (diperbarui: penyertaan `boot_id`, `sequence`, `sent_at_ms`, `motion_rms`, dan `rotation_rad`)
  - `scripts/check_sensors.py` (diadaptasi dari referensi: diagnostik mandiri sensor Termux + mode `--simulate` tanpa membutuhkan MQTT)
  - `src/engine/server.py` (diperbarui: endpoint `/config.json` via `public_browser_config()` yang tidak pernah membocorkan `MQTT_PASSWORD`)
  - `tests/test_mqtt_simulation.py`, `tests/test_sensor_gesture.py`, `tests/test_pairing_and_integration.py` (diperbarui: total 27 unit & integration tests)

## 3. Ringkasan Komponen Referensi yang Digunakan / Ditolak
- **USED**: `scripts/check_sensors.py` (diagnostik sensor mandiri tanpa broker MQTT).
- **ADAPTED**:
  - `shared/sensors.py` -> `TermuxSensorReader.discover_sensor_names()` (`termux-sensor -l`, filter `uncalibrated`) & `read_single_sample()`.
  - `shared/processing.py` -> `extract_features()` (`motion_rms` dan `rotation_rad`).
  - `shared/alarm.py` -> `vibrate_feedback()` (`termux-vibrate`).
  - `shared/mqtt_connection.py` & `shared/messages.py` -> `validate_tls_policy()`, `ALLOW_INSECURE_LOCAL`, `validate_finite_metrics()`.
  - `shared/dashboard_server.py` -> `public_browser_config()` (`/config.json` bebas bocoran password).
- **REFERENCE ONLY**: Anti-replay `(boot_id, sequence)` dan pemeriksaan `sent_at_ms` dari `nodes/detector_service/rules.py`.
- **NOT USED (Ditolak)**: Logika pencurian `TheftDetector` (`ALERT`, `COMMON_MOTION`, `ARM`/`DISARM`), `hp_dijaga.py`, `hp_pemilik.py`, dan `web-edge/index.html`.

## 4. Tes yang Dijalankan dan Hasil Aktual
1. **Suite Unit & Integration Tests (`unittest`)**:
   - **Perintah**: `python -m unittest discover -s tests -v`
   - **Hasil Aktual**: **27 tests ran in 0.822s — OK (27/27 PASS)**
   - **Cakupan**:
     - `test_game_engine.py` (8 test): 9 kombinasi aturan RPS, anti-duplikasi, skor +1 tepat sekali per ronde, timeout forfeit, edit username, konfirmasi reset skor.
     - `test_mqtt_simulation.py` (7 test): isolasi topik, alur 2 pemain virtual, penolakan `REJECTED_REPLAY`, `REJECTED_STALE`, `INVALID_FINITE_MOTION_RMS`, dan penegakan TLS (`ALLOW_INSECURE_LOCAL`).
     - `test_sensor_gesture.py` (8 test): kalibrasi baseline, ekstraksi `motion_rms` & `rotation_rad`, auto-discovery `termux-sensor -l` (exclude `uncalibrated`), mock `termux-vibrate`, klasifikasi `LEFT=ROCK`/`RIGHT=SCISSORS`/`UP=PAPER`, debounce anti-rebound, orientasi sumbu kustom.
     - `test_pairing_and_integration.py` (4 test): QR URI & fallback Termux bebas password, uji keamanan `/config.json` (`DO_NOT_LEAK_PASSWORD_123`), disconnect/reconnect, dan integrasi penuh HTTP server dashboard.
2. **Diagnostik Sensor Simulasi (`scripts/check_sensors.py`)**:
   - **Perintah**: `python -m scripts.check_sensors --simulate --gesture LEFT`
   - **Hasil Aktual**: `PASS` (`detected=ROCK`, `motion_rms=2.0162 m/s²`, `rotation_rad=0.4367 rad`).
3. **Status Pengujian Sensor Fisik**:
   - Semua pengujian di atas dijalankan pada PC Windows menggunakan simulator dan unit test otomatis (bukan klaim pengujian HP fisik). Pengujian sensor fisik dilakukan di Android Termux dengan `python -m scripts.check_sensors --readings 12`.

## 5. Bug atau Kendala yang Tersisa
- Tidak ada bug pada kode maupun pengujian otomatis.

## 6. Langkah Menjalankan Project
- **Jalankan Semua Tes**:
  ```powershell
  python -m unittest discover -s tests -v
  ```
- **Uji Diagnostik Sensor (Simulasi di Laptop / Nyata di Termux)**:
  ```powershell
  python -m scripts.check_sensors --simulate --gesture LEFT
  # Di Android Termux:
  python -m scripts.check_sensors --readings 12
  ```
- **Jalankan Server Game Engine & Dashboard**:
  ```powershell
  python -m src.engine.server --host 127.0.0.1 --port 8080 --session rps_kelompok_07
  ```
- **Jalankan Klien Edge di 2 HP (Termux)**:
  ```bash
  python -m src.edge.edge_client --pair-code rps_kelompok_07:PLAYER_A --debug-sensors
  python -m src.edge.edge_client --pair-code rps_kelompok_07:PLAYER_B --debug-sensors
  ```

## 7. Langkah Berikutnya yang Paling Prioritas
- Menjalankan `python -m scripts.check_sensors --readings 12` pada kedua HP Android fisik di Termux untuk memverifikasi orientasi sumbu nyata sebelum demo kelas dimulai.
