// Dashboard Controller for Motion RPS IoT (with ISO QR Code Rendering & Live Telemetry)

let lastSessionId = null;
let sessionActive = true;
const dirtyNames = { a: false, b: false };
const simulatedMoves = { a: null, b: null };

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
    return d.toLocaleTimeString("en-US");
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

function displayOutcome(outcome) {
    const labels = {
        PLAYER_A_WIN: "Player A wins",
        PLAYER_B_WIN: "Player B wins",
        DRAW: "Draw",
        FORFEIT_A: "Player A left",
        FORFEIT_B: "Player B left",
        VOID_NO_MOVES: "Round canceled",
        WIN: "Win",
        LOSE: "Lose"
    };
    return labels[outcome] || outcome || "-";
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
    const displayKey = move ? normalized : "BACK";
    if (element.dataset.displayedMove === displayKey) {
        return;
    }
    element.dataset.displayedMove = displayKey;
    element.classList.toggle("has-move", Boolean(move));
    element.classList.remove("is-revealed");
    element.setAttribute("role", "img");
    element.setAttribute("aria-label", move ? move[1] : "Move hidden until detected");

    const card = document.createElement("span");
    card.className = "move-card";
    const back = document.createElement("span");
    back.className = "move-card-face move-card-back";
    back.setAttribute("aria-hidden", "true");
    const leaf = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    leaf.setAttribute("viewBox", "0 0 64 64");
    leaf.innerHTML = '<path d="M32 52V32m0 12C18 44 10 35 10 21c13 0 22 8 22 23Zm0-10c0-15 9-24 23-25 0 14-9 25-23 25Z" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="3"/><path d="M17 53h30" fill="none" stroke="currentColor" stroke-linecap="round" stroke-width="3"/>';
    back.append(leaf);

    const front = document.createElement("span");
    front.className = "move-card-face move-card-front";
    front.setAttribute("aria-hidden", "true");
    if (move) {
        const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("class", "move-art");
        svg.setAttribute("viewBox", "0 0 64 64");
        const symbol = document.getElementById(move[0]);
        if (symbol) {
            for (const shape of symbol.children) {
                svg.append(shape.cloneNode(true));
            }
        }

        const label = document.createElement("span");
        label.textContent = move[1];
        front.append(svg, label);
    }
    card.append(back, front);
    element.replaceChildren(card);
    if (move) {
        void element.offsetWidth;
        element.classList.add("is-revealed");
    }
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
    ctx.fillText("Session ended", size / 2, size / 2);
}

function updatePlayerCard(suffix, playerState, outcomeText, pairingData) {
    const connEl = document.getElementById(`conn-status-${suffix}`);
    if (playerState.connected) {
        connEl.textContent = "Ready";
        connEl.className = "badge online";
    } else if (playerState.web_paired) {
        connEl.textContent = "Waiting for sensor";
        connEl.className = "badge warn";
    } else {
        connEl.textContent = "Not joined";
        connEl.className = "badge offline";
    }

    const nameInput = document.getElementById(`input-username-${suffix}`);
    if (nameInput && !dirtyNames[suffix]) {
        nameInput.value = playerState.username || "";
    }

    document.getElementById(`score-${suffix}`).textContent = playerState.score;

    // 1. Round Choice Display
    const actualChoice = playerState.choice || (playerState.has_chosen ? "LOCKED" : null);
    const gestureMoves = { LEFT: "ROCK", UP: "PAPER", RIGHT: "SCISSORS" };
    const detectedMove = playerState.diag_status === "DETECTED"
        ? gestureMoves[String(playerState.last_gesture || "").toUpperCase()]
        : null;
    const choiceDisplay = actualChoice || simulatedMoves[suffix] || detectedMove || "-";
    renderMoveDisplay(document.getElementById(`choice-${suffix}`), choiceDisplay);
    const outcomeEl = document.getElementById(`outcome-${suffix}`);
    const outcome = !actualChoice && simulatedMoves[suffix] ? "" : displayOutcome(outcomeText);
    if (outcomeEl) {
        outcomeEl.textContent = outcome === "-" ? "" : outcome;
        outcomeEl.className = "outcome-tag";
        outcomeEl.removeAttribute("data-outcome");
        if (outcome && outcome !== "-") {
            const norm = String(outcomeText || outcome).toUpperCase();
            if (norm === "WIN" || norm.includes("WIN") || outcome.toLowerCase().includes("win")) {
                outcomeEl.classList.add("win");
                outcomeEl.dataset.outcome = "win";
            } else if (norm === "LOSE" || norm.includes("LOSE") || outcome.toLowerCase().includes("lose")) {
                outcomeEl.classList.add("lose");
                outcomeEl.dataset.outcome = "lose";
            } else if (norm === "DRAW" || norm.includes("DRAW") || outcome.toLowerCase().includes("draw")) {
                outcomeEl.classList.add("draw");
                outcomeEl.dataset.outcome = "draw";
            }
        }
    }

    // 2. Live Telemetry & Diagnostic Panel
    const diagStatus = playerState.diag_status || "WAITING";
    const diagEl = document.getElementById(`diag-status-${suffix}`);
    if (diagEl) {
        const diagLabels = { DETECTED: "Detected", REJECTED: "Not recognized", WAITING: "Waiting", OFFLINE: "Offline" };
        diagEl.textContent = diagLabels[diagStatus] || diagStatus;
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
        document.getElementById("game-state-display").textContent = displayGameState(data.state);
        document.getElementById("round-id-display").textContent = data.round_id;

        const activeBadge = document.getElementById("session-active-badge");
        sessionActive = data.session_active !== false;
        if (activeBadge) {
            activeBadge.textContent = sessionActive ? "Active" : "Ended";
            activeBadge.className = "badge " + (sessionActive ? "online" : "offline");
        }
        ["a", "b"].forEach((suffix) => {
            const input = document.getElementById(`input-username-${suffix}`);
            const saveButton = document.getElementById(`btn-save-name-${suffix}`);
            if (input) input.disabled = !sessionActive;
            if (saveButton) saveButton.disabled = !sessionActive || saveButton.dataset.saving === "true";
        });

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
        mqttEl.textContent = data.mqtt.connected ? "Connected" : "Local mode";
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
        document.getElementById("round-outcome-display").textContent = last ? displayOutcome(last.outcome) : "-";
        document.getElementById("winner-name-display").textContent =
            last && last.winner_username ? last.winner_username : (last ? displayOutcome(last.outcome) : "-");

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
            dirtyNames.a = false;
            dirtyNames.b = false;
            simulatedMoves.a = null;
            simulatedMoves.b = null;
            lastSessionId = data.session_id;
        }
        if (data.state === "COUNTDOWN") {
            simulatedMoves.a = null;
            simulatedMoves.b = null;
        }
        if (!sessionActive) {
            dirtyNames.a = false;
            dirtyNames.b = false;
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
        errEl.textContent = "Could not connect to the game server.";
    }
}

