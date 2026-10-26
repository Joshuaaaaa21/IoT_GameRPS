# PROJECT STATE — MOTION RPS IoT (DEBUGGING & DIAGNOSTIC UPGRADE SELESAI)

## 1. Status Tahap
- **Tahap Selesai**:
  - **Tahap 1 — Penelusuran Alur Data End-to-End** (Sensor $\rightarrow$ Classifier $\rightarrow$ MQTT $\rightarrow$ Bridge $\rightarrow$ Dashboard)
  - **Tahap 2 — Diagnostik Sensor Tanpa MQTT** (`scripts/check_sensors.py` dengan min/max/rentang & interval sampling)
  - **Tahap 3 — Perbaikan & Diagnostik Algoritma Deteksi Gerakan** (`GestureDiagnostic` trace, `allow_z_for_up`, threshold responsif `5.0 m/s²`, penggabungan chunk JSON Termux terpisah)
  - **Tahap 4 — Perbaikan Pengiriman Telemetry MQTT & Session Switching** (Live telemetry kontinu di `edge_client.py` + `switch_session()` di `mqtt_bridge.py`)
  - **Tahap 5 — Panel Live Sensor & Diagnostik di Dashboard** (`index.html`, `app.js`, `styles.css` memisahkan *Live Telemetry* vs *Pilihan Ronde*)
  - **Tahap 6 & 7 — Perbaikan Kode, Simulator, dan Uji Regresi** (`29/29 PASS`)
- **Tahap Aktif**: **SELESAI (Siap Pengujian Fisik HP oleh Pengguna)**

## 2. Akar Masalah yang Ditemukan & Diperbaiki
1. **[BROKEN -> FIXED] `EdgeDeviceClient` Hanya Mengirim Telemetry Sekali Saat Connect dan Membuang Semua Gerakan di Luar Ronde Aktif (`COUNTDOWN`)**:
   - **Bukti**: Di `src/edge/edge_client.py` versi sebelumnya, `connect_and_announce()` hanya memanggil `send_telemetry()` satu kali saat `on_connect`. Di dalam `feed_sensor_sample()`, gerakan yang terdeteksi langsung diabaikan jika `self.round_active == False` (sebelum tombol `START GAME` ditekan atau saat hanya1 HP terhubung).
   - **Perbaikan**: `feed_sensor_sample()` kini selalu memperbarui `GestureDiagnostic` dan mempublikasikan *Live Telemetry* (`accel`, `gyro`, `motion_rms`, `rotation_rad`, `diag_status`, `diag_reason`, `last_gesture`, `triggered_axis`) secara berkala (~2.5 Hz) maupun seketika saat gestur terdeteksi, meskipun ronde belum dimulai.
2. **[BROKEN -> FIXED] Dashboard Tidak Menampilkan Data Telemetry maupun Alasan Penolakan Gestur**:
   - **Bukti**: `src/dashboard/index.html` dan `app.js` sebelumnya hanya menampilkan `Pilihan: -` (yang juga disembunyikan selama `COUNTDOWN` demi privasi), sehingga pengguna tidak melihat perubahan apa pun saat menggerakkan HP.
   - **Perbaikan**: Ditambahkan **Panel Live Sensor & Diagnostik** di kartu `PLAYER_A` dan `PLAYER_B` yang menampilkan `Accel (X,Y,Z)`, `Gyro (X,Y,Z)`, `RMS`, `Rotasi`, `Gestur Terakhir`, `Sumbu Pemicu`, `Status (WAITING/DETECTED/REJECTED)`, dan `Alasan` secara real-time, terpisah dari kotak `Pilihan Ronde`.
3. **[BROKEN -> FIXED] Potensi Hilangnya Pembacaan Gyroscope & Stall pada `termux-sensor` Streaming**:
   - **Bukti**: `parse_termux_json_chunk()` sebelumnya mengembalikan `None` jika sebuah blok JSON dari `termux-sensor` hanya berisi Gyroscope tanpa Accelerometer. Selain itu, kalibrasi `limit=10` pada mode streaming bisa tertahan oleh buffering stdout Android.
   - **Perbaikan**: `parse_termux_json_chunk()` kini menyimpan state `(last_accel, last_gyro)` untuk menggabungkan chunk terpisah, dan kalibrasi menggunakan `read_single_sample` (`-n 1`) yang deterministik.
