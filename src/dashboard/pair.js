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

function displaySlot(slot) {
    if (slot === "PLAYER_A") return "Player A";
    if (slot === "PLAYER_B") return "Player B";
    return slot || "-";
}

function displayGameState(state) {
    const labels = {
        WAITING_FOR_PLAYERS: "Waiting for players",
        READY: "Ready to play",
        COUNTDOWN: "Get ready...",
        FINISHED: "Round finished"
    };
    return labels[state] || state;
}

function displayPairingError(reason) {
    const messages = {
        SESSION_ENDED: "This session has ended. Ask the host to start a new one.",
        EXPIRED_PAIRING_TOKEN: "This QR code has expired. Scan the latest code.",
        INVALID_PAIRING_TOKEN: "This QR code is no longer valid. Scan the latest code.",
        SLOT_ALREADY_OCCUPIED: "That player slot is already taken. Choose the other player."
    };
    return messages[reason] || String(reason || "Unknown error").replace(/_/g, " ").toLowerCase();
}

function displayGesture(gesture) {
    const labels = { LEFT: "Left", RIGHT: "Right", UP: "Up", WEAK: "Weak", STILL: "Still" };
    return labels[gesture] || gesture || "-";
}

function renderMoveDisplay(element, choice) {
    if (!element) return;
    const normalized = String(choice || "").toUpperCase();
    const moves = {
        ROCK: ["move-rock", "Rock"],
        PAPER: ["move-paper", "Paper"],
        SCISSORS: ["move-scissors", "Scissors"]
    };
    const move = moves[normalized];
    element.replaceChildren();

    if (!move) {
        element.textContent = normalized === "LOCKED" ? "Locked in" : (choice || "-");
        return;
    }

    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "move-art");
    svg.setAttribute("viewBox", "0 0 64 64");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", move[1]);
    const symbol = document.getElementById(move[0]);
    if (!symbol) {
        element.textContent = move[1];
        return;
    }
    for (const shape of symbol.children) {
        svg.append(shape.cloneNode(true));
    }

    const label = document.createElement("span");
    label.textContent = move[1];
    element.append(svg, label);
}

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

function updateActiveSessionQr(data) {
    const message = document.getElementById("active-session-qr-message");
    const wrapper = document.getElementById("active-session-qr-wrap");
    const canvas = document.getElementById("qr-canvas-active-session");
    const sessionId = document.getElementById("active-session-qr-id");
    const pairing = data && data.pairing;
    const matrix = pairing && pairing.session_qr_matrix;
    const active = data && data.session_active !== false;
    const hasQr = active && Array.isArray(matrix) && matrix.length > 0;

    wrapper.hidden = !hasQr;
    sessionId.hidden = !hasQr;
    sessionId.textContent = hasQr ? `Game code: ${data.session_id}` : "";
    if (!hasQr) {
        canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);
        delete canvas.dataset.sessionId;
        message.textContent = active
            ? "The game QR is not available yet. Try again in a moment."
            : "This game has ended. Ask the host to start a new session.";
        return;
    }

    message.textContent = "Let another phone scan this code to join the same game.";
    if (canvas.dataset.sessionId === data.session_id) return;
    canvas.dataset.sessionId = data.session_id;
    const ctx = canvas.getContext("2d");
    const size = canvas.width;
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, size, size);

    const quietZone = 4;
    const modules = matrix.length;
    const cellSize = Math.floor(size / (modules + quietZone * 2));
    const offset = Math.floor((size - cellSize * modules) / 2);
    ctx.fillStyle = "#211b27";
    for (let row = 0; row < modules; row++) {
        for (let column = 0; column < modules; column++) {
            if (matrix[row][column]) {
                ctx.fillRect(
                    offset + column * cellSize,
                    offset + row * cellSize,
                    cellSize,
                    cellSize
                );
            }
        }
    }
}

/**
 * Parse a scanned QR string (URL, motionrps:// URI, or compact session:slot code)
 * and populate the form fields.
 */
function applyScannedQrText(rawText) {
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
        showFeedback("Invalid QR code. Please scan again.", true);
        return false;
    }

    currentPairingToken = pt;
    document.getElementById("input-session-code").value = session;
    if (slot !== "AUTO") {
        document.getElementById("select-player-slot").value = slot;
    }

    const resText = document.getElementById("scan-result-text");
    const resBadge = document.getElementById("scan-status-badge");
    resText.textContent = `${session} (${slot === "AUTO" ? "Automatic" : displaySlot(slot)})`;
    resBadge.textContent = "QR ready";
    resBadge.className = "badge online";

    showFeedback("Session found. Enter your name and join.");
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
            "Camera access is unavailable in this browser. Choose a QR photo or use your phone camera.",
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
                        applyScannedQrText(barcodes[0].rawValue);
                    }
                } catch (err) {
                    // Ignore transient frame errors
                }
            }, 300);
        } else {
            document.getElementById("camera-hint").textContent =
                "QR scanning is not supported in this browser. Use your phone camera to scan the code.";
        }
    } catch (err) {
        showFeedback("Camera permission was denied or is unavailable: " + err.message, true);
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
                applyScannedQrText(codes[0].rawValue);
                return;
            }
            showFeedback("Could not read the QR code. Try another photo or use the active session.", true);
        } catch (err) {
            // Fall through to active session fetch
        }
    }
    await fetchActiveSessionFromServer();
}

