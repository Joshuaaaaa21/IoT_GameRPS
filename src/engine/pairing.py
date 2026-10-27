"""Session ID generation, safe QR pairing payloads, temporary token validation,
slot ownership management, and Termux fallback parser for Motion RPS IoT."""

from dataclasses import dataclass, field
import json
import re
import secrets
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse

from src.engine.game_engine import PLAYER_A, PLAYER_B, VALID_PLAYERS
from src.engine.qr_encoder import encode_qr_matrix

PAIRING_SCHEME = "motionrps://pair"
PAIRING_VERSION = "1"
DEFAULT_TOKEN_TTL_SEC = 600.0  # 10 minutes temporary QR pairing token lifetime
SAFE_SESSION_REGEX = re.compile(r"^[A-Za-z0-9_-]{4,64}$")

FORBIDDEN_SECRET_KEYS = {"password", "secret", "token", "api_key", "mqtt_password", "broker_password"}


@dataclass(frozen=True)
class PairingToken:
    """Represents non-secret pairing configuration linking a phone to a session and player slot."""
    session_id: str
    player_id: str
    broker_host: Optional[str] = None
    broker_port: Optional[int] = None
    pairing_token: Optional[str] = None
    version: str = PAIRING_VERSION
    server_url: Optional[str] = None

    def validate(self) -> None:
        if not SAFE_SESSION_REGEX.match(self.session_id):
            raise ValueError(f"Invalid session_id '{self.session_id}'.")
        if self.player_id not in VALID_PLAYERS:
            raise ValueError(f"Invalid player_id '{self.player_id}'. Must be one of {VALID_PLAYERS}.")


@dataclass
class SlotAssignment:
    """Tracks web QR pairing claim and device identity for a player slot (PLAYER_A or PLAYER_B)."""
    player_id: str
    device_id: str
    client_token: str
    username: str
    paired_at: float
    last_active_at: float
    web_paired: bool = True


