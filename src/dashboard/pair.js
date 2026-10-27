// Mobile QR Pairing & Player Controller for Motion RPS IoT (/pair)

const STORAGE_KEY_DEVICE = "motion_rps_device_id";
const STORAGE_KEY_CLIENT_TOKEN = "motion_rps_client_token";
const STORAGE_KEY_SLOT = "motion_rps_assigned_slot";
const STORAGE_KEY_SESSION = "motion_rps_assigned_session";
const STORAGE_KEY_USERNAME = "motion_rps_username";

let currentPairingToken = null;
let assignedSlot = localStorage.getItem(STORAGE_KEY_SLOT) || null;
let assignedSession = localStorage.getItem(STORAGE_KEY_SESSION) || null;
let videoStream = null;
let scanTimer = null;

function getOrCreateDeviceId() {
    let devId = localStorage.getItem(STORAGE_KEY_DEVICE);
    if (!devId) {
        const rand = Math.random().toString(16).substring(2, 10);
        devId = `dev-${rand}`;
        localStorage.setItem(STORAGE_KEY_DEVICE, devId);
    }
    return devId;
}

function showFeedback(message, isError = false) {
    const banner = document.getElementById("pair-feedback-banner");
    if (!banner) return;
    banner.hidden = false;
    banner.textContent = message;
    banner.className = "status-banner " + (isError ? "error-text" : "");
}

function normalizeSlotOption(raw) {
    if (!raw) return "AUTO";
    const up = String(raw).trim().toUpperCase();
    if (up === "A" || up === "1" || up === "PLAYER_A") return "PLAYER_A";
    if (up === "B" || up === "2" || up === "PLAYER_B") return "PLAYER_B";
    return "AUTO";
}

/**
 * Parse a scanned QR string (URL, motionrps:// URI, or compact session:slot code)
 * and populate the form fields.
 */
function applyScannedQrText(rawText, sourceLabel = "QR Scan") {
    const cleaned = (rawText || "").trim();
    if (!cleaned) return false;

    let session = "";
    let slot = "AUTO";
    let pt = null;

    try {
        if (cleaned.startsWith("http://") || cleaned.startsWith("https://") || cleaned.startsWith("motionrps://") || cleaned.includes("session=")) {
            const urlObj = new URL(cleaned.includes("://") ? cleaned : `http://localhost/pair?${cleaned}`);
            session = (urlObj.searchParams.get("session") || urlObj.searchParams.get("session_id") || "").trim();
            const rawSlot = urlObj.searchParams.get("player") || urlObj.searchParams.get("slot") || "";
            slot = normalizeSlotOption(rawSlot);
            pt = urlObj.searchParams.get("pt") || urlObj.searchParams.get("pairing_token") || null;
        } else if (cleaned.includes(":")) {
            const parts = cleaned.split(":");
            session = parts[0].trim();
            slot = normalizeSlotOption(parts[1]);
        } else {
            session = cleaned;
        }
    } catch (e) {
        session = cleaned;
    }

    if (!session) {
        showFeedback("QR tidak berisi Session ID yang valid.", true);
        return false;
    }

    currentPairingToken = pt;
    document.getElementById("input-session-code").value = session;
    if (slot !== "AUTO") {
        document.getElementById("select-player-slot").value = slot;
    }

    const resText = document.getElementById("scan-result-text");
    const resBadge = document.getElementById("scan-status-badge");
    resText.textContent = `${session} (${slot === "AUTO" ? "Slot Otomatis" : slot})`;
    resBadge.textContent = "QR OK";
    resBadge.className = "badge online";

    showFeedback(`${sourceLabel} berhasil membaca sesi '${session}'. Silakan konfirmasi username dan klik Join Session.`);
    return true;
}

async function stopCameraScanner() {
    if (scanTimer) {
        clearInterval(scanTimer);
        scanTimer = null;
    }
    if (videoStream) {
        videoStream.getTracks().forEach((t) => t.stop());
        videoStream = null;
    }
    const camBox = document.getElementById("camera-container");
    const stopBtn = document.getElementById("btn-stop-camera");
    if (camBox) camBox.hidden = true;
    if (stopBtn) stopBtn.hidden = true;
}

