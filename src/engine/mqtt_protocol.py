"""MQTT topic schema, QoS policies, and payload validators for Motion RPS IoT.

Portions adapted from PocketGuard IoT (https://github.com/ruthhhs/iot-projek)
under the MIT License — Copyright (c) 2026 PocketGuard IoT contributors.
"""

import json
import math
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from src.engine.game_engine import VALID_PLAYERS

TOPIC_PREFIX = "motion-rps"
DEFAULT_HOST = "broker.hivemq.com"
DEFAULT_WS_URL = "wss://broker.hivemq.com:8884/mqtt"
DEFAULT_SESSION_ID = "rps_kelompok_07"
STALE_WINDOW_MS = 30_000  # Reject messages older/newer than 30 seconds when sent_at_ms is present

# QoS Constants
QOS_TELEMETRY = 0  # High-frequency sensor/status heartbeat
QOS_CRITICAL = 1   # Choices, commands, game state, and round results


@dataclass(frozen=True)
class MQTTConfig:
    host: str = DEFAULT_HOST
    port: int = 8883
    username: Optional[str] = None
    password: Optional[str] = None
    use_tls: bool = True
    websocket_url: str = DEFAULT_WS_URL
    allow_insecure_local: bool = False

    @classmethod
    def from_env(cls) -> "MQTTConfig":
        """Load MQTT broker settings from environment variables (or .env file if present)."""
        _load_dotenv_if_exists()
        host = (
            os.getenv("MQTT_HOST")
            or os.getenv("MQTT_BROKER_HOST")
            or DEFAULT_HOST
        ).strip()
        use_tls_raw = os.getenv("MQTT_TLS") or os.getenv("MQTT_USE_TLS") or "true"
        use_tls = use_tls_raw.strip().lower() in ("1", "true", "yes")
        allow_insecure = os.getenv("ALLOW_INSECURE_LOCAL", "false").strip().lower() in ("1", "true", "yes")

        default_port = "8883" if use_tls else "1883"
        port_raw = os.getenv("MQTT_PORT") or os.getenv("MQTT_BROKER_PORT") or default_port
        port = int(port_raw)

        username = (os.getenv("MQTT_USERNAME") or "").strip() or None
        password = os.getenv("MQTT_PASSWORD") or None

        ws_url = (os.getenv("MQTT_WS_URL") or "").strip()
        if not ws_url and host == DEFAULT_HOST:
            ws_url = DEFAULT_WS_URL

        return cls(
            host=host,
            port=port,
            username=username,
            password=password,
            use_tls=use_tls,
            websocket_url=ws_url,
            allow_insecure_local=allow_insecure,
        )

    def validate_tls_policy(self) -> None:
        """Enforce TLS unless ALLOW_INSECURE_LOCAL=true is explicitly set (adapted from reference)."""
        if not self.use_tls and not self.allow_insecure_local:
            raise RuntimeError(
                "TLS disabled. Only allow this for local development via ALLOW_INSECURE_LOCAL=true"
            )


def _load_dotenv_if_exists(dotenv_path: str = ".env") -> None:
    """Minimal .env parser to avoid mandatory external python-dotenv dependency."""
    if not os.path.isfile(dotenv_path):
        return
    with open(dotenv_path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, val = stripped.split("=", 1)
            key = key.strip()
            val = val.strip().strip("'").strip('"')
            if key and key not in os.environ:
                os.environ[key] = val


class TopicBuilder:
    """Builds and parses session-isolated MQTT topics."""

    def __init__(self, session_id: str) -> None:
        if not session_id or not re.match(r"^[A-Za-z0-9_-]+$", session_id):
            raise ValueError("session_id must contain only alphanumeric, dash, or underscore characters.")
        self.session_id = session_id

    def player_telemetry(self, player_id: str) -> str:
        return f"{TOPIC_PREFIX}/{self.session_id}/player/{player_id}/telemetry"

    def player_choice(self, player_id: str) -> str:
        return f"{TOPIC_PREFIX}/{self.session_id}/player/{player_id}/choice"

    def game_command(self) -> str:
        return f"{TOPIC_PREFIX}/{self.session_id}/game/command"

    def game_state(self) -> str:
        return f"{TOPIC_PREFIX}/{self.session_id}/game/state"

    def game_result(self) -> str:
        return f"{TOPIC_PREFIX}/{self.session_id}/game/result"

    def wildcard_all_player_telemetry(self) -> str:
        return f"{TOPIC_PREFIX}/{self.session_id}/player/+/telemetry"

    def wildcard_all_player_choices(self) -> str:
        return f"{TOPIC_PREFIX}/{self.session_id}/player/+/choice"


def parse_json_payload(raw_payload: bytes | str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Safely parse incoming MQTT payload into a JSON dictionary."""
    try:
        text = raw_payload.decode("utf-8") if isinstance(raw_payload, bytes) else str(raw_payload)
        data = json.loads(text)
        if not isinstance(data, dict):
            return None, "PAYLOAD_NOT_OBJECT"
        return data, None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "INVALID_JSON"


def validate_finite_metrics(payload: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Validate optional numeric sensor features (motion_rms, rotation_rad, battery)
    using strict type and math.isfinite checks adapted from PocketGuard shared/messages.py.
    """
    for key in ("motion_rms", "rotation_rad"):
        if key in payload and payload[key] is not None:
            val = payload[key]
            if type(val) not in (int, float) or not math.isfinite(val) or val < 0:
                return False, f"INVALID_FINITE_{key.upper()}"
    return True, "VALID_METRICS"


def validate_session_and_player(
    payload: Dict[str, Any],
    expected_session_id: str,
    topic_player_id: Optional[str] = None,
) -> Tuple[bool, str]:
    """Validate session_id, player_id, and finite numeric fields inside an incoming payload."""
    msg_session = payload.get("session_id")
    if msg_session != expected_session_id:
        return False, "SESSION_ID_MISMATCH"

    if topic_player_id is not None:
        if topic_player_id not in VALID_PLAYERS:
            return False, "INVALID_TOPIC_PLAYER_ID"
        msg_player = payload.get("player_id")
        if msg_player != topic_player_id:
            return False, "PLAYER_ID_MISMATCH"

    metrics_ok, metrics_reason = validate_finite_metrics(payload)
    if not metrics_ok:
        return False, metrics_reason

    return True, "VALID"
