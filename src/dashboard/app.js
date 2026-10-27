// Dashboard Controller for Motion RPS IoT (with ISO QR Code Rendering & Live Telemetry)

let lastSessionId = null;

async function postJSON(url, payload) {
    const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload || {})
    });
    return res.json();
}

function fmtVec(arr) {
    if (!Array.isArray(arr) || arr.length < 3) return "0.00, 0.00, 0.00";
    return arr.map((v) => Number(v || 0).toFixed(2)).join(", ");
}

function fmtTime(ts) {
    if (!ts) return "-";
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString("id-ID");
}

/**
 * Renders a real ISO/IEC 18004 QR Code matrix (with 4-module quiet zone) onto a canvas
 * so Android/iOS cameras and web QR scanners can scan it directly.
 */
function renderPairingMatrix(canvasId, uriText, pairCode, qrMatrix) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const size = canvas.width;
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, size, size);

    if (Array.isArray(qrMatrix) && qrMatrix.length > 0) {
        const modules = qrMatrix.length;
        const quietZone = 3;
        const totalCells = modules + quietZone * 2;
        const cellSize = Math.max(2, Math.floor(size / totalCells));
        const offset = Math.floor((size - cellSize * modules) / 2);

        ctx.fillStyle = "#000000";
        for (let r = 0; r < modules; r++) {
            const row = qrMatrix[r];
            for (let c = 0; c < modules; c++) {
                if (row[c]) {
                    ctx.fillRect(offset + c * cellSize, offset + r * cellSize, cellSize, cellSize);
                }
            }
        }
        return;
    }

    // Visual placeholder when session is ended
    ctx.fillStyle = "#888888";
    ctx.font = "12px sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("Sesi Berakhir", size / 2, size / 2);
}

function updatePlayerCard(suffix, playerState, outcomeText, pairingData) {
    const connEl = document.getElementById(`conn-status-${suffix}`);
    if (playerState.connected) {
        connEl.textContent = "READY / CONNECTED (Sensor Online)";
        connEl.className = "badge online";
    } else if (playerState.web_paired) {
        connEl.textContent = "PAIRED (Menunggu Sensor Termux)";
        connEl.className = "badge warn";
    } else {
        connEl.textContent = "WAITING (Belum Join)";
        connEl.className = "badge offline";
    }

    const nameInput = document.getElementById(`input-username-${suffix}`);
    if (nameInput && document.activeElement !== nameInput) {
        nameInput.value = playerState.username || "";
    }

    document.getElementById(`score-${suffix}`).textContent = playerState.score;

    // 1. Round Choice Display
    const choiceDisplay = playerState.choice
        ? playerState.choice
        : (playerState.has_chosen ? "LOCKED (Menunggu Lawan)" : "-");
    document.getElementById(`choice-${suffix}`).textContent = choiceDisplay;
    document.getElementById(`outcome-${suffix}`).textContent = outcomeText || "-";

    // 2. Live Telemetry & Diagnostic Panel
    const diagStatus = playerState.diag_status || "WAITING";
    const diagEl = document.getElementById(`diag-status-${suffix}`);
    if (diagEl) {
        diagEl.textContent = diagStatus;
        diagEl.className = "badge " + (
            diagStatus === "DETECTED" ? "online" : (diagStatus === "REJECTED" ? "warn" : "")
        );
    }

    const lastGestEl = document.getElementById(`last-gesture-${suffix}`);
    if (lastGestEl) lastGestEl.textContent = playerState.last_gesture || "-";

    const axisEl = document.getElementById(`axis-${suffix}`);
    if (axisEl) axisEl.textContent = playerState.triggered_axis || "-";

    const reasonEl = document.getElementById(`diag-reason-${suffix}`);
    if (reasonEl) reasonEl.textContent = playerState.diag_reason || "NO_TELEMETRY_YET";

    const accelEl = document.getElementById(`accel-${suffix}`);
    if (accelEl) accelEl.textContent = fmtVec(playerState.accel);

    const gyroEl = document.getElementById(`gyro-${suffix}`);
    if (gyroEl) gyroEl.textContent = fmtVec(playerState.gyro);

    const rmsEl = document.getElementById(`rms-${suffix}`);
    if (rmsEl) rmsEl.textContent = Number(playerState.motion_rms || 0).toFixed(3);

    const rotEl = document.getElementById(`rot-${suffix}`);
    if (rotEl) rotEl.textContent = Number(playerState.rotation_rad || 0).toFixed(3);

    const seenEl = document.getElementById(`last-seen-${suffix}`);
    if (seenEl) seenEl.textContent = fmtTime(playerState.last_telemetry_ts);

    const seqEl = document.getElementById(`seq-${suffix}`);
    if (seqEl) seqEl.textContent = String(playerState.sequence || 0);

    if (pairingData) {
        document.getElementById(`qr-uri-${suffix}`).textContent = pairingData.web_pair_url || pairingData.qr_uri;
        document.getElementById(`pair-code-${suffix}`).textContent = pairingData.pair_code;
        document.getElementById(`termux-cmd-${suffix}`).textContent = pairingData.termux_cmd;
        const autoCmdEl = document.getElementById(`auto-cmd-${suffix}`);
        if (autoCmdEl && pairingData.auto_termux_cmd) {
            autoCmdEl.textContent = pairingData.auto_termux_cmd;
        }
        renderPairingMatrix(`qr-canvas-${suffix}`, pairingData.qr_uri, pairingData.pair_code, pairingData.qr_matrix);
    }
}