async function startLiveCameraScanner() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        showFeedback(
            "Kamera live dibatasi oleh browser pada koneksi HTTP non-HTTPS. Gunakan tombol '📸 Ambil Foto QR' atau scan dari aplikasi Kamera bawaan HP.",
            true
        );
        return;
    }

    try {
        videoStream = await navigator.mediaDevices.getUserMedia({
            video: { facingMode: "environment" }
        });
        const video = document.getElementById("qr-video");
        const camBox = document.getElementById("camera-container");
        const stopBtn = document.getElementById("btn-stop-camera");
        camBox.hidden = false;
        stopBtn.hidden = false;
        video.srcObject = videoStream;
        await video.play();

        if ("BarcodeDetector" in window) {
            const detector = new window.BarcodeDetector({ formats: ["qr_code"] });
            scanTimer = setInterval(async () => {
                if (!videoStream || video.readyState < 2) return;
                try {
                    const barcodes = await detector.detect(video);
                    if (barcodes && barcodes.length > 0 && barcodes[0].rawValue) {
                        await stopCameraScanner();
                        applyScannedQrText(barcodes[0].rawValue, "Kamera Live");
                    }
                } catch (err) {
                    // Ignore transient frame errors
                }
            }, 300);
        } else {
            document.getElementById("camera-hint").textContent =
                "Browser ini tidak mendukung BarcodeDetector native. Gunakan aplikasi Kamera bawaan HP untuk memindai QR atau klik '🔄 Ambil Sesi Aktif Server'.";
        }
    } catch (err) {
        showFeedback("Izin kamera ditolak atau tidak tersedia: " + err.message, true);
    }
}

async function handleQrPhotoInput(event) {
    const file = event.target.files && event.target.files[0];
    if (!file) return;

    if ("BarcodeDetector" in window) {
        try {
            const bitmap = await createImageBitmap(file);
            const detector = new window.BarcodeDetector({ formats: ["qr_code"] });
            const codes = await detector.detect(bitmap);
            if (codes && codes.length > 0 && codes[0].rawValue) {
                applyScannedQrText(codes[0].rawValue, "Foto QR");
                return;
            }
            showFeedback("QR Code tidak terdeteksi di dalam foto. Mengambil sesi aktif dari server sebagai fallback...", true);
        } catch (err) {
            // Fall through to active session fetch
        }
    }
    await fetchActiveSessionFromServer("Fallback Kamera");
}

async function fetchActiveSessionFromServer(sourceLabel = "Server") {
    try {
        const res = await fetch("/api/state");
        const data = await res.json();
        if (data && data.pairing && data.pairing.session_pair_url) {
            applyScannedQrText(data.pairing.session_pair_url, sourceLabel);
        } else if (data && data.session_id) {
            applyScannedQrText(data.session_id, sourceLabel);
        }
    } catch (err) {
        showFeedback("Gagal mengambil sesi dari server.", true);
    }
}

async function joinSession() {
    const rawCode = document.getElementById("input-session-code").value.trim();
    const username = document.getElementById("input-pair-username").value.trim();
    const slotChoice = document.getElementById("select-player-slot").value;

    if (!rawCode) {
        showFeedback("Masukkan atau scan Session ID terlebih dahulu!", true);
        return;
    }

    let sessionId = rawCode;
    let qrInput = null;
    if (rawCode.includes("://") || rawCode.includes("session=") || rawCode.includes(":")) {
        qrInput = rawCode;
    }

    const payload = {
        session_id: sessionId,
        qr_input: qrInput,
        player_id: slotChoice,
        username: username || undefined,
        pairing_token: currentPairingToken,
        device_id: getOrCreateDeviceId(),
        client_token: localStorage.getItem(STORAGE_KEY_CLIENT_TOKEN) || undefined
    };

    try {
        const res = await fetch("/api/pair/join", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (!data.ok) {
            showFeedback(`Gagal bergabung: ${data.reason || data.error}`, true);
            return;
        }

        assignedSlot = data.player_id;
        assignedSession = data.session_id;
        localStorage.setItem(STORAGE_KEY_SLOT, assignedSlot);
        localStorage.setItem(STORAGE_KEY_SESSION, assignedSession);
        if (data.client_token) {
            localStorage.setItem(STORAGE_KEY_CLIENT_TOKEN, data.client_token);
        }
        if (data.username) {
            localStorage.setItem(STORAGE_KEY_USERNAME, data.username);
            document.getElementById("input-pair-username").value = data.username;
        }
        document.getElementById("select-player-slot").value = assignedSlot;
        document.getElementById("btn-leave-session").disabled = false;

        if (data.auto_termux_cmd) {
            document.getElementById("mobile-auto-cmd").textContent = data.auto_termux_cmd;
        }
        if (data.termux_cmd) {
            document.getElementById("mobile-direct-cmd").textContent = data.termux_cmd;
        }

        showFeedback(`Berhasil bergabung ke sesi '${assignedSession}' sebagai ${assignedSlot} (${data.username})!`);
        await refreshMobileStatus();
    } catch (err) {
        showFeedback("Gagal menghubungi server untuk Join Session.", true);
    }
}

async function leaveSession() {
    if (!assignedSlot) return;
    try {
        const res = await fetch("/api/pair/leave", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                player_id: assignedSlot,
                device_id: getOrCreateDeviceId(),
                client_token: localStorage.getItem(STORAGE_KEY_CLIENT_TOKEN) || undefined
            })
        });
        const data = await res.json();
        if (data.ok) {
            showFeedback(`Berhasil keluar (disconnect) dari slot ${assignedSlot}.`);
            assignedSlot = null;
            localStorage.removeItem(STORAGE_KEY_SLOT);
            document.getElementById("btn-leave-session").disabled = true;
            await refreshMobileStatus();
        } else {
            showFeedback(`Gagal keluar sesi: ${data.reason}`, true);
        }
    } catch (err) {
        showFeedback("Gagal menghubungi server untuk Leave Session.", true);
    }
}

