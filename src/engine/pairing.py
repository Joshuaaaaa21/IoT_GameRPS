"""Session ID generation, safe QR pairing payloads, and Termux fallback parser."""

import base64
from dataclasses import dataclass
import json
import re
import secrets
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse

from src.engine.game_engine import PLAYER_A, PLAYER_B, VALID_PLAYERS

PAIRING_SCHEME = "motionrps://pair"
SAFE_SESSION_REGEX = re.compile(r"^[A-Za-z0-9_-]{4,64}$")

FORBIDDEN_SECRET_KEYS = {"password", "secret", "token", "api_key", "mqtt_password"}


@dataclass(frozen=True)
class PairingToken:
    """Represents non-secret pairing configuration linking a phone to a session and player slot."""
    session_id: str
    player_id: str
    broker_host: Optional[str] = None
    broker_port: Optional[int] = None

    def validate(self) -> None:
        if not SAFE_SESSION_REGEX.match(self.session_id):
            raise ValueError(f"Invalid session_id '{self.session_id}'.")
        if self.player_id not in VALID_PLAYERS:
            raise ValueError(f"Invalid player_id '{self.player_id}'. Must be one of {VALID_PLAYERS}.")


def generate_session_id(prefix: str = "rps") -> str:
    """Generate a unique, URL-safe, human-readable session ID (e.g. 'rps-a1b2c3')."""
    clean_prefix = re.sub(r"[^A-Za-z0-9]", "", prefix).lower() or "rps"
    suffix = secrets.token_hex(3)
    return f"{clean_prefix}-{suffix}"


def build_qr_payload(token: PairingToken) -> str:
    """
    Build a compact URI string for QR code generation.
    NEVER includes broker passwords or secret tokens.
    Example: motionrps://pair?session=rps-a1b2c3&player=PLAYER_A&host=broker.hivemq.com&port=8883
    """
    token.validate()
    params = {
        "session": token.session_id,
        "player": token.player_id,
    }
    if token.broker_host:
        params["host"] = token.broker_host
    if token.broker_port:
        params["port"] = str(token.broker_port)
    return f"{PAIRING_SCHEME}?{urlencode(params)}"


def build_compact_pair_code(token: PairingToken) -> str:
    """
    Build a short copy-pasteable pairing code for Termux fallback when QR camera scanning
    cannot directly pass arguments into Termux.
    Format: <session_id>:<player_id> (e.g. 'rps-a1b2c3:PLAYER_A')
    """
    token.validate()
    return f"{token.session_id}:{token.player_id}"


def build_termux_cli_command(token: PairingToken, simulate: bool = False) -> str:
    """Return the exact Termux CLI command a user can copy-paste as fallback."""
    token.validate()
    sim_flag = " --simulate" if simulate else ""
    return f"python -m src.edge.edge_client --pair-code {build_compact_pair_code(token)}{sim_flag}"


def parse_pairing_input(raw_input: str) -> PairingToken:
    """
    Parse pairing configuration from any of the supported formats:
    1. QR URI: 'motionrps://pair?session=rps-a1b2c3&player=PLAYER_A'
    2. Compact fallback code: 'rps-a1b2c3:PLAYER_A' or 'rps-a1b2c3:A'
    3. JSON object (without secrets): '{"session_id": "rps-a1b2c3", "player_id": "PLAYER_A"}'
    """
    text = raw_input.strip()
    if not text:
        raise ValueError("Pairing input cannot be empty.")

    # 1. Check URI format
    if text.startswith("motionrps://") or "session=" in text:
        parsed = urlparse(text if "://" in text else f"motionrps://pair?{text}")
        qs = parse_qs(parsed.query)
        for key in qs:
            if key.lower() in FORBIDDEN_SECRET_KEYS:
                raise ValueError("Security violation: QR pairing URI must not contain secrets/passwords.")
        session_id = (qs.get("session") or qs.get("session_id") or [""])[0].strip()
        raw_player = (qs.get("player") or qs.get("player_id") or [""])[0].strip()
        host = (qs.get("host") or [None])[0]
        port_str = (qs.get("port") or [None])[0]
        port = int(port_str) if port_str else None
        token = PairingToken(
            session_id=session_id,
            player_id=_normalize_player_slot(raw_player),
            broker_host=host,
            broker_port=port,
        )
        token.validate()
        return token

    # 2. Check JSON format
    if text.startswith("{") and text.endswith("}"):
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("Invalid JSON pairing payload.")
        for key in data:
            if str(key).lower() in FORBIDDEN_SECRET_KEYS:
                raise ValueError("Security violation: Pairing JSON must not contain secrets/passwords.")
        token = PairingToken(
            session_id=str(data.get("session_id") or data.get("session") or "").strip(),
            player_id=_normalize_player_slot(str(data.get("player_id") or data.get("player") or "")),
            broker_host=data.get("broker_host") or data.get("host"),
            broker_port=int(data["broker_port"]) if "broker_port" in data else None,
        )
        token.validate()
        return token

    # 3. Check compact fallback format '<session_id>:<player_id>'
    if ":" in text:
        session_part, player_part = text.split(":", 1)
        token = PairingToken(
            session_id=session_part.strip(),
            player_id=_normalize_player_slot(player_part.strip()),
        )
        token.validate()
        return token

    raise ValueError("Unrecognized pairing input format. Expected QR URI, JSON, or '<session_id>:<player_id>'.")


def _normalize_player_slot(raw: str) -> str:
    cleaned = raw.strip().upper()
    if cleaned in ("A", "1", "PLAYER_A", "PLAYERA"):
        return PLAYER_A
    if cleaned in ("B", "2", "PLAYER_B", "PLAYERB"):
        return PLAYER_B
    return cleaned
