// Minimal Dashboard Controller for Motion RPS IoT

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
 * Draws a deterministic visual matrix pattern + readable label on canvas for QR pairing
 * without requiring external third-party CDN scripts (works 100% offline).
 */
function renderPairingMatrix(canvasId, uriText, pairCode) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const size = canvas.width;
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, size, size);

    const cells = 21;
    const cellSize = Math.floor((size - 16) / cells);
    const offset = 8;

    function drawFinder(cx, cy) {
        ctx.fillStyle = "#000000";
        ctx.fillRect(offset + cx * cellSize, offset + cy * cellSize, 7 * cellSize, 7 * cellSize);
        ctx.fillStyle = "#ffffff";
        ctx.fillRect(offset + (cx + 1) * cellSize, offset + (cy + 1) * cellSize, 5 * cellSize, 5 * cellSize);
        ctx.fillStyle = "#000000";
        ctx.fillRect(offset + (cx + 2) * cellSize, offset + (cy + 2) * cellSize, 3 * cellSize, 3 * cellSize);
    }

    drawFinder(0, 0);
    drawFinder(cells - 7, 0);
    drawFinder(0, cells - 7);

    ctx.fillStyle = "#000000";
    for (let r = 0; r < cells; r++) {
        for (let c = 0; c < cells; c++) {
            const inFinder =
                (r < 8 && c < 8) ||
                (r < 8 && c >= cells - 8) ||
                (r >= cells - 8 && c < 8);
            if (inFinder) continue;
            const idx = (r * cells + c) % uriText.length;
            const code = uriText.charCodeAt(idx);
            if (((code + r * 3 + c * 7) & 1) === 0) {
                ctx.fillRect(offset + c * cellSize, offset + r * cellSize, cellSize, cellSize);
            }
        }
    }
}

function updatePlayerCard(suffix, playerState, outcomeText, pairingData) {
    const connEl = document.getElementById(`conn-status-${suffix}`);
    connEl.textContent = playerState.connected ? "CONNECTED" : "DISCONNECTED";
    connEl.className = "badge " + (playerState.connected ? "online" : "offline");

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
        document.getElementById(`qr-uri-${suffix}`).textContent = pairingData.qr_uri;
        document.getElementById(`pair-code-${suffix}`).textContent = pairingData.pair_code;
        document.getElementById(`termux-cmd-${suffix}`).textContent = pairingData.termux_cmd;
        renderPairingMatrix(`qr-canvas-${suffix}`, pairingData.qr_uri, pairingData.pair_code);
    }
}

async function refreshState() {
    try {
        const res = await fetch("/api/state");
        const data = await res.json();

        document.getElementById("session-id-display").textContent = data.session_id;
        document.getElementById("game-state-display").textContent = data.state;
        document.getElementById("round-id-display").textContent = data.round_id;

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

window.simulateEdge = simulateEdge;
window.simulateGesture = simulateGesture;

refreshState();
setInterval(refreshState, 250);
