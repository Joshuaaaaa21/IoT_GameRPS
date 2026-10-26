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

    // Deterministic bit pattern from URI string
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

    const choiceDisplay = playerState.choice
        ? playerState.choice
        : (playerState.has_chosen ? "LOCKED (Menunggu Lawan)" : "-");
    document.getElementById(`choice-${suffix}`).textContent = choiceDisplay;
    document.getElementById(`outcome-${suffix}`).textContent = outcomeText || "-";

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

        // MQTT status
        const mqttEl = document.getElementById("mqtt-status");
        mqttEl.textContent = data.mqtt.status_text;
        mqttEl.className = "badge " + (data.mqtt.connected ? "online" : "");

        // Error display
        const errEl = document.getElementById("error-banner");
        if (data.error) {
            errEl.hidden = false;
            errEl.textContent = "Error: " + data.error;
        } else {
            errEl.hidden = true;
            errEl.textContent = "";
        }

        // Countdown calculation
        const cdEl = document.getElementById("countdown-display");
        if (data.state === "COUNTDOWN" && data.round_deadline) {
            const rem = Math.max(0, data.round_deadline - data.server_time);
            cdEl.textContent = rem.toFixed(1) + "s";
        } else {
            cdEl.textContent = "-";
        }

        // Last round result
        const last = data.last_result;
        document.getElementById("round-outcome-display").textContent = last ? last.outcome : "-";
        document.getElementById("winner-name-display").textContent =
            last && last.winner_username ? last.winner_username : (last ? "DRAW / TIDAK ADA" : "-");

        // Sync username inputs only when session changes so typing isn't interrupted
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
