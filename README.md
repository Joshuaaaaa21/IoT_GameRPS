# Motion RPS IoT — Rock Paper Scissors Berbasis Sensor HP & MQTT

Permainan IoT interaktif untuk dua pemain menggunakan sensor **Accelerometer** dan **Gyroscope** pada dua HP Android (via **Termux + Termux:API**), komunikasi **MQTT over TLS**, serta **Dashboard Web** dengan satu **Game Engine Otoritatif**.

---

## 1. Arsitektur Sistem & Workflow

### Diagram Arsitektur Minimal
```text
+-----------------------+                             +-----------------------+
|      HP Android A     |                             |      HP Android B     |
|  Termux + Termux:API  |                             |  Termux + Termux:API  |
|  sensor_gesture.py    |                             |  sensor_gesture.py    |
|  edge_client.py       |                             |  edge_client.py       |
+-----------+-----------+                             +-----------+-----------+
            | Publish Telemetry (QoS 0) & Choice (QoS 1)          |
            +--------------------+           +--------------------+
                                 |           |
                                 v           v
                       +-------------------------------+
                       |     MQTT Broker (TLS 8883)    |
                       |   Isolasi Topik per Session   |
                       +---------------+---------------+
                                       ^
                                       | Subscribe & Publish State/Result (QoS 1)
                                       v
                       +-------------------------------+
                       |   Authoritative Game Engine   |
                       |  game_engine.py + server.py   |
                       |    (Single Source of Truth)   |
                       +---------------+---------------+
                                       ^
                                       | HTTP / JSON State & Commands
                                       v
                       +-------------------------------+
                       |      Dashboard Web (GUI)      |
                       |   index.html + app.js + CSS   |
                       +-------------------------------+
```

### Workflow Permainan (Alur End-to-End)
1. **Inisialisasi Sesi**: Server Dashboard (`src/engine/server.py`) membuat `session_id` unik (contoh: `rps-4f8a1c`) dan menampilkan QR Code serta kode pairing singkat untuk `PLAYER_A` dan `PLAYER_B`.
2. **Pairing Perangkat**: Kedua HP memindai QR Code atau menjalankan perintah fallback di Termux (`python -m src.edge.edge_client --pair-code rps-4f8a1c:PLAYER_A`). Secret/password broker **tidak pernah** dimasukkan ke QR code, melainkan dibaca dari `.env` lokal.
3. **Kalibrasi Sensor**: Skrip di HP mengambil sampel awal saat HP diam selama ~1 detik untuk menghitung baseline gravitasi `(bx, by, bz)`.
4. **Koneksi & Ready Gate**: Kedua HP mengirim telemetri `connected: true`. Setelah kedua pemain aktif, state Game Engine otomatis berpindah dari `WAITING_FOR_PLAYERS` ke `READY`.
5. **Start & Countdown**: Saat tombol **START GAME** ditekan di Dashboard, Game Engine menaikkan `round_id`, mengubah state ke `COUNTDOWN` (default 5 detik), dan menyiarkan state terbaru.
6. **Deteksi Gesture & Lock Pilihan**:
   - Pemain menggoyangkan HP:
     - **Goyang ke Kiri (`LEFT`)** $\rightarrow$ `ROCK`
     - **Goyang ke Kanan (`RIGHT`)** $\rightarrow$ `SCISSORS`
     - **Goyang ke Atas (`UP`)** $\rightarrow$ `PAPER`
   - `GestureClassifier` menerapkan *threshold*, *settle guard* (HP harus kembali tenang sebelum arming ulang), dan *debounce* (`1.2s`) sehingga satu ayunan tangan beserta ayunan baliknya (rebound) dijamin hanya menghasilkan **tepat satu pilihan**.
7. **Evaluasi & Anti-Skor Ganda**: Begitu kedua pilihan diterima (atau waktu countdown habis), `GameEngine` mengevaluasi pemenang, mengunci `_round_scored = True` agar skor hanya bertambah **tepat satu kali per ronde**, lalu mempublikasikan hasil ke dashboard.

