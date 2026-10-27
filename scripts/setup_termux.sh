#!/usr/bin/env bash
# ==============================================================================
# Motion RPS IoT — Setup Satu Kali untuk Android Termux
# Jalankan sekali saja: bash scripts/setup_termux.sh [A|B]
# ==============================================================================
set -e

SLOT="${1:-A}"
SLOT_UPPER=$(echo "$SLOT" | tr '[:lower:]' '[:upper:]')
if [[ "$SLOT_UPPER" != "A" && "$SLOT_UPPER" != "B" && "$SLOT_UPPER" != "PLAYER_A" && "$SLOT_UPPER" != "PLAYER_B" ]]; then
    echo "[ERROR] Slot harus A atau B. Contoh: bash scripts/setup_termux.sh A"
    exit 1
fi

echo "=== [1/4] Memeriksa Lingkungan Termux & Python ==="
if ! command -v python >/dev/null 2>&1; then
    echo "[INFO] Python belum terpasang. Jalankan di Termux: pkg update && pkg install python termux-api"
    exit 1
fi

if ! command -v termux-sensor >/dev/null 2>&1; then
    echo "[WARN] 'termux-sensor' belum ditemukan di PATH."
    echo "       Pastikan aplikasi Termux:API sudah diinstal dari F-Droid dan jalankan:"
    echo "       pkg install termux-api"
else
    echo "[OK] termux-sensor tersedia."
fi

echo "=== [2/4] Memasang Dependency Python (requirements.txt) ==="
python -m pip install -r requirements.txt

echo "=== [3/4] Menyimpan Konfigurasi Default Perangkat (~/.motion_rps_edge.json) ==="
python -m src.edge.edge_client --auto-pair --player "$SLOT_UPPER" --save-config --sim-gesture STILL >/dev/null
echo "[OK] Perangkat ini dikonfigurasi sebagai Slot default: $SLOT_UPPER"

echo "=== [4/4] Membuat Shortcut Termux:Widget (~/.shortcuts/motion-rps.sh) ==="
PROJECT_DIR="$(pwd)"
mkdir -p "$HOME/.shortcuts"
cat > "$HOME/.shortcuts/motion-rps.sh" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
cd "$PROJECT_DIR" || exit 1
bash scripts/start_edge.sh "$SLOT_UPPER"
EOF
chmod +x "$HOME/.shortcuts/motion-rps.sh" scripts/start_edge.sh 2>/dev/null || true

echo ""
echo "=================================================================="
echo "✅ SETUP SELESAI! Anda TIDAK perlu menjalankan instalasi ini lagi."
echo "Untuk bermain berikutnya, cukup jalankan 1 perintah:"
echo "   bash scripts/start_edge.sh"
echo "Lalu buka browser HP dan Scan QR di Dashboard Laptop!"
echo "=================================================================="