async function simulateEdge(playerId, action) {
    const suffix = playerId === "PLAYER_A" ? "a" : "b";
    const feedback = document.getElementById(`sim-feedback-${suffix}`);
    simulatedMoves[suffix] = null;
    try {
        const result = await postJSON("/api/simulate_edge", { player_id: playerId, action: action });
        if (!result.ok) {
            feedback.textContent = result.reason || "The test action failed.";
            feedback.className = "sim-feedback error";
            return;
        }
        await refreshState();
        feedback.textContent = action === "CONNECT" ? "Connected for testing." : "Disconnected.";
        feedback.className = "sim-feedback success";
    } catch (error) {
        feedback.textContent = "Could not reach the game server.";
        feedback.className = "sim-feedback error";
    }
}

async function simulateGesture(playerId, gesture) {
    const suffix = playerId === "PLAYER_A" ? "a" : "b";
    const feedback = document.getElementById(`sim-feedback-${suffix}`);
    simulatedMoves[suffix] = null;
    try {
        const result = await postJSON("/api/simulate_edge", {
            player_id: playerId,
            action: "GESTURE",
            gesture: gesture
        });
        simulatedMoves[suffix] = result.choice || null;
        await refreshState();
        if (result.choice) {
            const move = String(result.choice).toLowerCase();
            feedback.textContent = result.ok
                ? `Detected ${move}.`
                : `Detected ${move}. Start a round to play it.`;
            feedback.className = "sim-feedback success";
        } else if (gesture === "WEAK" && result.diag_status === "REJECTED") {
            feedback.textContent = "Weak move rejected as expected.";
            feedback.className = "sim-feedback success";
        } else {
            feedback.textContent = `Move not detected: ${result.reason || "try again"}.`;
            feedback.className = "sim-feedback error";
        }
    } catch (error) {
        feedback.textContent = "Could not reach the game server.";
        feedback.className = "sim-feedback error";
    }
}

document.getElementById("btn-start-game").addEventListener("click", async () => {
    simulatedMoves.a = null;
    simulatedMoves.b = null;
    await postJSON("/api/command", { command: "START_GAME" });
    await refreshState();
});

document.getElementById("btn-reset-score").addEventListener("click", async () => {
    const confirmed = window.confirm("Reset both players' scores to zero?");
    if (!confirmed) return;
    await postJSON("/api/command", { command: "RESET_SCORE", confirm: true });
    await refreshState();
});

async function savePlayerName(suffix, playerId) {
    const input = document.getElementById(`input-username-${suffix}`);
    const button = document.getElementById(`btn-save-name-${suffix}`);
    const status = document.getElementById(`save-name-status-${suffix}`);
    if (!sessionActive || !input || !button || !status) return;

    const name = input.value.trim();
    status.textContent = "";
    status.className = "save-name-status";
    button.dataset.saving = "true";
    button.disabled = true;
    try {
        const result = await postJSON("/api/command", {
            command: "SET_USERNAME",
            player_id: playerId,
            username: name
        });
        if (!result.ok) {
            status.textContent = result.reason || "Could not save name.";
            status.classList.add("error");
            return;
        }

        input.value = name.slice(0, 32);
        dirtyNames[suffix] = false;
        status.textContent = "Saved";
        status.classList.add("success");
        await refreshState();
    } catch (error) {
        status.textContent = "Could not save name. Try again.";
        status.classList.add("error");
    } finally {
        button.dataset.saving = "false";
        button.disabled = !sessionActive;
    }
}

["a", "b"].forEach((suffix) => {
    const input = document.getElementById(`input-username-${suffix}`);
    const button = document.getElementById(`btn-save-name-${suffix}`);
    input.addEventListener("input", () => {
        dirtyNames[suffix] = true;
        const status = document.getElementById(`save-name-status-${suffix}`);
        status.textContent = "";
        status.className = "save-name-status";
    });
    button.addEventListener("click", () => {
        savePlayerName(suffix, suffix === "a" ? "PLAYER_A" : "PLAYER_B");
    });
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
