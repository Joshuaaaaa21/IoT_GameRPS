"""Unit and simulation tests for MQTT protocol, EngineMQTTBridge, and VirtualPlayer."""

import json
import unittest
from src.edge.simulator import VirtualPlayer, run_local_simulation_demo
from src.engine.game_engine import GameEngine, GameState, PLAYER_A, PLAYER_B, RoundOutcome
from src.engine.mqtt_bridge import EngineMQTTBridge
from src.engine.mqtt_protocol import MQTTConfig, TopicBuilder


class TestMQTTSimulation(unittest.TestCase):
    def setUp(self) -> None:
        self.session_id = "session-xyz-99"
        self.topics = TopicBuilder(self.session_id)
        self.engine = GameEngine(session_id=self.session_id, countdown_seconds=5.0)
        self.outbox: list[tuple[str, dict, int, bool]] = []

        def capture_publish(topic: str, payload: str, qos: int, retain: bool) -> None:
            self.outbox.append((topic, json.loads(payload), qos, retain))

        self.bridge = EngineMQTTBridge(engine=self.engine, publish_fn=capture_publish)

        def send_to_bridge(topic: str, payload: str, qos: int, retain: bool) -> None:
            self.bridge.handle_raw_message(topic, payload, now=100.0)

        self.player_a = VirtualPlayer(self.session_id, PLAYER_A, publish_fn=send_to_bridge)
        self.player_b = VirtualPlayer(self.session_id, PLAYER_B, publish_fn=send_to_bridge)

    def test_topic_builder_isolation(self) -> None:
        self.assertEqual(
            self.topics.player_telemetry(PLAYER_A),
            "motion-rps/session-xyz-99/player/PLAYER_A/telemetry",
        )
        self.assertEqual(
            self.topics.player_choice(PLAYER_B),
            "motion-rps/session-xyz-99/player/PLAYER_B/choice",
        )
        self.assertEqual(
            self.topics.game_command(),
            "motion-rps/session-xyz-99/game/command",
        )
        self.assertEqual(
            self.topics.game_state(),
            "motion-rps/session-xyz-99/game/state",
        )
        self.assertEqual(
            self.topics.game_result(),
            "motion-rps/session-xyz-99/game/result",
        )
        with self.assertRaises(ValueError):
            TopicBuilder("invalid/session/id")

    def test_two_virtual_players_full_mqtt_round_flow(self) -> None:
        # 1. Players connect
        self.player_a.send_telemetry(connected=True, now_ms=100_000)
        self.player_b.send_telemetry(connected=True, now_ms=100_000)
        self.assertEqual(self.engine.state, GameState.READY)

        # 2. Dashboard updates usernames via MQTT command
        self.bridge.handle_raw_message(
            self.topics.game_command(),
            json.dumps({
                "session_id": self.session_id,
                "command": "SET_USERNAME",
                "player_id": PLAYER_A,
                "username": "Shion",
            }),
        )
        self.assertEqual(self.engine.players[PLAYER_A].username, "Shion")

        # 3. Dashboard starts game
        ok, reason = self.bridge.handle_raw_message(
            self.topics.game_command(),
            json.dumps({"session_id": self.session_id, "command": "START_GAME"}),
            now=100.0,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "ROUND_STARTED")
        self.assertEqual(self.engine.state, GameState.COUNTDOWN)

        # 4. Players send choices via MQTT
        self.player_a.send_choice("PAPER", round_id=1, now_ms=101_000)
        self.player_b.send_choice("ROCK", round_id=1, now_ms=101_500)

        # 5. Verify round result & state published to MQTT outbox
        self.assertEqual(self.engine.state, GameState.FINISHED)
        self.assertEqual(self.engine.players[PLAYER_A].score, 1)
        self.assertEqual(self.engine.players[PLAYER_B].score, 0)

        result_msgs = [m for m in self.outbox if m[0] == self.topics.game_result()]
        self.assertEqual(len(result_msgs), 1)
        self.assertEqual(result_msgs[0][1]["outcome"], RoundOutcome.PLAYER_A_WIN.value)
        self.assertEqual(result_msgs[0][1]["winner_username"], "Shion")

    def test_mqtt_validation_session_player_and_deduplication(self) -> None:
        self.player_a.send_telemetry(connected=True, now_ms=100_000)
        self.player_b.send_telemetry(connected=True, now_ms=100_000)
        self.bridge.handle_raw_message(
            self.topics.game_command(),
            json.dumps({"session_id": self.session_id, "command": "START_GAME"}),
            now=100.0,
        )

        # Wrong session ID in topic
        ok_top, res_top = self.bridge.handle_raw_message(
            "motion-rps/wrong-session/player/PLAYER_A/choice",
            json.dumps({"session_id": "wrong-session", "player_id": PLAYER_A, "choice": "ROCK"}),
        )
        self.assertFalse(ok_top)
        self.assertEqual(res_top, "SESSION_ID_MISMATCH")

        # Mismatched player_id in payload vs topic
        ok_p, res_p = self.bridge.handle_raw_message(
            self.topics.player_choice(PLAYER_A),
            json.dumps({"session_id": self.session_id, "player_id": PLAYER_B, "choice": "ROCK"}),
        )
        self.assertFalse(ok_p)
        self.assertEqual(res_p, "PLAYER_ID_MISMATCH")

        # Duplicate msg_id protection (e.g. QoS 1 re-delivery)
        self.player_a.send_choice("SCISSORS", round_id=1, msg_id="fixed-msg-001", now_ms=101_000)
        ok_dup, res_dup = self.bridge.handle_raw_message(
            self.topics.player_choice(PLAYER_A),
            json.dumps({
                "msg_id": "fixed-msg-001",
                "session_id": self.session_id,
                "player_id": PLAYER_A,
                "choice": "SCISSORS",
                "round_id": 1,
            }),
        )
        self.assertFalse(ok_dup)
        self.assertEqual(res_dup, "IGNORED_DUPLICATE_MSG_ID")

    def test_replay_sequence_stale_timestamp_and_non_finite_rejected(self) -> None:
        """Verify anti-replay (boot_id + sequence), stale timestamp (>30s), and math.isfinite checks adapted from reference."""
        # 1. Valid sequence 1
        ok1, reason1 = self.bridge.handle_raw_message(
            self.topics.player_telemetry(PLAYER_A),
            json.dumps({
                "session_id": self.session_id,
                "player_id": PLAYER_A,
                "boot_id": "boot-abc",
                "sequence": 1,
                "sent_at_ms": 100_000,
                "motion_rms": 1.2,
                "rotation_rad": 0.4,
            }),
            now=100.0,
        )
        self.assertTrue(ok1)
        self.assertEqual(reason1, "TELEMETRY_OK")

        # 2. Replay attack with same sequence 1 -> REJECTED_REPLAY
        ok_rep, reason_rep = self.bridge.handle_raw_message(
            self.topics.player_telemetry(PLAYER_A),
            json.dumps({
                "session_id": self.session_id,
                "player_id": PLAYER_A,
                "boot_id": "boot-abc",
                "sequence": 1,
                "sent_at_ms": 100_000,
            }),
            now=100.0,
        )
        self.assertFalse(ok_rep)
        self.assertEqual(reason_rep, "REJECTED_REPLAY")

        # 3. Stale message (> 30s difference) -> REJECTED_STALE
        ok_stale, reason_stale = self.bridge.handle_raw_message(
            self.topics.player_telemetry(PLAYER_A),
            json.dumps({
                "session_id": self.session_id,
                "player_id": PLAYER_A,
                "boot_id": "boot-abc",
                "sequence": 2,
                "sent_at_ms": 60_000,  # 40s old relative to now=100.0
            }),
            now=100.0,
        )
        self.assertFalse(ok_stale)
        self.assertEqual(reason_stale, "REJECTED_STALE")

        # 4. Non-finite or string metric -> INVALID_FINITE_MOTION_RMS
        ok_inf, reason_inf = self.bridge.handle_raw_message(
            self.topics.player_telemetry(PLAYER_A),
            json.dumps({
                "session_id": self.session_id,
                "player_id": PLAYER_A,
                "motion_rms": "infinite",
                "rotation_rad": 0.0,
            }),
            now=100.0,
        )
        self.assertFalse(ok_inf)
        self.assertEqual(reason_inf, "INVALID_FINITE_MOTION_RMS")

    def test_tls_policy_enforcement(self) -> None:
        cfg_insecure = MQTTConfig(host="broker.hivemq.com", port=1883, use_tls=False, allow_insecure_local=False)
        with self.assertRaises(RuntimeError):
            cfg_insecure.validate_tls_policy()

        cfg_local = MQTTConfig(host="localhost", port=1883, use_tls=False, allow_insecure_local=True)
        cfg_local.validate_tls_policy()

    def test_tick_timeout_publishes_result(self) -> None:
        self.player_a.send_telemetry(connected=True, now_ms=100_000)
        self.player_b.send_telemetry(connected=True, now_ms=100_000)
        self.bridge.handle_raw_message(
            self.topics.game_command(),
            json.dumps({"session_id": self.session_id, "command": "START_GAME"}),
            now=100.0,
        )
        self.player_a.send_choice("SCISSORS", round_id=1, now_ms=101_000)

        # Before deadline (105.0)
        self.assertFalse(self.bridge.tick(now=103.0))
        # At deadline
        self.assertTrue(self.bridge.tick(now=105.5))
        self.assertEqual(self.engine.state, GameState.FINISHED)
        self.assertEqual(self.engine.last_result.outcome, RoundOutcome.FORFEIT_B)

    def test_standalone_simulation_demo_helper(self) -> None:
        snapshot = run_local_simulation_demo("demo-01")
        self.assertEqual(snapshot["state"], "FINISHED")
        self.assertEqual(snapshot["players"][PLAYER_A]["score"], 1)


if __name__ == "__main__":
    unittest.main()