async function refreshMobileStatus() {
    try {
        const res = await fetch("/api/state");
        const data = await res.json();

        document.getElementById("mobile-game-state").textContent = data.state;
        document.getElementById("mobile-round-id").textContent = data.round_id;

        const targetSlot = assignedSlot || (
            document.getElementById("select-player-slot").value !== "AUTO"
                ? document.getElementById("select-player-slot").value
                : null
        );

        document.getElementById("assigned-slot-display").textContent = targetSlot || "Belum dipilih";
        document.getElementById("assigned-session-display").textContent = data.session_id;

        if (targetSlot && data.players && data.players[targetSlot]) {
            const p = data.players[targetSlot];
            const webEl = document.getElementById("web-pair-status");
            if (p.web_paired) {
                webEl.textContent = `PAIRED (${p.username})`;
                webEl.className = "badge online";
                document.getElementById("btn-leave-session").disabled = false;
            } else {
                webEl.textContent = "BELUM JOIN WEB";
                webEl.className = "badge offline";
            }

            const sensorEl = document.getElementById("sensor-conn-status");
            if (p.connected) {
                sensorEl.textContent = "READY / ONLINE (Telemetry Sensor Aktif)";
                sensorEl.className = "badge online";
            } else {
                sensorEl.textContent = "MENUNGGU SENSOR TERMUX";
                sensorEl.className = "badge warn";
            }

            document.getElementById("mobile-score").textContent = p.score;
            document.getElementById("mobile-last-gesture").textContent = p.last_gesture || "-";
            document.getElementById("mobile-round-choice").textContent =
                p.choice ? p.choice : (p.has_chosen ? "LOCKED" : "-");
            document.getElementById("mobile-accel").textContent =
                Array.isArray(p.accel) ? p.accel.map((v) => Number(v || 0).toFixed(2)).join(", ") : "0.00, 0.00, 0.00";
            document.getElementById("mobile-diag-reason").textContent = p.diag_reason || "NO_TELEMETRY_YET";

            const shortSlot = targetSlot === "PLAYER_A" ? "A" : "B";
            document.getElementById("mobile-auto-cmd").textContent = `bash scripts/start_edge.sh ${shortSlot}`;
            if (data.pairing && data.pairing[targetSlot]) {
                document.getElementById("mobile-direct-cmd").textContent = data.pairing[targetSlot].termux_cmd;
            }
        }
    } catch (err) {
        // Ignore transient network poll error
    }
}

function copyTextFromElement(elId) {
    const el = document.getElementById(elId);
    if (!el || !navigator.clipboard) return;
    navigator.clipboard.writeText(el.textContent.trim());
    showFeedback("Perintah Termux berhasil disalin ke clipboard HP!");
}

// Initialize page on load
(function initPairPage() {
    getOrCreateDeviceId();
    const savedName = localStorage.getItem(STORAGE_KEY_USERNAME);
    if (savedName) {
        document.getElementById("input-pair-username").value = savedName;
    }

    // Check if opened directly from QR Code URL (?v=1&session=...&player=...&pt=...)
    const params = new URLSearchParams(window.location.search);
    if (params.has("session") || params.has("session_id")) {
        applyScannedQrText(window.location.href, "URL QR Kamera");
    } else {
        fetchActiveSessionFromServer("Auto-Detect Server");
    }

    document.getElementById("btn-scan-qr").addEventListener("click", startLiveCameraScanner);
    document.getElementById("btn-stop-camera").addEventListener("click", stopCameraScanner);
    document.getElementById("input-qr-photo").addEventListener("change", handleQrPhotoInput);
    document.getElementById("btn-fetch-active-session").addEventListener("click", () => {
        fetchActiveSessionFromServer("Server Aktif");
    });
    document.getElementById("btn-join-session").addEventListener("click", joinSession);
    document.getElementById("btn-leave-session").addEventListener("click", leaveSession);
    document.getElementById("btn-copy-auto-cmd").addEventListener("click", () => copyTextFromElement("mobile-auto-cmd"));
    document.getElementById("btn-copy-direct-cmd").addEventListener("click", () => copyTextFromElement("mobile-direct-cmd"));

    refreshMobileStatus();
    setInterval(refreshMobileStatus, 500);
})();
