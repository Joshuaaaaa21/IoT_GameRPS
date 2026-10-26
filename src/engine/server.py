"""Lightweight HTTP + MQTT Server for Motion RPS IoT Dashboard and Authoritative GameEngine.

Portions adapted from PocketGuard IoT (`shared/dashboard_server.py`, https://github.com/ruthhhs/iot-projek)
under the MIT License — Copyright (c) 2026 PocketGuard IoT contributors.
"""

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from src.edge.sensor_gesture import SensorSample, TermuxSensorReader
from src.engine.game_engine import GameEngine, PLAYER_A, PLAYER_B
from src.engine.mqtt_bridge import EngineMQTTBridge
from src.engine.mqtt_protocol import DEFAULT_HOST, DEFAULT_WS_URL, MQTTConfig, TopicBuilder
from src.engine.pairing import (
    PairingToken,
    build_compact_pair_code,
    build_qr_payload,
    build_termux_cli_command,
    generate_session_id,
)

DASHBOARD_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "dashboard")


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
    """Holds the single authoritative GameEngine, MQTT bridge, and connection status."""

    def __init__(self, session_id: Optional[str] = None, countdown_seconds: float = 5.0) -> None:
        self.lock = threading.Lock()
        env_sid = os.getenv("PROJECT_ID") or os.getenv("DEFAULT_SESSION_ID")
        initial_sid = session_id or env_sid or generate_session_id("rps")
        self.countdown_seconds = countdown_seconds
        self.mqtt_config = MQTTConfig.from_env()
        self.mqtt_connected: bool = False
        self.mqtt_status_text: str = "LOCAL_READY (MQTT broker optional/configurable in .env)"
        self.engine = GameEngine(session_id=initial_sid, countdown_seconds=countdown_seconds)
        self.bridge = EngineMQTTBridge(engine=self.engine)

    def create_new_session(self, custom_session_id: Optional[str] = None) -> str:
        with self.lock:
            new_sid = custom_session_id.strip() if custom_session_id else generate_session_id("rps")
            self.engine = GameEngine(session_id=new_sid, countdown_seconds=self.countdown_seconds)
            self.bridge = EngineMQTTBridge(engine=self.engine)
            return new_sid

    def get_pairing_info(self) -> Dict[str, Any]:
        sid = self.engine.session_id
        info: Dict[str, Any] = {}
        for pid in (PLAYER_A, PLAYER_B):
            token = PairingToken(
                session_id=sid,
                player_id=pid,
                broker_host=self.mqtt_config.host,
            )
            info[pid] = {
                "player_id": pid,
                "qr_uri": build_qr_payload(token),
                "pair_code": build_compact_pair_code(token),
                "termux_cmd": build_termux_cli_command(token, simulate=False),
                "sim_cmd": build_termux_cli_command(token, simulate=True),
            }
        return info

    def build_full_dashboard_payload(self) -> Dict[str, Any]:
        with self.lock:
            self.bridge.tick()
            snapshot = self.engine.get_state_snapshot()
            for pid, feats in self.bridge.last_telemetry_features.items():
                if pid in snapshot["players"]:
                    snapshot["players"][pid]["motion_rms"] = feats.get("motion_rms", 0.0)
                    snapshot["players"][pid]["rotation_rad"] = feats.get("rotation_rad", 0.0)
            snapshot["mqtt"] = {
                "connected": self.mqtt_connected,
                "broker_host": self.mqtt_config.host,
                "broker_port": self.mqtt_config.port,
                "use_tls": self.mqtt_config.use_tls,
                "websocket_url": self.mqtt_config.websocket_url,
                "status_text": self.mqtt_status_text,
            }
            snapshot["pairing"] = self.get_pairing_info()
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
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._serve_static("index.html", "text/html; charset=utf-8")
            elif path == "/styles.css":
                self._serve_static("styles.css", "text/css; charset=utf-8")
            elif path == "/app.js":
                self._serve_static("app.js", "application/javascript; charset=utf-8")
            elif path == "/config.json":
                self._send_json(public_browser_config(ctx.engine.session_id))
            elif path == "/api/state":
                self._send_json(ctx.build_full_dashboard_payload())
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

            with ctx.lock:
                topics = TopicBuilder(ctx.engine.session_id)

                if path == "/api/new_session":
                    custom_sid = body.get("session_id")
                    try:
                        sid = ctx.create_new_session(custom_sid)
                    except ValueError as exc:
                        self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                        return
                    self._send_json({"ok": True, "session_id": sid, "state": ctx.engine.get_state_snapshot()})
                    return

                if path == "/api/command":
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
                        ok, reason = ctx.bridge.handle_raw_message(
                            topics.player_telemetry(player_id),
                            json.dumps({
                                "session_id": ctx.engine.session_id,
                                "player_id": player_id,
                                "connected": (action == "CONNECT"),
                                "motion_rms": 0.0,
                                "rotation_rad": 0.0,
                            }),
                        )
                        self._send_json({"ok": ok, "reason": reason})
                        return

                    if action == "GESTURE":
                        gesture = str(body.get("gesture", "LEFT")).upper()
                        from src.edge.sensor_gesture import GestureClassifier
                        clf = GestureClassifier()
                        clf.calibrate([SensorSample(0.0, 0.0, 9.81, timestamp=1.0)])
                        detected_choice = None
                        for s in TermuxSensorReader.generate_simulated_gesture_stream(gesture, base_time=10.0):
                            c = clf.process_sample(s)
                            if c is not None:
                                detected_choice = c.value
                        if not detected_choice:
                            self._send_json({"ok": False, "reason": "NO_GESTURE_DETECTED"})
                            return
                        feats = clf.last_features
                        ok, reason = ctx.bridge.handle_raw_message(
                            topics.player_choice(player_id),
                            json.dumps({
                                "session_id": ctx.engine.session_id,
                                "player_id": player_id,
                                "choice": detected_choice,
                                "round_id": ctx.engine.round_id,
                                "motion_rms": feats.motion_rms,
                                "rotation_rad": feats.rotation_rad,
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
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind address")
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
    print(f"Motion RPS Server berjalan di http://{args.host}:{args.port} (Session: {ctx.engine.session_id})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
