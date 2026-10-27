# PROJECT STATE — MOTION RPS IoT (QR PAIRING PLAYER A & B + AUTO-PAIRING TERMUX SELESAI)

## 1. Status Tahap
- **Tahap Selesai**:
  - **Tahap 1 — Audit Sebelum Implementasi** (Audit arsitektur MQTT, `edge_client.py`, `server.py`, dan identifikasi bahwa matriks QR sebelumnya hanya visual simulasi non-standar serta belum ada halaman `/pair` maupun jembatan pairing dinamis ke Termux)
  - **Tahap 2 — Rancangan & Implementasi QR Pairing** (Generator QR ISO/IEC 18004 murni `src/engine/qr_encoder.py`, `SessionPairingManager` dengan token sementara `pt` ber-TTL, halaman mobile `/pair` di `src/dashboard/pair.html` & `pair.js`)
  - **Tahap 3 — Hubungan Nyata Web QR dengan Termux (Pendekatan A + B)** (Penerusan sesi otomatis melalui topik MQTT `motion-rps/pairing/slot/{player_id}` & endpoint HTTP `/api/pair/assignment` + launcher 1-perintah `scripts/start_edge.sh`)
  - **Tahap 4 — Konfigurasi, Proteksi Slot, & Status Pemain** (Status bertingkat `WAITING` -> `PAIRED` -> `READY/CONNECTED`, proteksi anti-timpa slot `SLOT_ALREADY_OCCUPIED`, penolakan pemain ke-3 `SESSION_FULL`, isolasi sesi lama `SESSION_ID_MISMATCH`)
  - **Tahap 5 — Persingkat Setup Termux** (`scripts/setup_termux.sh` sekali jalan + `~/.motion_rps_edge.json` + shortcut `Termux:Widget` + launcher `scripts/start_edge.sh`)
  - **Tahap 6 — Penanganan Keluar, Background, Wake-Lock, & Reconnect** (Heartbeat periodik 2.0 detik di `edge_client.py`, deteksi `HEARTBEAT_TIMEOUT` 15 detik di `mqtt_bridge.py`, dan `termux-wake-lock` otomatis)
  - **Tahap 7 — Pengujian Otomatis & Prosedur Fisik** (`30/30 PASS` unit & integration tests)
- **Tahap Aktif**: **SELESAI (Siap Pengujian Fisik 2 HP oleh Pengguna)**

## 2. Arsitektur QR Pairing & Hubungan ke Termux
1. **Generator QR Standar ISO/IEC 18004 (`src/engine/qr_encoder.py`)**:
   - Menghasilkan matriks QR Code standar (Byte Mode, ECC Level L, Reed-Solomon GF(256)) tanpa dependency eksternal maupun CDN, sehingga kamera HP dapat memindai QR langsung dari layar laptop.
   - Isi QR hanya memuat informasi aman minimum: `http://<ip-laptop>:8080/pair?v=1&session=<session_id>&player=<A|B>&pt=<temp_token>` (tanpa password broker atau kredensial rahasia).
2. **Manajemen Sesi, Token Sementara, & Kepemilikan Slot (`SessionPairingManager` di `src/engine/pairing.py`)**:
   - Setiap sesi memiliki `pairing_token` (`pt`) sementara dengan masa berlaku (`TTL = 600 detik`).
   - Memvalidasi sesi aktif, menolak token kedaluwarsa (`EXPIRED_PAIRING_TOKEN`), menolak perebutan slot aktif oleh perangkat lain (`SLOT_ALREADY_OCCUPIED`), menolak pemain ke-3 (`SESSION_FULL`), dan mengizinkan reconnect/scan ulang dari `device_id` atau `client_token` yang sama.
3. **Jembatan Nyata Browser HP $\rightarrow$ Backend $\rightarrow$ Script Sensor Termux (Kombinasi Pendekatan A & B)**:
   - Karena browser Android berjalan di dalam sandbox yang tidak dapat mengeksekusi proses Python di Termux secara langsung, hubungan diimplementasikan melalui **mode Auto-Pairing (`--auto-pair`)**:
   - Pengguna cukup menjalankan `bash scripts/start_edge.sh` satu kali di Termux (tanpa mengetik Session ID). Script memegang `termux-wake-lock` agar tetap hidup saat Termux berpindah ke background.
   - Saat pemain melakukan Scan QR di browser HP (`/pair`) dan menekan **Join Session**, server memvalidasi token, menyimpan username, dan mem-publish pesan penugasan `ASSIGN_SESSION` ke topik MQTT `motion-rps/pairing/slot/{player_id}` (serta menyediakannya di `GET /api/pair/assignment`).
   - `EdgeDeviceClient` di Termux menerima penugasan tersebut, otomatis memanggil `reconfigure_session(new_session_id, player_id)`, dan langsung mengalirkan telemetry sensor fisik ke sesi baru tanpa perlu mengetik ulang konfigurasi.

## 3. File yang Diperiksa, Ditambahkan, dan Diubah
- `src/engine/qr_encoder.py` (**baru**: encoder QR Code ISO/IEC 18004 murni Python untuk matriks `<canvas>`)
- `src/engine/pairing.py` (**diubah**: `SessionPairingManager`, `SlotAssignment`, token sementara `pt` dengan TTL, `build_web_pair_url`, dukungan URL `/pair` pada `parse_pairing_input`)
- `src/engine/mqtt_protocol.py` (**diubah**: topik pairing dinamis `TopicBuilder.pairing_slot()` dan `TopicBuilder.pairing_device()`)
- `src/engine/mqtt_bridge.py` (**diubah**: `publish_pairing_assignment()` dan deteksi `HEARTBEAT_TIMEOUT` di `tick()`)
- `src/engine/server.py` (**diubah**: deteksi IP LAN otomatis, endpoint `/pair`, `/api/pair/join`, `/api/pair/leave`, `/api/pair/assignment`, `/api/end_session`, dan status bertingkat `WAITING` / `PAIRED` / `READY`)
- `src/edge/edge_client.py` (**diubah**: mode `--auto-pair`, `reconfigure_session()`, `handle_pairing_assignment_message()`, heartbeat periodik `send_heartbeat_if_due()`, penyimpanan `~/.motion_rps_edge.json`, dan `termux-wake-lock`)
- `src/dashboard/index.html`, `src/dashboard/app.js`, `src/dashboard/styles.css` (**diubah**: render QR ISO asli, tombol `Akhiri Sesi`, aktivasi tombol `START GAME` otomatis saat kedua pemain `READY`, dan link ke `/pair`)
- `src/dashboard/pair.html`, `src/dashboard/pair.js` (**baru**: halaman mobile pairing HP dengan pemindai kamera live, ambil foto kamera, fallback manual, konfirmasi username & slot, tombol Join/Leave, dan monitor sensor live)
- `scripts/setup_termux.sh`, `scripts/start_edge.sh` (**baru**: script setup satu kali & launcher 1-perintah Termux)
- `tests/test_pairing_and_integration.py` (**diubah**: 30 pengujian unit & integrasi end-to-end)

## 4. Hasil Pengujian
- **Pemeriksaan Sintaks**: `python -m compileall -q src scripts tests` -> **PASS (Exit code 0)**.
- **Unit & Integration Tests**: `python -m unittest discover -s tests -v` -> **Ran 30 tests in 0.838s — OK (30/30 PASS)** *(SIMULATED / AUTOMATED LOCAL TEST)*.
- **Pengujian Perangkat Fisik 2 HP Android**: **UNVERIFIED / MENUNGGU EKSEKUSI PADA 2 HP FISIK PENGGUNA**.
