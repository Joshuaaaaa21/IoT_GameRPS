# Motion RPS IoT — Rock Paper Scissors Berbasis Sensor HP, QR Pairing & MQTT

Permainan IoT interaktif untuk dua pemain menggunakan sensor **Accelerometer** dan **Gyroscope** pada dua HP Android (via **Termux + Termux:API**), fitur **QR Code Pairing (`PLAYER_A` & `PLAYER_B`)**, komunikasi **MQTT over TLS**, serta **Dashboard Web** dengan satu **Game Engine Otoritatif**.

---

## 1. Arsitektur Sistem & Alur QR Pairing

### Diagram Arsitektur
```text
+---------------------------+                         +---------------------------+
|        HP Android A       |                         |        HP Android B       |
| 1. Browser: /pair Scan QR |                         | 1. Browser: /pair Scan QR |
| 2. Termux: start_edge.sh  |                         | 2. Termux: start_edge.sh  |
+-------------+-------------+                         +-------------+-------------+
              |  Join Session (HTTP /api/pair/join)                 |
              |  & Live Telemetry/Choice (MQTT)                     |
              +--------------------+           +--------------------+
                                   |           |
                                   v           v
                         +-------------------------------+
                         |     MQTT Broker (TLS 8883)    |
                         |  + Topik Pairing Dinamis      |
                         +---------------+---------------+
                                         ^
                                         | Subscribe & Publish State/Result/Assignment
                                         v
                         +-------------------------------+
                         |   Authoritative Game Engine   |
                         |  game_engine.py + server.py   |
                         |    (Single Source of Truth)   |
                         +---------------+---------------+
                                         ^
                                         | HTTP / JSON State & QR Matrix
                                         v
                         +-------------------------------+
                         |    Dashboard Laptop & /pair   |
                         |  index.html + pair.html + JS  |
                         +-------------------------------+
```

### Alur QR Pairing Sampai Sensor Terhubung (Tanpa Ketik Session ID Berulang)
1. **Buat Sesi & Tampilkan QR**: Dashboard laptop (`http://127.0.0.1:8080`) menampilkan `session_id` unik serta **QR Code ISO/IEC 18004 asli** (baik QR Sesi Umum maupun QR spesifik `PLAYER_A` & `PLAYER_B`). QR hanya memuat versi (`v=1`), URL `/pair`, `session`, `player`, dan token sementara (`pt` ber-TTL 10 menit) — **tanpa password broker atau kredensial rahasia**.
2. **Launcher 1-Perintah di Termux**: Di masing-masing HP, pengguna cukup menjalankan `bash scripts/start_edge.sh` satu kali. Script mengaktifkan `termux-wake-lock` dan menunggu penugasan sesi (`--auto-pair`).
3. **Scan QR & Join di Browser HP**:
   - Pemain A dan Pemain B memindai QR Code dari layar laptop (menggunakan aplikasi Kamera HP atau tombol **📷 Scan QR** / **📸 Ambil Foto QR** di halaman `http://<ip-laptop>:8080/pair`).
   - Pemain mengonfirmasi **Username** dan **Slot (`PLAYER_A` / `PLAYER_B` / `Otomatis`)**, lalu menekan **✅ Join Session**.
4. **Penugasan Sesi Otomatis ke Termux**:
   - Backend memvalidasi token sementara & kepemilikan slot (`SessionPairingManager`), menyimpan username di `GameEngine`, lalu mem-publish perintah `ASSIGN_SESSION` ke topik MQTT `motion-rps/pairing/slot/{player_id}` (serta menyediakannya di `GET /api/pair/assignment`).
   - Script `edge_client.py` di Termux otomatis berpindah (`reconfigure_session`) ke `session_id` tersebut dan mulai mengirimkan *live telemetry* sensor fisik.
5. **Validasi Status Online & Mulai Permainan**:
   - Dashboard membedakan status `WAITING` (belum join), `PAIRED (Menunggu Sensor Termux)` (baru scan QR di web), dan `READY / CONNECTED (Sensor Online)` (telemetry/heartbeat sensor aktual telah masuk).
   - Tombol **START GAME** otomatis aktif ketika kedua pemain berstatus `READY / CONNECTED`.

---

## 2. Tabel Spesifikasi Topik MQTT

| Topik | Publisher | Subscriber | QoS | Retain | Fungsi |
| :--- | :--- | :--- | :---: | :---: | :--- |
| `motion-rps/{session_id}/player/{player_id}/telemetry` | Edge HP (`PLAYER_A` / `PLAYER_B`) & LWT | Game Engine | 0 (1 LWT) | False | Heartbeat periodik (2.0s), akselerometer, giroskop, & status diagnostik |
| `motion-rps/{session_id}/player/{player_id}/choice` | Edge HP (`PLAYER_A` / `PLAYER_B`) | Game Engine | 1 | False | Pilihan gerakan pemain (`ROCK`, `PAPER`, `SCISSORS`) saat `COUNTDOWN` |
| `motion-rps/{session_id}/game/command` | Dashboard Web | Game Engine | 1 | False | Perintah kontrol (`START_GAME`, `RESET_SCORE`, `SET_USERNAME`) |
| `motion-rps/{session_id}/game/state` | Game Engine | Dashboard & Edge HP | 1 | True | Snapshot otoritatif state permainan, koneksi, countdown, dan skor |
| `motion-rps/{session_id}/game/result` | Game Engine | Dashboard Web | 1 | False | Hasil akhir ronde (`PLAYER_A_WIN`, `PLAYER_B_WIN`, `DRAW`, `FORFEIT_A/B`, `VOID_NO_MOVES`) |
| `motion-rps/pairing/slot/{player_id}` | Server / Bridge | Edge HP (`--auto-pair`) | 1 | True | Penugasan sesi dinamis (`ASSIGN_SESSION` / `LEAVE_SESSION`) hasil QR pairing |
| `motion-rps/pairing/device/{device_id}` | Server / Bridge | Edge HP (`--auto-pair`) | 1 | True | Penugasan sesi dinamis spesifik per identitas perangkat (`device_id`) |

