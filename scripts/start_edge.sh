#!/usr/bin/env bash
# ==============================================================================
# Motion RPS IoT — Launcher Sensor 1-Perintah (Auto-Pairing)
# Penggunaan:
#   bash scripts/start_edge.sh          (menggunakan slot tersimpan di ~/.motion_rps_edge.json)
#   bash scripts/start_edge.sh A        (sebagai PLAYER_A)
#   bash scripts/start_edge.sh B        (sebagai PLAYER_B)
# ==============================================================================

SLOT_ARG="${1:-}"
SERVER_URL_ARG="${2:-}"

cleanup() {
    echo ""
    echo "[SHUTDOWN] Menghentikan sensor dan melepas wake-lock..."
    if command -v termux-wake-unlock >/dev/null 2>&1; then
        termux-wake-unlock || true
    fi
}
trap cleanup EXIT INT TERM

echo "=== Motion RPS IoT — Memulai Edge Sensor (Auto-Pairing) ==="

if ! command -v python >/dev/null 2>&1; then
    echo "[ERROR] Python tidak ditemukan. Jalankan: pkg install python"
    exit 1
fi

if command -v termux-sensor >/dev/null 2>&1; then
    echo "[OK] termux-sensor terdeteksi (Mode Sensor Fisik Android)."
    if command -v termux-wake-lock >/dev/null 2>&1; then
        termux-wake-lock || true
        echo "[OK] termux-wake-lock aktif (Termux tetap berjalan saat Anda buka browser untuk Scan QR)."
    fi
else
    echo "[WARN] termux-sensor tidak ditemukan! Beralih ke Mode Simulasi."
    echo "       Jika di HP Android, pastikan aplikasi Termux:API terpasang dan jalankan: pkg install termux-api"
fi

CMD=(python -m src.edge.edge_client --auto-pair)
if [[ -n "$SLOT_ARG" ]]; then
    CMD+=(--player "$SLOT_ARG")
fi
if [[ -n "$SERVER_URL_ARG" ]]; then
    CMD+=(--server-url "$SERVER_URL_ARG")
fi

echo "[INFO] Menjalankan: ${CMD[*]}"
echo "[INFO] Tekan Ctrl+C kapan saja untuk menghentikan script dengan aman."
echo "------------------------------------------------------------------"
"${CMD[@]}"