---

## 2. Tabel Spesifikasi Topik MQTT

Seluruh topik diisolasi berdasarkan `session_id`: `motion-rps/{session_id}/...`

| Topik | Publisher | Subscriber | QoS | Retain | Fungsi & Contoh Payload JSON |
| :--- | :--- | :--- | :---: | :---: | :--- |
| `motion-rps/{session_id}/player/{player_id}/telemetry` | Edge HP (`PLAYER_A` / `PLAYER_B`) & Broker LWT | Game Engine | 0 (1 untuk LWT) | False | Heartbeat koneksi & status perangkat:<br>`{"msg_id":"a1b2", "session_id":"rps-01", "player_id":"PLAYER_A", "connected":true, "mode":"TERMUX_SENSOR"}` |
| `motion-rps/{session_id}/player/{player_id}/choice` | Edge HP (`PLAYER_A` / `PLAYER_B`) | Game Engine | 1 | False | Pilihan gesture pemain pada ronde aktif:<br>`{"msg_id":"c3d4", "session_id":"rps-01", "player_id":"PLAYER_A", "choice":"ROCK", "round_id":1}` |
| `motion-rps/{session_id}/game/command` | Dashboard Web | Game Engine | 1 | False | Perintah kontrol (`START_GAME`, `RESET_SCORE`, `SET_USERNAME`):<br>`{"session_id":"rps-01", "command":"START_GAME"}` |
| `motion-rps/{session_id}/game/state` | Game Engine | Dashboard & Edge HP | 1 | True | Snapshot otoritatif state permainan, koneksi, countdown, dan skor |
| `motion-rps/{session_id}/game/result` | Game Engine | Dashboard Web | 1 | False | Hasil akhir ronde (`PLAYER_A_WIN`, `PLAYER_B_WIN`, `DRAW`, `FORFEIT_A/B`, `VOID_NO_MOVES`) |

---

## 3. Konfigurasi Sensor & Cara Menguji Gesture

Orientasi sumbu sensor dapat berbeda antar merek HP Android. Secara default (`src/edge/sensor_gesture.py` — `GestureConfig`), saat HP dipegang vertikal (portrait):
- **Goyang Kiri (`ROCK`)**: lonjakan akselerasi negatif pada sumbu X (`left_axis="ax", left_sign=-1`).
- **Goyang Kanan (`SCISSORS`)**: lonjakan akselerasi positif pada sumbu X (`right_axis="ax", right_sign=+1`).
- **Goyang Atas (`PAPER`)**: lonjakan akselerasi positif pada sumbu Y (`up_axis="ay", up_sign=+1`).

Jika sumbu X pada HP Anda terbalik, jalankan `edge_client.py` dengan flag `--invert-x` atau ubah parameter `GestureConfig`.

### Uji Cepat Satu Gesture (Tanpa HP Fisik)
```powershell
python -m scripts.check_sensors --simulate --gesture LEFT
python -m src.edge.edge_client --pair-code rps-demo:PLAYER_A --sim-gesture LEFT
python -m src.edge.edge_client --pair-code rps-demo:PLAYER_A --sim-gesture RIGHT
python -m src.edge.edge_client --pair-code rps-demo:PLAYER_A --sim-gesture UP
```

### Diagnostik Sensor di Termux (Diadaptasi dari Referensi PocketGuard IoT)
Sebelum menghubungkan ke broker MQTT, uji sensor fisik di kedua HP Android menggunakan:
```bash
python -m scripts.check_sensors --readings 12
```
Skrip ini otomatis mendeteksi nama sensor terkalibrasi lewat `termux-sensor -l` (mengabaikan `uncalibrated`), menghitung `motion_rms` dan `rotation_rad`, serta menampilkan hasil deteksi arah (`ROCK`, `SCISSORS`, `PAPER`). Untuk mencetak nilai mentah saat bermain, tambahkan flag `--debug-sensors` pada `edge_client.py`.

---

## 4. Langkah Demo & Cara Menjalankan Proyek

