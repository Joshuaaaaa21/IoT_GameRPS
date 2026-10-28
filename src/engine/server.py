"""Lightweight HTTP + MQTT Server for Motion RPS IoT Dashboard, Mobile QR Pairing, and Authoritative GameEngine.

Portions adapted from PocketGuard IoT (`shared/dashboard_server.py`, https://github.com/ruthhhs/iot-projek)
under the MIT License — Copyright (c) 2026 PocketGuard IoT contributors.
"""

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
import threading
import time
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from src.edge.sensor_gesture import GestureClassifier, SensorSample, TermuxSensorReader
from src.engine.game_engine import GameEngine, PLAYER_A, PLAYER_B, VALID_PLAYERS
from src.engine.mqtt_bridge import EngineMQTTBridge
from src.engine.mqtt_protocol import DEFAULT_HOST, DEFAULT_WS_URL, MQTTConfig, TopicBuilder
from src.engine.pairing import (
    PairingToken,
    SessionPairingManager,
    build_compact_pair_code,
    build_qr_payload,
    build_termux_cli_command,
    build_web_pair_url,
    encode_qr_matrix,
    generate_session_id,
    parse_pairing_input,
)

DASHBOARD_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "dashboard")


def detect_lan_ip() -> str:
    """Detect local LAN IPv4 address so QR codes opened on localhost still work when scanned by phones on the same Wi-Fi."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.2)
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
    except Exception:
        pass
    return "127.0.0.1"


def public_browser_config(session_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Return public browser MQTT/session configuration without exposing MQTT_PASSWORD.
    Adapted from PocketGuard IoT `shared/dashboard_server.py`.
    """
    cfg = MQTTConfig.from_env()
    project = (
        session_id
        or os.getenv("PROJECT_ID")
        or os.getenv("DEFAULT_SESSION_ID")
        or "rps_kelompok_07"
    ).strip()
    ws_url = cfg.websocket_url
    if not ws_url and cfg.host == DEFAULT_HOST:
        ws_url = DEFAULT_WS_URL

    return {
        "project_id": project,
        "session_id": project,
        "websocket_url": ws_url,
        "automatic_connection": bool(ws_url and not cfg.username),
    }