async function refreshState() {
    try {
        const res = await fetch("/api/state");
        const data = await res.json();

        document.getElementById("session-id-display").textContent = data.session_id;
        document.getElementById("game-state-display").textContent = data.state;
        document.getElementById("round-id-display").textContent = data.round_id;

        const activeBadge = document.getElementById("session-active-badge");
        if (activeBadge) {
            const isActive = data.session_active !== false;
            activeBadge.textContent = isActive ? "ACTIVE" : "ENDED";
            activeBadge.className = "badge " + (isActive ? "online" : "offline");
        }

        const pairUrlEl = document.getElementById("session-pair-url-display");
        if (pairUrlEl && data.pairing && data.pairing.session_pair_url) {
            pairUrlEl.textContent = data.pairing.session_pair_url;
        }

        if (data.pairing) {
            renderPairingMatrix(
                "qr-canvas-session",
                data.pairing.session_pair_url || "",
                data.session_id,
                data.pairing.session_qr_matrix
            );
        }

        const mqttEl = document.getElementById("mqtt-status");
        mqttEl.textContent = data.mqtt.status_text;
        mqttEl.className = "badge " + (data.mqtt.connected ? "online" : "");

        const errEl = document.getElementById("error-banner");
        if (data.error) {
            errEl.hidden = false;
            errEl.textContent = "Error: " + data.error;
        } else {
            errEl.hidden = true;
            errEl.textContent = "";
        }

        const cdEl = document.getElementById("countdown-display");
        if (data.state === "COUNTDOWN" && data.round_deadline) {
            const rem = Math.max(0, data.round_deadline - data.server_time);
            cdEl.textContent = rem.toFixed(1) + "s";
        } else {
            cdEl.textContent = "-";
        }

        const last = data.last_result;
        document.getElementById("round-outcome-display").textContent = last ? last.outcome : "-";
        document.getElementById("winner-name-display").textContent =
            last && last.winner_username ? last.winner_username : (last ? "DRAW / TIDAK ADA" : "-");

        // Enable START GAME only when both players have active sensor connections and no round is in countdown
        const startBtn = document.getElementById("btn-start-game");
        if (startBtn) {
            const bothReady = Boolean(
                data.players.PLAYER_A.connected &&
                data.players.PLAYER_B.connected &&
                data.session_active !== false &&
                data.state !== "COUNTDOWN"
            );
            startBtn.disabled = !bothReady;
        }

        if (lastSessionId !== data.session_id) {
            document.getElementById("input-username-a").value = data.players.PLAYER_A.username;
            document.getElementById("input-username-b").value = data.players.PLAYER_B.username;
            lastSessionId = data.session_id;
        }

        updatePlayerCard(
            "a",
            data.players.PLAYER_A,
            last ? last.player_a_outcome : "-",
            data.pairing ? data.pairing.PLAYER_A : null
        );
        updatePlayerCard(
            "b",
            data.players.PLAYER_B,
            last ? last.player_b_outcome : "-",
            data.pairing ? data.pairing.PLAYER_B : null
        );
    } catch (err) {
        const errEl = document.getElementById("error-banner");
        errEl.hidden = false;
        errEl.textContent = "Gagal menghubungi server Game Engine.";
    }
}

async function simulateEdge(playerId, action) {
    await postJSON("/api/simulate_edge", { player_id: playerId, action: action });
    await refreshState();
}

async function simulateGesture(playerId, gesture) {
    await postJSON("/api/simulate_edge", { player_id: playerId, action: "GESTURE", gesture: gesture });
    await refreshState();
}

document.getElementById("btn-start-game").addEventListener("click", async () => {
    await postJSON("/api/command", { command: "START_GAME" });
    await refreshState();
});

document.getElementById("btn-reset-score").addEventListener("click", async () => {
    const confirmed = window.confirm("Apakah Anda yakin ingin mereset skor kedua pemain ke 0?");
    if (!confirmed) return;
    await postJSON("/api/command", { command: "RESET_SCORE", confirm: true });
    await refreshState();
});

document.getElementById("btn-save-name-a").addEventListener("click", async () => {
    const name = document.getElementById("input-username-a").value;
    await postJSON("/api/command", { command: "SET_USERNAME", player_id: "PLAYER_A", username: name });
    await refreshState();
});

document.getElementById("btn-save-name-b").addEventListener("click", async () => {
    const name = document.getElementById("input-username-b").value;
    await postJSON("/api/command", { command: "SET_USERNAME", player_id: "PLAYER_B", username: name });
    await refreshState();
});

document.getElementById("btn-new-session").addEventListener("click", async () => {
    await postJSON("/api/new_session", {});
    await refreshState();
});

const endSessionBtn = document.getElementById("btn-end-session");
if (endSessionBtn) {
    endSessionBtn.addEventListener("click", async () => {
        await postJSON("/api/end_session", {});
        await refreshState();
    });
}

window.simulateEdge = simulateEdge;
window.simulateGesture = simulateGesture;

refreshState();
setInterval(refreshState, 250);
