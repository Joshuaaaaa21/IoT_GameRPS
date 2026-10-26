"""End-to-end integration tests for Pairing, EdgeDeviceClient, Dashboard Server, and Full Game Flow."""

from http.server import ThreadingHTTPServer
import json
import os
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

from src.edge.edge_client import EdgeDeviceClient
from src.edge.sensor_gesture import SensorSample, TermuxSensorReader
from src.engine.game_engine import Choice, GameEngine, GameState, PLAYER_A, PLAYER_B
from src.engine.mqtt_bridge import EngineMQTTBridge
from src.engine.mqtt_protocol import TopicBuilder
from src.engine.pairing import (
    PairingToken,
    build_compact_pair_code,
    build_qr_payload,
    generate_session_id,
    parse_pairing_input,
)
from src.engine.server import GameRuntimeContext, create_http_handler, public_browser_config


class TestPairingAndIntegration(unittest.TestCase):
    def test_pairing_qr_and_fallback_formats(self) -> None:
        sid = generate_session_id("rps")
        token_a = PairingToken(session_id=sid, player_id=PLAYER_A, broker_host="broker.hivemq.com", broker_port=8883)

        uri = build_qr_payload(token_a)
        self.assertIn(sid, uri)
        self.assertNotIn("password", uri.lower())
        parsed_uri = parse_pairing_input(uri)
        self.assertEqual(parsed_uri.session_id, sid)
        self.assertEqual(parsed_uri.player_id, PLAYER_A)

        code = build_compact_pair_code(token_a)
        parsed_code = parse_pairing_input(code)
        self.assertEqual(parsed_code.session_id, sid)
        self.assertEqual(parsed_code.player_id, PLAYER_A)

        parsed_short = parse_pairing_input(f"{sid}:b")
        self.assertEqual(parsed_short.player_id, PLAYER_B)

        with self.assertRaises(ValueError):
            parse_pairing_input(f"motionrps://pair?session={sid}&player=A&password=123")
        with self.assertRaises(ValueError):
            parse_pairing_input(json.dumps({"session_id": sid, "player_id": "A", "password": "secret"}))

    def test_public_browser_config_never_leaks_password(self) -> None:
        """Verify /config.json security rule adapted from PocketGuard test_dashboard_server.py."""
        with patch.dict(os.environ, {
            "PROJECT_ID": "rps_secret_test",
            "MQTT_HOST": "private.example.com",
            "MQTT_WS_URL": "wss://private.example.com/mqtt",
            "MQTT_USERNAME": "secret-user",
            "MQTT_PASSWORD": "DO_NOT_LEAK_PASSWORD_123",
        }):
            cfg = public_browser_config()
            self.assertEqual(cfg["project_id"], "rps_secret_test")
            self.assertFalse(cfg["automatic_connection"])
            self.assertNotIn("DO_NOT_LEAK_PASSWORD_123", json.dumps(cfg))
            self.assertNotIn("secret-user", json.dumps(cfg))

    def test_full_edge_sensor_to_mqtt_to_engine_integration(self) -> None:
        sid = "integ-session-77"
        engine = GameEngine(session_id=sid, countdown_seconds=5.0)
        topics = TopicBuilder(sid)

        edge_a: EdgeDeviceClient
        edge_b: EdgeDeviceClient

        def engine_publish(topic: str, payload: str, qos: int, retain: bool) -> None:
            if topic == topics.game_state():
                edge_a.handle_game_state_message(payload)
                edge_b.handle_game_state_message(payload)

        bridge = EngineMQTTBridge(engine=engine, publish_fn=engine_publish)

        def edge_publish(topic: str, payload: str, qos: int, retain: bool) -> None:
            # Inject current wall-clock time so `sent_at_ms` freshness check passes
            bridge.handle_raw_message(topic, payload)

        token_a = parse_pairing_input(f"{sid}:PLAYER_A")
        token_b = parse_pairing_input(f"{sid}:PLAYER_B")

        edge_a = EdgeDeviceClient(token=token_a, simulate=True, publish_fn=edge_publish)
        edge_b = EdgeDeviceClient(token=token_b, simulate=True, publish_fn=edge_publish)

        rest = [SensorSample(0.0, 0.0, 9.81, timestamp=1.0)]
        edge_a.classifier.calibrate(rest)
        edge_b.classifier.calibrate(rest)

        edge_a.connect_and_announce()
        edge_b.connect_and_announce()
        self.assertEqual(engine.state, GameState.READY)

        # Disconnect & Reconnect test
        edge_a.virtual_player.send_telemetry(connected=False)
        self.assertEqual(engine.state, GameState.WAITING_FOR_PLAYERS)
        edge_a.connect_and_announce()
        self.assertEqual(engine.state, GameState.READY)

        bridge.handle_raw_message(
            topics.game_command(),
            json.dumps({"session_id": sid, "command": "START_GAME"}),
        )
        self.assertTrue(edge_a.round_active)
        self.assertTrue(edge_b.round_active)

        for sample in TermuxSensorReader.generate_simulated_gesture_stream("UP", base_time=51.0):
            edge_a.feed_sensor_sample(sample)

        for sample in TermuxSensorReader.generate_simulated_gesture_stream("LEFT", base_time=51.5):
            edge_b.feed_sensor_sample(sample)

        self.assertEqual(engine.state, GameState.FINISHED)
        self.assertEqual(engine.players[PLAYER_A].score, 1)
        self.assertEqual(engine.players[PLAYER_B].score, 0)
        self.assertEqual(engine.last_result.player_a_choice, Choice.PAPER.value)
        self.assertEqual(engine.last_result.player_b_choice, Choice.ROCK.value)

    def test_dashboard_http_server_integration(self) -> None:
        ctx = GameRuntimeContext(session_id="web-test-01")
        server = ThreadingHTTPServer(("127.0.0.1", 0), create_http_handler(ctx))
        port = server.server_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        base_url = f"http://127.0.0.1:{port}"

        def http_get(path: str) -> dict:
            with urlopen(f"{base_url}{path}") as resp:
                return json.loads(resp.read().decode("utf-8"))

        def http_post(path: str, data: dict) -> dict:
            req = Request(
                f"{base_url}{path}",
                data=json.dumps(data).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req) as resp:
                return json.loads(resp.read().decode("utf-8"))

        try:
            # Verify /config.json endpoint
            pub_cfg = http_get("/config.json")
            self.assertEqual(pub_cfg["session_id"], "web-test-01")

            st = http_get("/api/state")
            self.assertEqual(st["session_id"], "web-test-01")
            self.assertEqual(st["state"], "WAITING_FOR_PLAYERS")

            http_post("/api/simulate_edge", {"player_id": PLAYER_A, "action": "CONNECT"})
            http_post("/api/simulate_edge", {"player_id": PLAYER_B, "action": "CONNECT"})

            http_post("/api/command", {"command": "SET_USERNAME", "player_id": PLAYER_B, "username": "Rival"})
            http_post("/api/command", {"command": "START_GAME"})

            http_post("/api/simulate_edge", {"player_id": PLAYER_A, "action": "GESTURE", "gesture": "LEFT"})
            http_post("/api/simulate_edge", {"player_id": PLAYER_B, "action": "GESTURE", "gesture": "UP"})

            st_after = http_get("/api/state")
            self.assertEqual(st_after["state"], "FINISHED")
            self.assertEqual(st_after["players"][PLAYER_B]["score"], 1)
            self.assertEqual(st_after["last_result"]["winner_username"], "Rival")

            http_post("/api/command", {"command": "RESET_SCORE", "confirm": True})
            st_reset = http_get("/api/state")
            self.assertEqual(st_reset["players"][PLAYER_B]["score"], 0)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