async function fetchActiveSessionFromServer() {
    try {
        const res = await fetch("/api/state");
        const data = await res.json();
        updateActiveSessionQr(data);
        if (data && data.session_active === false) {
            showFeedback("This game has ended. Ask the host to start a new session.", true);
        } else if (data && data.pairing && data.pairing.session_pair_url) {
            applyScannedQrText(data.pairing.session_pair_url);
        } else if (data && data.session_id) {
            applyScannedQrText(data.session_id);
        }
    } catch (err) {
        showFeedback("Could not get the active session from the server.", true);
    }
}

async function joinSession() {
    const rawCode = document.getElementById("input-session-code").value.trim();
    const username = document.getElementById("input-pair-username").value.trim();
    const slotChoice = document.getElementById("select-player-slot").value;

    if (!rawCode) {
        showFeedback("Scan a QR code or enter a session code first.", true);
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
            showFeedback(`Could not join: ${displayPairingError(data.reason || data.error)}`, true);
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

        showFeedback(`You joined as ${displaySlot(assignedSlot)}. Have fun!`);
        await refreshMobileStatus();
    } catch (err) {
        showFeedback("Could not contact the server to join the session.", true);
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
            showFeedback("You have left the game.");
            assignedSlot = null;
            localStorage.removeItem(STORAGE_KEY_SLOT);
            document.getElementById("btn-leave-session").disabled = true;
            await refreshMobileStatus();
        } else {
            showFeedback(`Could not leave the session: ${displayPairingError(data.reason)}`, true);
        }
    } catch (err) {
        showFeedback("Could not contact the server to leave the session.", true);
    }
}

async function refreshMobileStatus() {
    try {
        const res = await fetch("/api/state");
        const data = await res.json();

        updateActiveSessionQr(data);
        document.getElementById("mobile-game-state").textContent = displayGameState(data.state);
        document.getElementById("mobile-round-id").textContent = data.round_id;

        const targetSlot = assignedSlot || (
            document.getElementById("select-player-slot").value !== "AUTO"
                ? document.getElementById("select-player-slot").value
                : null
        );

        document.getElementById("assigned-slot-display").textContent = displaySlot(targetSlot === "AUTO" ? null : targetSlot);
        document.getElementById("assigned-session-display").textContent = data.session_id;

        if (targetSlot && data.players && data.players[targetSlot]) {
            const p = data.players[targetSlot];
            const webEl = document.getElementById("web-pair-status");
            if (p.web_paired) {
                webEl.textContent = "Connected";
                webEl.className = "badge online";
                document.getElementById("btn-leave-session").disabled = false;
            } else {
                webEl.textContent = "Not joined";
                webEl.className = "badge offline";
            }

            const sensorEl = document.getElementById("sensor-conn-status");
            if (p.connected) {
                sensorEl.textContent = "Active";
                sensorEl.className = "badge online";
            } else {
                sensorEl.textContent = "Waiting for sensor";
                sensorEl.className = "badge warn";
            }

            document.getElementById("mobile-score").textContent = p.score;
            document.getElementById("mobile-last-gesture").textContent = displayGesture(p.last_gesture);
            renderMoveDisplay(
                document.getElementById("mobile-round-choice"),
                p.choice || (p.has_chosen ? "LOCKED" : "-")
            );
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
    showFeedback("Command copied to clipboard!");
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
        applyScannedQrText(window.location.href);
    } else {
        fetchActiveSessionFromServer();
    }

    document.getElementById("btn-scan-qr").addEventListener("click", startLiveCameraScanner);
    document.getElementById("btn-stop-camera").addEventListener("click", stopCameraScanner);
    document.getElementById("input-qr-photo").addEventListener("change", handleQrPhotoInput);
    document.getElementById("btn-fetch-active-session").addEventListener("click", () => {
        fetchActiveSessionFromServer();
    });
    document.getElementById("btn-join-session").addEventListener("click", joinSession);
    document.getElementById("btn-leave-session").addEventListener("click", leaveSession);
    document.getElementById("btn-copy-auto-cmd").addEventListener("click", () => copyTextFromElement("mobile-auto-cmd"));
    document.getElementById("btn-copy-direct-cmd").addEventListener("click", () => copyTextFromElement("mobile-direct-cmd"));

    refreshMobileStatus();
    setInterval(refreshMobileStatus, 500);
})();