class SessionPairingManager:
    """
    Manages server-side QR pairing tokens (with expiration), slot assignments (PLAYER_A / PLAYER_B),
    anti-hijack slot protection, reconnect identity preservation, and 3rd-player rejection.
    """

    def __init__(
        self,
        session_id: str,
        ttl_seconds: float = DEFAULT_TOKEN_TTL_SEC,
        now: Optional[float] = None,
    ) -> None:
        self.ttl_seconds = float(ttl_seconds)
        self.session_id: str = ""
        self.pairing_token: str = ""
        self.created_at: float = 0.0
        self.expires_at: float = 0.0
        self.session_active: bool = True
        self.slots: Dict[str, Optional[SlotAssignment]] = {
            PLAYER_A: None,
            PLAYER_B: None,
        }
        self.reset_for_session(session_id, now=now)

    def reset_for_session(self, session_id: str, now: Optional[float] = None) -> None:
        if not SAFE_SESSION_REGEX.match(session_id):
            raise ValueError(f"Invalid session_id '{session_id}'.")
        current_time = time.time() if now is None else float(now)
        self.session_id = session_id
        self.pairing_token = secrets.token_urlsafe(9)
        self.created_at = current_time
        self.expires_at = current_time + self.ttl_seconds
        self.session_active = True
        self.slots = {PLAYER_A: None, PLAYER_B: None}

    def end_session(self) -> None:
        """Invalidate the current session and release all pairing claims."""
        self.session_active = False
        self.expires_at = 0.0
        self.slots = {PLAYER_A: None, PLAYER_B: None}

    def refresh_token(self, now: Optional[float] = None) -> str:
        current_time = time.time() if now is None else float(now)
        self.pairing_token = secrets.token_urlsafe(9)
        self.expires_at = current_time + self.ttl_seconds
        return self.pairing_token

    def is_token_valid(self, token_str: Optional[str], now: Optional[float] = None) -> Tuple[bool, str]:
        if not self.session_active:
            return False, "SESSION_ENDED"
        current_time = time.time() if now is None else float(now)
        if current_time > self.expires_at:
            return False, "EXPIRED_PAIRING_TOKEN"
        if not token_str or token_str.strip() != self.pairing_token:
            return False, "INVALID_PAIRING_TOKEN"
        return True, "VALID_TOKEN"

    def join_session(
        self,
        session_id: str,
        username: str,
        requested_slot: Optional[str] = None,
        pairing_token: Optional[str] = None,
        device_id: Optional[str] = None,
        client_token: Optional[str] = None,
        require_token: bool = False,
        sensor_connected_map: Optional[Dict[str, bool]] = None,
        now: Optional[float] = None,
    ) -> Tuple[bool, str, Optional[SlotAssignment]]:
        """
        Validate and register a player joining via QR scan or manual session code.
        Enforces:
        - Session validity & active state
        - Temporary token expiration (when token is provided or required)
        - Slot protection (cannot overwrite an occupied slot from a different device)
        - Reconnect preservation (same device_id or client_token reclaims its slot)
        - 3rd player rejection ('SESSION_FULL')
        """
        current_time = time.time() if now is None else float(now)
        if not self.session_active:
            return False, "SESSION_ENDED", None

        if session_id.strip() != self.session_id:
            return False, "INVALID_SESSION", None

        # Validate temporary QR token if provided or strictly required
        if require_token or (pairing_token is not None and pairing_token.strip() != ""):
            tok_ok, tok_reason = self.is_token_valid(pairing_token, now=current_time)
            if not tok_ok:
                return False, tok_reason, None

        clean_user = (username or "").strip()[:32]
        dev_id = (device_id or "").strip() or f"dev-{secrets.token_hex(4)}"
        cli_tok = (client_token or "").strip()
        sensor_map = sensor_connected_map or {}

        # Normalize requested slot
        norm_slot: Optional[str] = None
        if requested_slot and requested_slot.strip().upper() not in ("", "AUTO", "ANY"):
            candidate = _normalize_player_slot(requested_slot)
            if candidate not in VALID_PLAYERS:
                return False, "INVALID_PLAYER_SLOT", None
            norm_slot = candidate

        # Check if this device_id or client_token already owns a slot in this session
        existing_slot_for_device: Optional[str] = None
        for pid, assign in self.slots.items():
            if assign is not None:
                if (cli_tok and assign.client_token == cli_tok) or (device_id and assign.device_id == dev_id):
                    existing_slot_for_device = pid
                    break

        target_slot: Optional[str] = norm_slot
        if target_slot is None:
            if existing_slot_for_device is not None:
                target_slot = existing_slot_for_device
            else:
                # Pick first available slot (neither claimed by another web device nor occupied by active sensor)
                for pid in (PLAYER_A, PLAYER_B):
                    occupant = self.slots[pid]
                    sensor_busy = bool(sensor_map.get(pid, False))
                    if occupant is None and not sensor_busy:
                        target_slot = pid
                        break
                if target_slot is None:
                    return False, "SESSION_FULL", None
        else:
            # Explicit slot requested (PLAYER_A or PLAYER_B)
            occupant = self.slots[target_slot]
            sensor_busy = bool(sensor_map.get(target_slot, False))
            if occupant is not None:
                same_owner = (
                    (cli_tok and occupant.client_token == cli_tok)
                    or (device_id and occupant.device_id == dev_id)
                )
                if not same_owner:
                    return False, "SLOT_ALREADY_OCCUPIED", None
            elif sensor_busy and existing_slot_for_device != target_slot:
                # A physical sensor is already connected on this slot without web claim from this device
                return False, "SLOT_ALREADY_OCCUPIED", None

            # If this device previously held the OTHER slot and is switching to an empty slot, release old slot
            if existing_slot_for_device and existing_slot_for_device != target_slot:
                self.slots[existing_slot_for_device] = None

        # Create or update slot assignment
        prev = self.slots[target_slot]
        final_username = clean_user or (prev.username if prev else (
            "Player A" if target_slot == PLAYER_A else "Player B"
        ))
        final_client_token = prev.client_token if prev else (cli_tok or secrets.token_urlsafe(12))

        assignment = SlotAssignment(
            player_id=target_slot,
            device_id=dev_id,
            client_token=final_client_token,
            username=final_username,
            paired_at=prev.paired_at if prev else current_time,
            last_active_at=current_time,
            web_paired=True,
        )
        self.slots[target_slot] = assignment
        return True, "PAIRED_OK", assignment

    def leave_session(
        self,
        player_id: str,
        device_id: Optional[str] = None,
        client_token: Optional[str] = None,
        force: bool = False,
    ) -> Tuple[bool, str]:
        norm_pid = _normalize_player_slot(player_id)
        if norm_pid not in VALID_PLAYERS:
            return False, "INVALID_PLAYER_SLOT"
        occupant = self.slots.get(norm_pid)
        if occupant is None:
            return True, "ALREADY_EMPTY"
        if not force:
            if client_token and occupant.client_token == client_token:
                pass
            elif device_id and occupant.device_id == device_id:
                pass
            elif client_token or device_id:
                return False, "UNAUTHORIZED_SLOT_LEAVE"
        self.slots[norm_pid] = None
        return True, "LEFT_SESSION"

    def get_slot_snapshot(self, player_id: str) -> Dict[str, Any]:
        assign = self.slots.get(player_id)
        if assign is None:
            return {
                "web_paired": False,
                "device_id": None,
                "paired_at": None,
            }
        return {
            "web_paired": assign.web_paired,
            "device_id": assign.device_id,
            "paired_at": assign.paired_at,
        }


def generate_session_id(prefix: str = "rps") -> str:
    """Generate a unique, URL-safe, human-readable session ID (e.g. 'rps-a1b2c3')."""
    clean_prefix = re.sub(r"[^A-Za-z0-9]", "", prefix).lower() or "rps"
    suffix = secrets.token_hex(3)
    return f"{clean_prefix}-{suffix}"