---

## 3. Setup Termux Satu Kali & Penggunaan Harian

### A. Setup Pertama Kali di Termux (Cukup Sekali Saja)
Instal aplikasi **Termux** dan **Termux:API** dari F-Droid, salin folder proyek beserta `.env`, lalu jalankan script setup satu kali:
```bash
# Di HP A (disimpan sebagai PLAYER_A di ~/.motion_rps_edge.json):
bash scripts/setup_termux.sh A

# Di HP B (disimpan sebagai PLAYER_B di ~/.motion_rps_edge.json):
bash scripts/setup_termux.sh B
```
Script ini memeriksa `termux-sensor`, memasang dependency `requirements.txt`, menyimpan konfigurasi slot default, dan membuat shortcut `~/.shortcuts/motion-rps.sh` untuk **Termux:Widget**.

### B. Menjalankan Permainan pada Penggunaan Berikutnya
1. **Di Laptop**:
   ```powershell
   python -m src.engine.server --host 0.0.0.0 --port 8080 --connect-mqtt
   ```
   Buka `http://127.0.0.1:8080` di browser laptop.
2. **Di Termux HP A & HP B** (cukup 1 perintah, tanpa mengetik Session ID):
   ```bash
   bash scripts/start_edge.sh
   ```
   *(Atau ketuk shortcut `motion-rps.sh` dari widget homescreen Termux:Widget).*
3. **Di Browser HP A & HP B**:
   - Scan QR Code di layar laptop atau buka `http://<ip-laptop>:8080/pair`.
   - Masukkan Username, pastikan slot sesuai (`PLAYER_A` / `PLAYER_B`), lalu tekan **✅ Join Session**.
   - Untuk menghentikan sensor di Termux kapan saja, tekan **`Ctrl+C`** (otomatis melepas `termux-wake-lock` dan mengirim status `OFFLINE`).

---

## 4. Perilaku Background, Reconnect, & Troubleshooting

| Situasi | Perilaku Aktual Sistem & Solusi |
| :--- | :--- |
| **Browser HP ditutup setelah Join** | Sensor di Termux **tetap berjalan dan terhubung** karena komunikasi telemetry dilakukan oleh proses `edge_client.py` di Termux, bukan oleh tab browser. |
| **Termux berpindah ke background / HP terkunci** | `start_edge.sh` dan `edge_client.py` mengaktifkan `termux-wake-lock` agar CPU tidak tidur saat Anda membuka browser HP atau mengunci layar. Pastikan optimasi baterai Android untuk **Termux** dan **Termux:API** diatur ke *Unrestricted / Jangan Optimalkan*. |
| **Termux ditutup paksa (Force Stop / Swipe Kill)** | Proses Python berhenti. Dashboard akan mengubah status pemain menjadi `OFFLINE` (melalui MQTT LWT atau `HEARTBEAT_TIMEOUT` 15 detik). Buka kembali Termux dan jalankan `bash scripts/start_edge.sh` untuk reconnect otomatis tanpa kehilangan skor sesi. |
| **Kamera live diblokir browser pada HTTP LAN** | Browser modern membatasi live video `getUserMedia` pada koneksi non-HTTPS (`http://192.168.x.x`). Gunakan tombol **📸 Ambil Foto QR (Kamera HP)**, scan langsung dari aplikasi Kamera bawaan HP, atau gunakan tombol **🔄 Ambil Sesi Aktif Server** / input manual di `/pair`. |
| **Scan QR dua kali / Refresh halaman** | Sistem menyimpan `device_id` dan `client_token` di browser sehingga scan ulang oleh HP yang sama dikenali sebagai reconnect idempoten tanpa menolak pemain atau mereset skor. |
| **Pemain ke-3 mencoba masuk** | Ditolak secara otomatis dengan status `SESSION_FULL` (atau `SLOT_ALREADY_OCCUPIED` jika mencoba mengambil alih slot pemain aktif). |

---

## 5. Pengujian Otomatis

Jalankan seluruh pengujian unit & integrasi (30 test mencakup pembuatan sesi, matriks QR ISO, validasi token sementara, kedaluwarsa QR, penolakan pemain ke-3, anti-bajakan slot, dynamic pairing MQTT ke Termux, heartbeat timeout, reconnect, dan isolasi sesi lama):
```powershell
python -m unittest discover -s tests -v
```
