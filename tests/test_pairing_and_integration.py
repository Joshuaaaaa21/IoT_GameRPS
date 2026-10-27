"""End-to-end integration tests for QR Pairing (Player A & B), Temporary Tokens, Slot Protection,
Dynamic Termux Session Assignment, Heartbeat Timeout, Reconnect, and Full Game Flow."""

from http.server import ThreadingHTTPServer
import json
import os
import threading
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.edge.edge_client import EdgeDeviceClient
from src.edge.sensor_gesture import SensorSample, TermuxSensorReader
from src.engine.game_engine import Choice, GameEngine, GameState, PLAYER_A, PLAYER_B
from src.engine.mqtt_bridge import EngineMQTTBridge
from src.engine.mqtt_protocol import TopicBuilder
from src.engine.pairing import (
    PairingToken,
    SessionPairingManager,
    build_compact_pair_code,
    build_qr_payload,
    build_web_pair_url,
    encode_qr_matrix,
    generate_session_id,
    parse_pairing_input,
)
from src.engine.server import GameRuntimeContext, create_http_handler, public_browser_config


class TestPairingAndIntegration(unittest.TestCase):
    def test_pairing_qr_and_fallback_formats(self) -> None:
        sid = generate_session_id("rps")
        token_a = PairingToken(
            session_id=sid,
            player_id=PLAYER_A,
            broker_host="broker.hivemq.com",
            broker_port=8883,
            pairing_token="temp-tok-123",
        )

        uri = build_qr_payload(token_a)
        self.assertIn("v=1", uri)
        self.assertIn(sid, uri)
        self.assertIn("pt=temp-tok-123", uri)
        self.assertNotIn("password", uri.lower())
        parsed_uri = parse_pairing_input(uri)
        self.assertEqual(parsed_uri.session_id, sid)
        self.assertEqual(parsed_uri.player_id, PLAYER_A)
        self.assertEqual(parsed_uri.pairing_token, "temp-tok-123")

        web_url = build_web_pair_url("http://192.168.1.10:8080", sid, pairing_token="tok99", player_id=PLAYER_B)
        parsed_web = parse_pairing_input(web_url)
        self.assertEqual(parsed_web.session_id, sid)
        self.assertEqual(parsed_web.player_id, PLAYER_B)
        self.assertEqual(parsed_web.pairing_token, "tok99")

        qr_mat = encode_qr_matrix(web_url)
        self.assertGreaterEqual(len(qr_mat), 21)
        self.assertEqual(len(qr_mat), len(qr_mat[0]))

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

    def test_session_pairing_manager_join_tokens_expiry_third_player_and_duplicates(self) -> None:
        """
        Verify Tahap 7 requirements:
        - Player A & B join
        - Username & identity stored
        - Expired QR temporary token rejected
        - Invalid session rejected
        - Duplicate join from same device allowed (reconnect/idempotent)
        - Third player rejected (SESSION_FULL or SLOT_ALREADY_OCCUPIED)
        """
        sid = "rps-pair-test01"
        mgr = SessionPairingManager(session_id=sid, ttl_seconds=60.0, now=1000.0)
        pt = mgr.pairing_token

        # 1. Invalid session rejected
        ok, reason, _ = mgr.join_session("wrong-session", username="Alice", pairing_token=pt, now=1005.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "INVALID_SESSION")

        # 2. Invalid token rejected
        ok, reason, _ = mgr.join_session(sid, username="Alice", pairing_token="bad-token", now=1005.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "INVALID_PAIRING_TOKEN")

        # 3. Expired token rejected (now=1070 > expires_at=1060)
        ok, reason, _ = mgr.join_session(sid, username="Alice", pairing_token=pt, now=1070.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "EXPIRED_PAIRING_TOKEN")

        # 4. Player A joins via AUTO slot
        ok_a, reason_a, assign_a = mgr.join_session(
            sid, username="Andi", requested_slot="AUTO", pairing_token=pt, device_id="dev-hp-a", now=1010.0
        )
        self.assertTrue(ok_a)
        self.assertEqual(reason_a, "PAIRED_OK")
        assert assign_a is not None
        self.assertEqual(assign_a.player_id, PLAYER_A)
        self.assertEqual(assign_a.username, "Andi")

        # 5. Duplicate scan/join from HP A preserves PLAYER_A slot and updates username cleanly
        ok_a2, _, assign_a2 = mgr.join_session(
            sid, username="Andi Baru", requested_slot="AUTO", pairing_token=pt, device_id="dev-hp-a", now=1012.0
        )
        self.assertTrue(ok_a2)
        assert assign_a2 is not None
        self.assertEqual(assign_a2.player_id, PLAYER_A)
        self.assertEqual(assign_a2.username, "Andi Baru")

        # 6. Another phone trying to hijack PLAYER_A explicitly is rejected
        ok_hijack, reason_hijack, _ = mgr.join_session(
            sid, username="Hacker", requested_slot="PLAYER_A", pairing_token=pt, device_id="dev-hp-b", now=1015.0
        )
        self.assertFalse(ok_hijack)
        self.assertEqual(reason_hijack, "SLOT_ALREADY_OCCUPIED")

        # 7. Player B joins via AUTO slot -> receives PLAYER_B
        ok_b, reason_b, assign_b = mgr.join_session(
            sid, username="Budi", requested_slot="AUTO", pairing_token=pt, device_id="dev-hp-b", now=1016.0
        )
        self.assertTrue(ok_b)
        assert assign_b is not None
        self.assertEqual(assign_b.player_id, PLAYER_B)
        self.assertEqual(assign_b.username, "Budi")

        # 8. Third player trying to join when both slots are occupied is rejected (SESSION_FULL)
        ok_c, reason_c, _ = mgr.join_session(
            sid, username="Charlie", requested_slot="AUTO", pairing_token=pt, device_id="dev-hp-c", now=1018.0
        )
        self.assertFalse(ok_c)
        self.assertEqual(reason_c, "SESSION_FULL")

        # 9. Player B leaves session -> Charlie can now join as PLAYER_B
        left_ok, _ = mgr.leave_session(PLAYER_B, device_id="dev-hp-b")
        self.assertTrue(left_ok)
        ok_c2, _, assign_c2 = mgr.join_session(
            sid, username="Charlie", requested_slot="AUTO", pairing_token=pt, device_id="dev-hp-c", now=1020.0
        )
        self.assertTrue(ok_c2)
        assert assign_c2 is not None
        self.assertEqual(assign_c2.player_id, PLAYER_B)

    def test_dynamic_qr_to_termux_mqtt_assignment_and_old_session_isolation(self) -> None:
        """
        Verify Tahap 3 & 4:
        - Running Termux EdgeDeviceClient in standby/auto-pair mode receives MQTT pairing assignment
          when user joins via Web QR and automatically switches session & starts sending telemetry.
        - Old session messages cannot alter new session scores.
        - Heartbeat timeout detects silent disconnects, and reconnect preserves player score & identity.
        """
        ctx = GameRuntimeContext(session_id="sesi-awal-01")
        initial_sid = ctx.engine.session_id

        # Put Edge A & Edge B in auto-pair standby mode (initially on 'rps_standby')
        edge_a = EdgeDeviceClient(
            token=parse_pairing_input("rps_standby:A"),
            simulate=True,
            device_id="dev-a",
            standby_waiting_pair=True,
        )
        edge_b = EdgeDeviceClient(
            token=parse_pairing_input("rps_standby:B"),
            simulate=True,
            device_id="dev-b",
            standby_waiting_pair=True,
        )

        # Wire bi-directional MQTT routing between Server Bridge and the two Termux Edge Clients
        def server_publish(topic: str, payload: str, qos: int, retain: bool) -> None:
            if topic == TopicBuilder(ctx.engine.session_id).game_state():
                edge_a.handle_game_state_message(payload)
                edge_b.handle_game_state_message(payload)
            elif topic in (TopicBuilder.pairing_slot(PLAYER_A), TopicBuilder.pairing_device("dev-a")):
                edge_a.handle_pairing_assignment_message(payload)
            elif topic in (TopicBuilder.pairing_slot(PLAYER_B), TopicBuilder.pairing_device("dev-b")):
                edge_b.handle_pairing_assignment_message(payload)

        ctx.bridge._publish_fn = server_publish

        def edge_publish(topic: str, payload: str, qos: int, retain: bool) -> None:
            ctx.bridge.handle_raw_message(topic, payload)

        edge_a.virtual_player._publish_fn = edge_publish
        edge_b.virtual_player._publish_fn = edge_publish

        rest = [SensorSample(0.0, 0.0, 9.81, timestamp=1.0)]
        edge_a.classifier.calibrate(rest)
        edge_b.classifier.calibrate(rest)

        # Before QR join, edge clients are in standby and engine is WAITING_FOR_PLAYERS
        self.assertTrue(edge_a.standby_waiting_pair)
        self.assertEqual(ctx.engine.state, GameState.WAITING_FOR_PLAYERS)

        # Player A and Player B scan QR and Join on web -> triggers MQTT pairing assignment to Termux!
        pt = ctx.pairing_manager.pairing_token
        res_a = ctx.join_pairing(session_id=initial_sid, username="Rina", requested_slot="PLAYER_A", pairing_token=pt, device_id="dev-a")
        res_b = ctx.join_pairing(session_id=initial_sid, username="Dodi", requested_slot="PLAYER_B", pairing_token=pt, device_id="dev-b")
        self.assertTrue(res_a["ok"])
        self.assertTrue(res_b["ok"])

        # Both Termux clients automatically switched to 'sesi-awal-01' and announced telemetry!
        self.assertFalse(edge_a.standby_waiting_pair)
        self.assertFalse(edge_b.standby_waiting_pair)
        self.assertEqual(edge_a.session_id, initial_sid)
        self.assertEqual(edge_b.session_id, initial_sid)
        self.assertEqual(ctx.engine.state, GameState.READY)
        self.assertEqual(ctx.engine.players[PLAYER_A].username, "Rina")
        self.assertEqual(ctx.engine.players[PLAYER_B].username, "Dodi")

        # Play Round 1: Rina (ROCK) vs Dodi (SCISSORS) -> Rina wins (score 1 - 0)
        ctx.bridge.handle_raw_message(
            TopicBuilder(initial_sid).game_command(),
            json.dumps({"session_id": initial_sid, "command": "START_GAME"}),
        )
        for s in TermuxSensorReader.generate_simulated_gesture_stream("LEFT", base_time=20.0):
            edge_a.feed_sensor_sample(s)
        for s in TermuxSensorReader.generate_simulated_gesture_stream("RIGHT", base_time=20.2):
            edge_b.feed_sensor_sample(s)
        self.assertEqual(ctx.engine.players[PLAYER_A].score, 1)

        # Test Heartbeat Timeout & Reconnect:
        # Simulate 20 seconds passing without telemetry from Player A (> 15s heartbeat_timeout_sec)
        last_ts_a = ctx.bridge.last_telemetry_features[PLAYER_A]["last_telemetry_ts"]
        ctx.bridge.tick(now=last_ts_a + 20.0)
        self.assertFalse(ctx.engine.players[PLAYER_A].connected)
        self.assertEqual(ctx.bridge.last_telemetry_features[PLAYER_A]["diag_reason"], "HEARTBEAT_TIMEOUT")

        # Player A reconnects (sends heartbeat) -> online again and score (1) + username ("Rina") preserved!
        edge_a.connect_and_announce()
        self.assertTrue(ctx.engine.players[PLAYER_A].connected)
        self.assertEqual(ctx.engine.players[PLAYER_A].score, 1)
        self.assertEqual(ctx.engine.players[PLAYER_A].username, "Rina")

        # Create a brand new session on Dashboard ('sesi-baru-02') and verify stale messages from 'sesi-awal-01' are rejected
        stale_client = EdgeDeviceClient(
            token=parse_pairing_input(f"{initial_sid}:PLAYER_A"),
            simulate=True,
            publish_fn=edge_publish,
        )
        new_sid = ctx.create_new_session("sesi-baru-02")
        self.assertEqual(new_sid, "sesi-baru-02")
        ok_stale, reason_stale = ctx.bridge.handle_raw_message(
            TopicBuilder(initial_sid).player_choice(PLAYER_A),
            json.dumps({"session_id": initial_sid, "player_id": PLAYER_A, "choice": "ROCK", "round_id": 1}),
        )
        self.assertFalse(ok_stale)
        self.assertEqual(reason_stale, "SESSION_ID_MISMATCH")
        self.assertEqual(ctx.engine.players[PLAYER_A].score, 0)

    def test_public_browser_config_never_leaks_password(self) -> None:
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

    def test_live_telemetry_visible_outside_round_and_session_switch_preserves_mqtt(self) -> None:
        sid = "live-diag-01"
        engine = GameEngine(session_id=sid, countdown_seconds=5.0)
        bridge = EngineMQTTBridge(engine=engine, publish_fn=lambda t, p, q, r: None)

        fake_mqtt = MagicMock()
        bridge._client = fake_mqtt
        new_engine = GameEngine(session_id="live-diag-02", countdown_seconds=5.0)
        bridge.switch_session(new_engine)
        self.assertIs(bridge._client, fake_mqtt)
        fake_mqtt.unsubscribe.assert_called_once()
        self.assertEqual(fake_mqtt.subscribe.call_count, 3)

        def route_to_bridge(topic: str, payload: str, qos: int, retain: bool) -> None:
            bridge.handle_raw_message(topic, payload)

        token_a = parse_pairing_input("live-diag-02:PLAYER_A")
        edge_a = EdgeDeviceClient(token=token_a, simulate=True, publish_fn=route_to_bridge)
        edge_a.classifier.calibrate([SensorSample(0.0, 0.0, 9.81, timestamp=1.0)])
        edge_a.connect_and_announce()

        for s in TermuxSensorReader.generate_simulated_gesture_stream("LEFT", base_time=10.0):
            edge_a.feed_sensor_sample(s)

        snap = bridge.publish_state()
        p_a = snap["players"][PLAYER_A]
        self.assertEqual(p_a["last_gesture"], "ROCK")
        self.assertEqual(p_a["triggered_axis"], "-ax")
        self.assertEqual(p_a["score"], 0)

    def test_dashboard_and_mobile_pair_http_endpoints(self) -> None:
        ctx = GameRuntimeContext(session_id="web-test-01")
        server = ThreadingHTTPServer(("127.0.0.1", 0), create_http_handler(ctx))
        port = server.server_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        base_url = f"http://127.0.0.1:{port}"

        def http_get(path: str) -> dict:
            with urlopen(f"{base_url}{path}") as resp:
                return json.loads(resp.read().decode("utf-8"))

        def http_post(path: str, data: dict) -> tuple[int, dict]:
            req = Request(
                f"{base_url}{path}",
                data=json.dumps(data).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urlopen(req) as resp:
                    return resp.status, json.loads(resp.read().decode("utf-8"))
            except HTTPError as err:
                try:
                    return err.code, json.loads(err.read().decode("utf-8"))
                finally:
                    err.close()

        try:
            # Verify /pair HTML page is served
            with urlopen(f"{base_url}/pair") as pair_resp:
                html_text = pair_resp.read().decode("utf-8")
                self.assertIn("Scan QR Code Sesi", html_text)

            st = http_get("/api/state")
            self.assertEqual(st["session_id"], "web-test-01")
            self.assertTrue(len(st["pairing"]["session_qr_matrix"]) >= 21)
            pt = st["pairing"]["pairing_token"]

            # Join Player A & Player B via /api/pair/join
            code_a, join_a = http_post("/api/pair/join", {
                "qr_input": st["pairing"]["PLAYER_A"]["web_pair_url"],
                "username": "Siti",
                "device_id": "hp-siti",
            })
            self.assertEqual(code_a, 200)
            self.assertTrue(join_a["ok"])
            self.assertEqual(join_a["player_id"], PLAYER_A)

            code_b, join_b = http_post("/api/pair/join", {
                "session_id": "web-test-01",
                "pairing_token": pt,
                "player_id": "AUTO",
                "username": "Bambang",
                "device_id": "hp-bambang",
            })
            self.assertEqual(code_b, 200)
            self.assertTrue(join_b["ok"])
            self.assertEqual(join_b["player_id"], PLAYER_B)

            # 3rd device rejected with 400 SESSION_FULL
            code_c, join_c = http_post("/api/pair/join", {
                "session_id": "web-test-01",
                "pairing_token": pt,
                "player_id": "AUTO",
                "username": "Tamu",
                "device_id": "hp-tamu",
            })
            self.assertEqual(code_c, 400)
            self.assertEqual(join_c["reason"], "SESSION_FULL")

            # Check polling endpoint /api/pair/assignment for Termux auto-pair
            assign_poll = http_get("/api/pair/assignment?device_id=hp-siti")
            self.assertTrue(assign_poll["assigned"])
            self.assertEqual(assign_poll["player_id"], PLAYER_A)
            self.assertEqual(assign_poll["username"], "Siti")

            # Verify web pairing alone sets status to PAIRED (not READY until sensor connects)
            st_paired = http_get("/api/state")
            self.assertEqual(st_paired["players"][PLAYER_A]["pairing_status"], "PAIRED")
            self.assertFalse(st_paired["players"][PLAYER_A]["connected"])

            # Now connect actual sensor telemetry for A and B -> status becomes READY
            http_post("/api/simulate_edge", {"player_id": PLAYER_A, "action": "CONNECT"})
            http_post("/api/simulate_edge", {"player_id": PLAYER_B, "action": "CONNECT"})

            st_ready = http_get("/api/state")
            self.assertEqual(st_ready["players"][PLAYER_A]["pairing_status"], "READY")
            self.assertEqual(st_ready["state"], "READY")

            http_post("/api/command", {"command": "START_GAME"})
            http_post("/api/simulate_edge", {"player_id": PLAYER_A, "action": "GESTURE", "gesture": "LEFT"})
            http_post("/api/simulate_edge", {"player_id": PLAYER_B, "action": "GESTURE", "gesture": "UP"})

            st_after = http_get("/api/state")
            self.assertEqual(st_after["state"], "FINISHED")
            self.assertEqual(st_after["players"][PLAYER_B]["score"], 1)
            self.assertEqual(st_after["last_result"]["winner_username"], "Bambang")

            # End session
            _, end_res = http_post("/api/end_session", {})
            self.assertTrue(end_res["ok"])
            st_ended = http_get("/api/state")
            self.assertFalse(st_ended["session_active"])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