def build_qr_payload(token: PairingToken) -> str:
    """
    Build a compact URI string for QR code generation.
    NEVER includes broker passwords or secret tokens.
    Includes format version v=1, session_id, player_id, and optional temporary pairing token (pt).
    Example: motionrps://pair?v=1&session=rps-a1b2c3&player=PLAYER_A
    """
    token.validate()
    params: Dict[str, str] = {
        "v": token.version or PAIRING_VERSION,
        "session": token.session_id,
        "player": token.player_id,
    }
    if token.pairing_token:
        params["pt"] = token.pairing_token
    if token.broker_host:
        params["host"] = token.broker_host
    if token.broker_port:
        params["port"] = str(token.broker_port)
    return f"{PAIRING_SCHEME}?{urlencode(params)}"


def build_web_pair_url(
    base_url: str,
    session_id: str,
    pairing_token: Optional[str] = None,
    player_id: Optional[str] = None,
) -> str:
    """
    Build a direct HTTP(S) URL to the mobile pairing page (/pair) for QR code scanning.
    Contains only minimum safe fields: v=1, session, optional slot, and temporary pairing token (pt).
    """
    if not SAFE_SESSION_REGEX.match(session_id):
        raise ValueError(f"Invalid session_id '{session_id}'.")
    clean_base = base_url.rstrip("/")
    params: Dict[str, str] = {
        "v": PAIRING_VERSION,
        "session": session_id,
    }
    if player_id and player_id in VALID_PLAYERS:
        params["player"] = player_id
    if pairing_token:
        params["pt"] = pairing_token
    return f"{clean_base}/pair?{urlencode(params)}"


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


def parse_pairing_input(raw_input: str, default_player: str = PLAYER_A) -> PairingToken:
    """
    Parse pairing configuration from any of the supported formats:
    1. QR URI or Web Pairing URL:
       - 'motionrps://pair?v=1&session=rps-a1b2c3&player=PLAYER_A&pt=...'
       - 'http://192.168.1.10:8080/pair?v=1&session=rps-a1b2c3&player=PLAYER_A&pt=...'
    2. Compact fallback code: 'rps-a1b2c3:PLAYER_A' or 'rps-a1b2c3:A'
    3. JSON object (without secrets): '{"session_id": "rps-a1b2c3", "player_id": "PLAYER_A"}'
    """
    text = raw_input.strip()
    if not text:
        raise ValueError("Pairing input cannot be empty.")

    # 1. Check URI / URL format
    if text.startswith(("motionrps://", "http://", "https://")) or "session=" in text:
        parsed = urlparse(text if "://" in text else f"motionrps://pair?{text}")
        qs = parse_qs(parsed.query)
        for key in qs:
            if key.lower() in FORBIDDEN_SECRET_KEYS:
                raise ValueError("Security violation: QR pairing URI must not contain secrets/passwords.")
        session_id = (qs.get("session") or qs.get("session_id") or [""])[0].strip()
        raw_player = (qs.get("player") or qs.get("player_id") or qs.get("slot") or [""])[0].strip()
        if not raw_player or raw_player.upper() == "AUTO":
            norm_player = _normalize_player_slot(default_player)
        else:
            norm_player = _normalize_player_slot(raw_player)
        host = (qs.get("host") or [None])[0]
        port_str = (qs.get("port") or [None])[0]
        port = int(port_str) if port_str else None
        pt = (qs.get("pt") or qs.get("pairing_token") or [None])[0]
        ver = (qs.get("v") or [PAIRING_VERSION])[0]
        server_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme in ("http", "https") and parsed.netloc else None
        token = PairingToken(
            session_id=session_id,
            player_id=norm_player,
            broker_host=host,
            broker_port=port,
            pairing_token=pt,
            version=str(ver),
            server_url=server_url,
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
        raw_p = str(data.get("player_id") or data.get("player") or data.get("slot") or default_player)
        norm_p = _normalize_player_slot(default_player if raw_p.upper() == "AUTO" else raw_p)
        token = PairingToken(
            session_id=str(data.get("session_id") or data.get("session") or "").strip(),
            player_id=norm_p,
            broker_host=data.get("broker_host") or data.get("host"),
            broker_port=int(data["broker_port"]) if "broker_port" in data and data["broker_port"] is not None else None,
            pairing_token=data.get("pt") or data.get("pairing_token"),
            version=str(data.get("v") or data.get("version") or PAIRING_VERSION),
            server_url=data.get("server_url"),
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


__all__ = [
    " DEFAULT_TOKEN_TTL_SEC",
    "PAIRING_SCHEME",
    "PAIRING_VERSION",
    "PairingToken",
    "SessionPairingManager",
    "SlotAssignment",
    "build_compact_pair_code",
    "build_qr_payload",
    "build_termux_cli_command",
    "build_web_pair_url",
    "encode_qr_matrix",
    "generate_session_id",
    "parse_pairing_input",
]