### A. Persiapan Environment
```powershell
pip install -r requirements.txt
Copy-Item .env.example .env
# Edit .env jika ingin menghubungkan ke HiveMQ Cloud (TLS port 8883)
```

### B. Menjalankan Dashboard & Game Engine Server
```powershell
# Mode lokal + simulasi (tanpa broker eksternal):
python -m src.engine.server --host 127.0.0.1 --port 8080 --session rps-demo01

# Mode terkoneksi ke Broker MQTT (sesuai .env):
python -m src.engine.server --host 0.0.0.0 --port 8080 --session rps-demo01 --connect-mqtt
```
Buka browser di **`http://127.0.0.1:8080`**.
- Di dalam Dashboard tersedia panel **Simulasi Cepat Edge A & Edge B** sehingga Anda dapat langsung mendemokan `Connect`, `START GAME`, `Goyang Kiri/Atas/Kanan`, dan `RESET SCORE` langsung dari browser.

### C. Menjalankan Klien di 2 HP Android (Termux)
1. Instal aplikasi **Termux** dan **Termux:API** dari F-Droid, lalu di dalam Termux jalankan:
   ```bash
   pkg update && pkg install termux-api python git
   pip install paho-mqtt
   ```
2. Salin kode proyek dan file `.env` ke kedua HP.
3. Salin perintah pairing dari Dashboard:
   - **HP 1 (Player A)**:
     ```bash
     python -m src.edge.edge_client --pair-code rps-demo01:PLAYER_A
     ```
   - **HP 2 (Player B)**:
     ```bash
     python -m src.edge.edge_client --pair-code rps-demo01:PLAYER_B
     ```
4. Jika diuji di laptop tanpa sensor Termux, tambahkan `--simulate` untuk memakai input interaktif (`LEFT`, `RIGHT`, `UP`).

---

## 5. Hasil Pengujian (Otomatis vs Simulasi vs Perangkat Nyata)

Jalankan seluruh suite pengujian dengan perintah:
```powershell
python -m unittest discover -s tests -v
```

| Kategori Pengujian | Cakupan | Status | Detail Verifikasi |
| :--- | :--- | :---: | :--- |
| **1. Tes Otomatis (Unit Tests)** | `tests/test_game_engine.py`<br>`tests/test_sensor_gesture.py`<br>`tests/test_pairing_and_integration.py` | **PASS (27/27 test)** | Seluruh 9 aturan kombinasi RPS, anti-duplikasi pilihan, jaminan skor +1 tepat sekali per ronde, timeout forfeit, ekstraksi `motion_rms` & `rotation_rad`, auto-discovery `termux-sensor -l`, anti-replay `(boot_id, sequence)`, cek `sent_at_ms`, proteksi `/config.json` & QR code. |
| **2. Tes Simulasi End-to-End** | `tests/test_mqtt_simulation.py`<br>`src/edge/simulator.py`<br>HTTP Dashboard Simulation API | **PASS** | Dua pemain virtual (`PLAYER_A` & `PLAYER_B`) berkomunikasi lewat `EngineMQTTBridge` dan server HTTP Dashboard (`ThreadingHTTPServer`) dari state `WAITING_FOR_PLAYERS` $\rightarrow$ `READY` $\rightarrow$ `COUNTDOWN` $\rightarrow$ `FINISHED` dan `RESET_SCORE`. |
| **3. Pengujian Perangkat Nyata (Android Termux)** | `TermuxSensorReader.stream_termux_sensors()` | **BELUM DIUJI PADA HP FISIK DI WORKSPACE INI** | Lingkungan eksekusi saat ini adalah Windows PC (`termux-sensor` binary tidak tersedia di Windows). Parser JSON `termux-sensor` telah diverifikasi melalui unit test, dan deteksi ketersediaan sensor menggunakan `shutil.which("termux-sensor")` telah diterapkan secara jujur (otomatis fallback ke mode simulasi bila dijalankan di PC). |
#   t e s t _ i o t _ g a m e s r p s  
 