class GameRuntimeContext:
    """Holds the single authoritative GameEngine, MQTT bridge, QR SessionPairingManager, and connection status."""

    def __init__(
        self,
        session_id: Optional[str] = None,
        countdown_seconds: float = 5.0,
        token_ttl_seconds: float = 600.0,
        public_base_url: Optional[str] = None,
    ) -> None:
        self.lock = threading.Lock()
        env_sid = os.getenv("PROJECT_ID") or os.getenv("DEFAULT_SESSION_ID")
        initial_sid = session_id or env_sid or generate_session_id("rps")
        self.countdown_seconds = countdown_seconds
        self.mqtt_config = MQTTConfig.from_env()
        self.mqtt_connected: bool = False
        self.mqtt_status_text: str = "LOCAL_READY (MQTT broker optional/configurable in .env)"
        self.lan_ip: str = detect_lan_ip()
        self.port: int = int(os.getenv("DASHBOARD_PORT", "8080"))
        self.public_base_url: Optional[str] = public_base_url or os.getenv("PUBLIC_BASE_URL")
        self.engine = GameEngine(session_id=initial_sid, countdown_seconds=countdown_seconds)
        self.bridge = EngineMQTTBridge(engine=self.engine)
        self.pairing_manager = SessionPairingManager(session_id=initial_sid, ttl_seconds=token_ttl_seconds)
        self.simulated_players = set()

    def resolve_base_url(self, host_header: Optional[str] = None) -> str:
        if self.public_base_url:
            return self.public_base_url.rstrip("/")
        if host_header:
            clean_host = host_header.strip()
            if clean_host.startswith(("localhost", "127.0.0.1", "0.0.0.0")):
                port_part = clean_host.split(":", 1)[1] if ":" in clean_host else str(self.port)
                return f"http://{self.lan_ip}:{port_part}"
            return f"http://{clean_host}"
        return f"http://{self.lan_ip}:{self.port}"

    def create_new_session(self, custom_session_id: Optional[str] = None) -> str:
        with self.lock:
            old_sid = self.engine.session_id
            self.simulated_players.clear()
            new_sid = custom_session_id.strip() if custom_session_id else generate_session_id("rps")
            for pid in (PLAYER_A, PLAYER_B):
                self.bridge.publish_pairing_assignment(
                    action="LEAVE_SESSION",
                    session_id=old_sid,
                    player_id=pid,
                )
            new_engine = GameEngine(session_id=new_sid, countdown_seconds=self.countdown_seconds)
            self.engine = new_engine
            self.bridge.switch_session(new_engine)
            self.pairing_manager.reset_for_session(new_sid)
            return new_sid

    def end_session(self) -> Dict[str, Any]:
        with self.lock:
            sid = self.engine.session_id
            self.simulated_players.clear()
            self.pairing_manager.end_session()
            for pid in (PLAYER_A, PLAYER_B):
                self.engine.set_player_connection(pid, False)
                if pid in self.bridge.last_telemetry_features:
                    self.bridge.last_telemetry_features[pid]["diag_status"] = "OFFLINE"
                    self.bridge.last_telemetry_features[pid]["diag_reason"] = "SESSION_ENDED"
                self.bridge.publish_pairing_assignment(
                    action="LEAVE_SESSION",
                    session_id=sid,
                    player_id=pid,
                )
            self.bridge.publish_state()
            return {"ok": True, "session_id": sid, "session_active": False}

    def _set_simulated_connection(self, player_id: str, connected: bool) -> tuple[bool, str]:
        action = "CONNECT" if connected else "DISCONNECT"
        ok, reason = self.bridge.handle_raw_message(
            TopicBuilder(self.engine.session_id).player_telemetry(player_id),
            json.dumps({
                "session_id": self.engine.session_id,
                "player_id": player_id,
                "connected": connected,
                "motion_rms": 0.0,
                "rotation_rad": 0.0,
                "accel": [0.0, 0.0, 9.81],
                "gyro": [0.0, 0.0, 0.0],
                "diag_status": "WAITING" if connected else "OFFLINE",
                "diag_reason": f"SIMULATED_{action}",
            }),
        )
        if ok:
            if connected:
                self.simulated_players.add(player_id)
            else:
                self.simulated_players.discard(player_id)
        return ok, reason

    def join_pairing(
        self,
        session_id: str,
        username: str,
        requested_slot: Optional[str] = None,
        pairing_token: Optional[str] = None,
        device_id: Optional[str] = None,
        client_token: Optional[str] = None,
        require_token: bool = False,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        with self.lock:
            sensor_map = {
                PLAYER_A: self.engine.players[PLAYER_A].connected,
                PLAYER_B: self.engine.players[PLAYER_B].connected,
            }
            ok, reason, assign = self.pairing_manager.join_session(
                session_id=session_id,
                username=username,
                requested_slot=requested_slot,
                pairing_token=pairing_token,
                device_id=device_id,
                client_token=client_token,
                require_token=require_token,
                sensor_connected_map=sensor_map,
                now=now,
            )
            if not ok or assign is None:
                return {"ok": False, "reason": reason}

            self.engine.set_username(assign.player_id, assign.username)
            token_obj = PairingToken(
                session_id=self.engine.session_id,
                player_id=assign.player_id,
                broker_host=self.mqtt_config.host,
            )
            self.bridge.publish_pairing_assignment(
                action="ASSIGN_SESSION",
                session_id=self.engine.session_id,
                player_id=assign.player_id,
                username=assign.username,
                device_id=assign.device_id,
            )
            self.bridge.publish_state()
            return {
                "ok": True,
                "reason": reason,
                "session_id": self.engine.session_id,
                "player_id": assign.player_id,
                "username": self.engine.players[assign.player_id].username,
                "device_id": assign.device_id,
                "client_token": assign.client_token,
                "sensor_connected": self.engine.players[assign.player_id].connected,
                "pair_code": build_compact_pair_code(token_obj),
                "termux_cmd": build_termux_cli_command(token_obj, simulate=False),
                "auto_termux_cmd": f"bash scripts/start_edge.sh {assign.player_id[-1]}",
            }

    def leave_pairing(
        self,
        player_id: str,
        device_id: Optional[str] = None,
        client_token: Optional[str] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        with self.lock:
            ok, reason = self.pairing_manager.leave_session(
                player_id=player_id,
                device_id=device_id,
                client_token=client_token,
                force=force,
            )
            if not ok:
                return {"ok": False, "reason": reason}

            norm_pid = PLAYER_A if player_id.strip().upper().endswith("A") else PLAYER_B
            if norm_pid in VALID_PLAYERS:
                self.engine.set_player_connection(norm_pid, False)
                if norm_pid in self.bridge.last_telemetry_features:
                    self.bridge.last_telemetry_features[norm_pid]["diag_status"] = "OFFLINE"
                    self.bridge.last_telemetry_features[norm_pid]["diag_reason"] = "PLAYER_LEFT_SESSION"
                self.bridge.publish_pairing_assignment(
                    action="LEAVE_SESSION",
                    session_id=self.engine.session_id,
                    player_id=norm_pid,
                    device_id=device_id,
                )
                self.bridge.publish_state()
            return {"ok": True, "reason": reason, "player_id": norm_pid}

    def get_edge_assignment(
        self,
        player_id: Optional[str] = None,
        device_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return active session & slot assignment for a polling Termux edge client."""
        with self.lock:
            if not self.pairing_manager.session_active:
                return {"ok": False, "session_active": False, "reason": "SESSION_ENDED"}

            # 1. Match by device_id if paired via web
            if device_id:
                for pid, assign in self.pairing_manager.slots.items():
                    if assign and assign.device_id == device_id.strip():
                        return {
                            "ok": True,
                            "assigned": True,
                            "session_id": self.engine.session_id,
                            "player_id": pid,
                            "username": assign.username,
                            "device_id": assign.device_id,
                            "web_paired": True,
                        }

            # 2. Match by slot (PLAYER_A or PLAYER_B)
            if player_id:
                norm_pid = PLAYER_A if player_id.strip().upper() in ("A", "PLAYER_A") else PLAYER_B
                assign = self.pairing_manager.slots.get(norm_pid)
                return {
                    "ok": True,
                    "assigned": assign is not None,
                    "session_id": self.engine.session_id,
                    "player_id": norm_pid,
                    "username": self.engine.players[norm_pid].username,
                    "device_id": assign.device_id if assign else None,
                    "web_paired": assign is not None,
                }

            return {
                "ok": True,
                "assigned": False,
                "session_id": self.engine.session_id,
                "session_active": self.pairing_manager.session_active,
            }

    def get_pairing_info(self, host_header: Optional[str] = None) -> Dict[str, Any]:
        sid = self.engine.session_id
        pt = self.pairing_manager.pairing_token if self.pairing_manager.session_active else ""
        base_url = self.resolve_base_url(host_header)
        session_web_url = build_web_pair_url(base_url, sid, pairing_token=pt)

        info: Dict[str, Any] = {
            "version": "1",
            "session_active": self.pairing_manager.session_active,
            "pairing_token": pt,
            "expires_at": self.pairing_manager.expires_at,
            "base_url": base_url,
            "session_pair_url": session_web_url,
            "session_qr_matrix": encode_qr_matrix(session_web_url) if self.pairing_manager.session_active else [],
        }
        for pid in (PLAYER_A, PLAYER_B):
            short_slot = "A" if pid == PLAYER_A else "B"
            token = PairingToken(
                session_id=sid,
                player_id=pid,
                broker_host=self.mqtt_config.host,
                pairing_token=pt or None,
            )
            qr_uri = build_qr_payload(token)
            web_url = f"{base_url}/pair?v=1&session={sid}&player={short_slot}" + (f"&pt={pt}" if pt else "")
            info[pid] = {
                "player_id": pid,
                "qr_uri": qr_uri,
                "web_pair_url": web_url,
                "qr_matrix": encode_qr_matrix(web_url) if self.pairing_manager.session_active else [],
                "pair_code": build_compact_pair_code(token),
                "termux_cmd": build_termux_cli_command(token, simulate=False),
                "auto_termux_cmd": f"bash scripts/start_edge.sh {short_slot}",
                "sim_cmd": build_termux_cli_command(token, simulate=True),
                **self.pairing_manager.get_slot_snapshot(pid),
            }
        return info

    def build_full_dashboard_payload(self, host_header: Optional[str] = None) -> Dict[str, Any]:
        with self.lock:
            if self.pairing_manager.session_active:
                for player_id in tuple(self.simulated_players):
                    self._set_simulated_connection(player_id, True)
            self.bridge.tick()
            snapshot = self.engine.get_state_snapshot()
            self.bridge._enrich_players_with_telemetry(snapshot)

            for pid, p_dict in snapshot.get("players", {}).items():
                slot_meta = self.pairing_manager.get_slot_snapshot(pid)
                p_dict["web_paired"] = slot_meta["web_paired"]
                p_dict["device_id"] = slot_meta["device_id"]
                if p_dict["connected"]:
                    p_dict["pairing_status"] = "READY"
                elif slot_meta["web_paired"]:
                    p_dict["pairing_status"] = "PAIRED"
                else:
                    p_dict["pairing_status"] = "WAITING"

            snapshot["session_active"] = self.pairing_manager.session_active
            snapshot["mqtt"] = {
                "connected": self.mqtt_connected,
                "broker_host": self.mqtt_config.host,
                "broker_port": self.mqtt_config.port,
                "use_tls": self.mqtt_config.use_tls,
                "websocket_url": self.mqtt_config.websocket_url,
                "status_text": self.mqtt_status_text,
            }
            snapshot["pairing"] = self.get_pairing_info(host_header=host_header)
            snapshot["error"] = self.bridge.last_error
            snapshot["server_time"] = time.time()
            return snapshot


def create_http_handler(ctx: GameRuntimeContext):
    class DashboardRequestHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(self, data: Dict[str, Any], status: int = HTTPStatus.OK) -> None:
            raw = json.dumps(data).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _serve_static(self, filename: str, content_type: str) -> None:
            full_path = os.path.join(DASHBOARD_DIR, filename)
            if not os.path.isfile(full_path):
                self.send_error(HTTPStatus.NOT_FOUND, "File not found")
                return
            with open(full_path, "rb") as f:
                content = f.read()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            host_hdr = self.headers.get("Host")
            if path in ("/", "/index.html"):
                self._serve_static("index.html", "text/html; charset=utf-8")
            elif path in ("/pair", "/pair.html"):
                self._serve_static("pair.html", "text/html; charset=utf-8")
            elif path == "/styles.css":
                self._serve_static("styles.css", "text/css; charset=utf-8")
            elif path == "/app.js":
                self._serve_static("app.js", "application/javascript; charset=utf-8")
            elif path == "/pair.js":
                self._serve_static("pair.js", "application/javascript; charset=utf-8")
            elif path == "/config.json":
                self._send_json(public_browser_config(ctx.engine.session_id))
            elif path == "/api/state":
                self._send_json(ctx.build_full_dashboard_payload(host_header=host_hdr))
            elif path == "/api/pair/assignment":
                qs = parse_qs(parsed.query)
                pid = (qs.get("player_id") or qs.get("player") or [None])[0]
                dev_id = (qs.get("device_id") or [None])[0]
                self._send_json(ctx.get_edge_assignment(player_id=pid, device_id=dev_id))
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            try:
                body = json.loads(raw_body) if raw_body else {}
            except json.JSONDecodeError:
                self._send_json({"ok": False, "error": "INVALID_JSON"}, HTTPStatus.BAD_REQUEST)
                return

            if path == "/api/new_session":
                custom_sid = body.get("session_id")
                try:
                    sid = ctx.create_new_session(custom_sid)
                except ValueError as exc:
                    self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                self._send_json({"ok": True, "session_id": sid, "state": ctx.engine.get_state_snapshot()})
                return

            if path == "/api/end_session":
                res = ctx.end_session()
                self._send_json(res)
                return

            if path == "/api/pair/join":
                qr_input = str(body.get("qr_input") or "").strip()
                session_id = str(body.get("session_id") or "").strip()
                requested_slot = body.get("player_id") or body.get("slot")
                pairing_token = body.get("pairing_token") or body.get("pt")
                username = str(body.get("username") or "").strip()
                device_id = body.get("device_id")
                client_token = body.get("client_token")

                if qr_input:
                    try:
                        has_explicit_slot = any(
                            k in qr_input for k in ("player=", "player_id=", "slot=", ":")
                        )
                        parsed_tok = parse_pairing_input(qr_input)
                        session_id = parsed_tok.session_id
                        if not requested_slot and has_explicit_slot:
                            requested_slot = parsed_tok.player_id
                        if not pairing_token and parsed_tok.pairing_token:
                            pairing_token = parsed_tok.pairing_token
                    except ValueError as exc:
                        self._send_json({"ok": False, "reason": "INVALID_QR_PAYLOAD", "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                        return

                res = ctx.join_pairing(
                    session_id=session_id,
                    username=username,
                    requested_slot=requested_slot,
                    pairing_token=pairing_token,
                    device_id=device_id,
                    client_token=client_token,
                    require_token=bool(body.get("require_token", False)),
                )
                status_code = HTTPStatus.OK if res.get("ok") else HTTPStatus.BAD_REQUEST
                self._send_json(res, status_code)
                return

            if path == "/api/pair/leave":
                player_id = str(body.get("player_id") or "")
                device_id = body.get("device_id")
                client_token = body.get("client_token")
                res = ctx.leave_pairing(
                    player_id=player_id,
                    device_id=device_id,
                    client_token=client_token,
                    force=bool(body.get("force", False)),
                )
                status_code = HTTPStatus.OK if res.get("ok") else HTTPStatus.BAD_REQUEST
                self._send_json(res, status_code)
                return

            with ctx.lock:
                topics = TopicBuilder(ctx.engine.session_id)

                if path == "/api/command":
                    if (
                        str(body.get("command", "")).strip().upper() == "SET_USERNAME"
                        and not ctx.pairing_manager.session_active
                    ):
                        self._send_json(
                            {"ok": False, "reason": "SESSION_ENDED"},
                            HTTPStatus.CONFLICT,
                        )
                        return
                    payload = dict(body)
                    payload["session_id"] = ctx.engine.session_id
                    ok, reason = ctx.bridge.handle_raw_message(
                        topics.game_command(),
                        json.dumps(payload),
                    )
                    self._send_json({"ok": ok, "reason": reason})
                    return

                if path == "/api/simulate_edge":
                    player_id = str(body.get("player_id", PLAYER_A))
                    action = str(body.get("action", "CONNECT")).upper()
                    if action in ("CONNECT", "DISCONNECT"):
                        ok, reason = ctx._set_simulated_connection(
                            player_id,
                            connected=(action == "CONNECT"),
                        )
                        self._send_json({"ok": ok, "reason": reason})
                        return

                    if action == "GESTURE":
                        gesture = str(body.get("gesture", "LEFT")).upper()
                        clf = GestureClassifier()
                        clf.calibrate([SensorSample(0.0, 0.0, 9.81, timestamp=1.0)])
                        detected_choice = None
                        peak_sample: Optional[SensorSample] = None
                        peak_diag = clf.last_diagnostic
                        for s in TermuxSensorReader.generate_simulated_gesture_stream(gesture, base_time=10.0):
                            c = clf.process_sample(s)
                            if c is not None:
                                detected_choice = c.value
                                peak_sample = s
                                peak_diag = clf.last_diagnostic
                            elif peak_sample is None and clf.last_diagnostic.status == "REJECTED":
                                peak_sample = s
                                peak_diag = clf.last_diagnostic

                        final_sample = peak_sample or SensorSample(0.0, 0.0, 9.81, timestamp=10.4)
                        feats = clf.last_features

                        # 1. Always publish live telemetry so Dashboard shows sensor & diagnostic state
                        ctx.bridge.handle_raw_message(
                            topics.player_telemetry(player_id),
                            json.dumps({
                                "session_id": ctx.engine.session_id,
                                "player_id": player_id,
                                "connected": True,
                                "motion_rms": feats.motion_rms,
                                "rotation_rad": feats.rotation_rad,
                                "accel": list(final_sample.acceleration),
                                "gyro": list(final_sample.gyroscope),
                                "diag_status": peak_diag.status,
                                "diag_reason": peak_diag.reason,
                                "last_gesture": detected_choice,
                                "triggered_axis": peak_diag.triggered_axis,
                            }),
                        )

                        if not detected_choice:
                            self._send_json({
                                "ok": False,
                                "reason": peak_diag.reason,
                                "choice": None,
                                "diag_status": peak_diag.status,
                            })
                            return

                        # 2. Submit choice (if round is active, locks choice; if outside round, updates diagnostic reason)
                        ok, reason = ctx.bridge.handle_raw_message(
                            topics.player_choice(player_id),
                            json.dumps({
                                "session_id": ctx.engine.session_id,
                                "player_id": player_id,
                                "choice": detected_choice,
                                "round_id": ctx.engine.round_id,
                                "motion_rms": feats.motion_rms,
                                "rotation_rad": feats.rotation_rad,
                                "accel": list(final_sample.acceleration),
                                "gyro": list(final_sample.gyroscope),
                                "triggered_axis": peak_diag.triggered_axis,
                                "diag_reason": peak_diag.reason,
                            }),
                        )
                        self._send_json({
                            "ok": ok,
                            "reason": reason,
                            "choice": detected_choice,
                            "motion_rms": feats.motion_rms,
                            "rotation_rad": feats.rotation_rad,
                        })
                        return

            self.send_error(HTTPStatus.NOT_FOUND, "Unknown endpoint")

    return DashboardRequestHandler


def main() -> None:
    parser = argparse.ArgumentParser(description="Motion RPS IoT Authoritative Engine & Dashboard Server")
    parser.add_argument("--host", default="0.0.0.0", help="HTTP bind address (default 0.0.0.0 for LAN mobile access)")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("DASHBOARD_PORT", "8080")),
        help="HTTP port (default from DASHBOARD_PORT or 8080)",
    )
    parser.add_argument("--session", default=None, help="Initial session ID")
    parser.add_argument("--connect-mqtt", action="store_true", help="Connect to live MQTT broker from .env")
    args = parser.parse_args()

    ctx = GameRuntimeContext(session_id=args.session)
    ctx.port = args.port

    if args.connect_mqtt:
        try:
            client = ctx.bridge.bind_mqtt_client(ctx.mqtt_config)
            client.connect_async(ctx.mqtt_config.host, ctx.mqtt_config.port, keepalive=30)
            client.loop_start()
            ctx.mqtt_connected = True
            ctx.mqtt_status_text = f"CONNECTING/ACTIVE ({ctx.mqtt_config.host}:{ctx.mqtt_config.port})"
        except Exception as exc:
            ctx.mqtt_connected = False
            ctx.mqtt_status_text = f"MQTT_ERROR: {exc}"

    server = ThreadingHTTPServer((args.host, args.port), create_http_handler(ctx))
    print(f"Motion RPS Server berjalan di http://127.0.0.1:{args.port} | LAN HP: http://{ctx.lan_ip}:{args.port}/pair (Session: {ctx.engine.session_id})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