4. **[BROKEN -> FIXED] Bug Koneksi MQTT Terputus Saat Ganti Session di Dashboard**:
   - **Bukti**: `GameRuntimeContext.create_new_session()` sebelumnya membuat instance `EngineMQTTBridge` baru tanpa memindahkan `_client` MQTT yang aktif.
   - **Perbaikan**: Ditambahkan metode `EngineMQTTBridge.switch_session()` yang melakukan `unsubscribe` topik lama dan `subscribe` topik sesi baru pada klien MQTT yang sedang aktif.

## 3. File yang Diperiksa dan Diubah
- `src/edge/sensor_gesture.py` (diubah: `GestureDiagnostic`, `allow_z_for_up=True`, `accel_threshold=5.0`, stateful `parse_termux_json_chunk`, skenario simulasi `STILL` & `WEAK`)
- `scripts/check_sensors.py` (diubah: menampilkan timestamp, interval `dt`, status/alasan klasifikasi, dan tabel ringkasan statistik `min`, `max`, `range` untuk ke-6 sumbu)
- `src/edge/simulator.py` (diubah: menyertakan `accel`, `gyro`, `diag_status`, `diag_reason`, `last_gesture`, `triggered_axis` pada payload MQTT)
- `src/edge/edge_client.py` (diubah: pengiriman live telemetry kontinu, log bertahap `SENSOR_READ -> PAYLOAD_CREATED -> MQTT_PUBLISHED`, dan flag `--threshold`)
- `src/engine/mqtt_bridge.py` (diubah: metode `switch_session()` dan caching penuh metrik diagnostik per pemain)
- `src/engine/server.py` (diubah: integrasi `switch_session()` dan simulasi gerakan lemah/kuat di `/api/simulate_edge`)
- `src/dashboard/index.html`, `src/dashboard/styles.css`, `src/dashboard/app.js` (diubah: penambahan panel *Live Sensor & Diagnostik* dan tombol uji *Gerak Lemah (Ditolak)*)
- `tests/test_sensor_gesture.py`, `tests/test_pairing_and_integration.py` (diubah: 29 unit & integration tests)

## 4. Tes yang Dijalankan dan Hasil Aktual
- **Pemeriksaan Sintaks**: `python -m compileall -q src scripts tests` -> **PASS (Exit code 0)**.
- **Unit & Integration Tests**: `python -m unittest discover -s tests -v` -> **Ran 29 tests in 1.093s — OK (29/29 PASS)** *(Status: SIMULATED / AUTOMATED LOCAL TEST)*.
- **Diagnostik Simulasi Sensor**:
  - `python -m scripts.check_sensors --simulate --gesture WEAK` -> **PASS** (Status `REJECTED`, `reason=BELOW_ACCEL_THRESHOLD`).
  - `python -m scripts.check_sensors --simulate --gesture LEFT` -> **PASS** (Status `DETECTED`, `reason=DETECTED_LEFT_ROCK (-ax=9.50 >= 5.0)`, `choice=ROCK`).
- **Pengujian Perangkat Fisik Android**: **BLOCKED / MENUNGGU EKSEKUSI DI HP PENGGUNA** (tidak diklaim berhasil sebelum diuji di Termux HP nyata).

## 5. Cara Menjalankan Diagnostik di HP Android
1. Uji sensor lokal di Termux tanpa MQTT:
   ```bash
   python -m scripts.check_sensors --readings 15
   ```
2. Jalankan Server Dashboard di Laptop (dengan koneksi broker MQTT):
   ```powershell
   python -m src.engine.server --host 127.0.0.1 --port 8080 --session rps_kelompok_07 --connect-mqtt
   ```
3. Jalankan Klien di HP dengan log diagnostik lengkap:
   ```bash
   python -m src.edge.edge_client --pair-code rps_kelompok_07:PLAYER_A --debug-sensors
   ```
