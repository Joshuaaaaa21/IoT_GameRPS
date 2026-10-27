"""MQTT Bridge connecting the authoritative GameEngine with edge devices and dashboard.

Portions adapted from PocketGuard IoT (https://github.com/ruthhhs/iot-projek)
under the MIT License — Copyright (c) 2026 PocketGuard IoT contributors.
"""

import json
import ssl
import time
import uuid
from typing import Any, Callable, Dict, Optional, Tuple
import paho.mqtt.client as mqtt

from src.engine.game_engine import GameEngine, GameState
from src.engine.mqtt_protocol import (
    MQTTConfig,
    QOS_CRITICAL,
    QOS_TELEMETRY,
    STALE_WINDOW_MS,
    TopicBuilder,
    parse_json_payload,
    validate_session_and_player,
)


class EngineMQTTBridge:
    """
    Wraps GameEngine and handles incoming/outgoing MQTT messages for a specific session_id.
    Stores full live telemetry & gesture diagnostic metadata per player so the Dashboard
    can render real-time sensor readings, classification reasons, and pipeline timestamps.
    """

    def __init__(
        self,
        engine: GameEngine,
        publish_fn: Optional[Callable[[str, str, int, bool], None]] = None,
        heartbeat_timeout_sec: float = 15.0,
    ) -> None:
        self.engine = engine
        self.topics = TopicBuilder(engine.session_id)
        self._client: Optional[mqtt.Client] = None
        self._publish_fn = publish_fn or self._default_publish
        self.heartbeat_timeout_sec: float = float(heartbeat_timeout_sec)
        self._seen_msg_ids: set[str] = set()
        self._last_seq: Dict[Tuple[str, str], int] = {}
        self.last_telemetry_features: Dict[str, Dict[str, Any]] = {}
        self.last_error: Optional[str] = None

    def publish_pairing_assignment(
        self,
        action: str,
        session_id: str,
        player_id: str,
        username: str = "",
        device_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Publish dynamic QR pairing assignment over MQTT so a running Termux edge_client
        in --auto-pair mode automatically joins or leaves the session without manual typing.
        """
        payload = {
            "v": 1,
            "action": action,
            "session_id": session_id,
            "player_id": player_id,
            "username": username,
            "device_id": device_id,
            "timestamp": time.time(),
        }
        raw = json.dumps(payload)
        self._publish_fn(TopicBuilder.pairing_slot(player_id), raw, QOS_CRITICAL, True)
        if device_id:
            self._publish_fn(TopicBuilder.pairing_device(device_id), raw, QOS_CRITICAL, True)
        return payload

    def _default_publish(self, topic: str, payload: str, qos: int, retain: bool) -> None:
        if self._client is not None:
            self._client.publish(topic, payload=payload, qos=qos, retain=retain)

    def switch_session(self, new_engine: GameEngine) -> None:
        """
        Safely switch the active session_id and re-subscribe the live MQTT client
        so creating a new session on the Dashboard never orphans the MQTT connection.
        """
        old_topics = self.topics
        self.engine = new_engine
        self.topics = TopicBuilder(new_engine.session_id)
        self._seen_msg_ids.clear()
        self._last_seq.clear()
        self.last_telemetry_features.clear()
        self.last_error = None

        if self._client is not None:
            try:
                self._client.unsubscribe([
                    old_topics.wildcard_all_player_telemetry(),
                    old_topics.wildcard_all_player_choices(),
                    old_topics.game_command(),
                ])
                self._client.subscribe(self.topics.wildcard_all_player_telemetry(), qos=QOS_TELEMETRY)
                self._client.subscribe(self.topics.wildcard_all_player_choices(), qos=QOS_CRITICAL)
                self._client.subscribe(self.topics.game_command(), qos=QOS_CRITICAL)
            except Exception:
                pass
        self.publish_state()

    def bind_mqtt_client(self, config: MQTTConfig) -> mqtt.Client:
        """Create and configure a paho-mqtt v2 Client with TLS enforcement, LWT, and auto-reconnect."""
        config.validate_tls_policy()
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"{self.engine.session_id}-engine-{uuid.uuid4().hex[:8]}",
            protocol=mqtt.MQTTv311,
        )
        if config.username:
            client.username_pw_set(config.username, config.password)
        if config.use_tls:
            client.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)
        client.reconnect_delay_set(min_delay=1, max_delay=30)

        offline_payload = json.dumps({
            "session_id": self.engine.session_id,
            "state": "OFFLINE",
            "error": "Engine disconnected from MQTT broker",
            "timestamp": time.time(),
        })
        client.will_set(self.topics.game_state(), payload=offline_payload, qos=QOS_CRITICAL, retain=True)

        client.on_connect = self._on_connect
        client.on_message = self._on_message
        self._client = client
        return client

    def _on_connect(self, client: mqtt.Client, userdata: Any, flags: Any, reason_code: Any, properties: Any = None) -> None:
        if hasattr(reason_code, "is_failure") and reason_code.is_failure:
            return
        client.subscribe(self.topics.wildcard_all_player_telemetry(), qos=QOS_TELEMETRY)
        client.subscribe(self.topics.wildcard_all_player_choices(), qos=QOS_CRITICAL)
        client.subscribe(self.topics.game_command(), qos=QOS_CRITICAL)
        self.publish_state()

    def _on_message(self, client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage) -> None:
        self.handle_raw_message(msg.topic, msg.payload)

    def _check_freshness_and_replay(
        self,
        sender_id: str,
        payload: Dict[str, Any],
        now: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """Validate optional `sent_at_ms` (30s window) and `(boot_id, sequence)` monotonic counter."""
        sent_at_ms = payload.get("sent_at_ms")
        if sent_at_ms is not None:
            if type(sent_at_ms) is not int or sent_at_ms < 0:
                return False, "INVALID_TIMESTAMP"
            current_ms = int((time.time() if now is None else float(now)) * 1000)
            if abs(current_ms - sent_at_ms) > STALE_WINDOW_MS:
                return False, "REJECTED_STALE"

        boot_id = payload.get("boot_id")
        sequence = payload.get("sequence")
        if boot_id is not None or sequence is not None:
            if not isinstance(boot_id, str) or not boot_id.strip():
                return False, "INVALID_BOOT_ID"
            if type(sequence) is not int or sequence < 0:
                return False, "INVALID_SEQUENCE"
            key = (sender_id, boot_id)
            if sequence <= self._last_seq.get(key, -1):
                return False, "REJECTED_REPLAY"
            self._last_seq[key] = sequence

        return True, "OK"

    def _enrich_players_with_telemetry(self, snapshot: Dict[str, Any]) -> None:
        """Attach live telemetry & diagnostic fields to each player's state snapshot."""
        for pid, p_dict in snapshot.get("players", {}).items():
            feats = self.last_telemetry_features.get(pid, {})
            p_dict["motion_rms"] = feats.get("motion_rms", 0.0)
            p_dict["rotation_rad"] = feats.get("rotation_rad", 0.0)
            p_dict["accel"] = feats.get("accel", [0.0, 0.0, 0.0])
            p_dict["gyro"] = feats.get("gyro", [0.0, 0.0, 0.0])
            p_dict["diag_status"] = feats.get("diag_status", "WAITING")
            p_dict["diag_reason"] = feats.get("diag_reason", "NO_TELEMETRY_YET")
            p_dict["last_gesture"] = feats.get("last_gesture", None)
            p_dict["triggered_axis"] = feats.get("triggered_axis", None)
            p_dict["last_telemetry_ts"] = feats.get("last_telemetry_ts", None)
            p_dict["sequence"] = feats.get("sequence", 0)

    def publish_state(self, error_msg: Optional[str] = None) -> Dict[str, Any]:
        """Publish authoritative game state snapshot to motion-rps/{session_id}/game/state."""
        snapshot = self.engine.get_state_snapshot()
        self._enrich_players_with_telemetry(snapshot)
        snapshot["timestamp"] = time.time()
        snapshot["error"] = error_msg
        self.last_error = error_msg
        self._publish_fn(
            self.topics.game_state(),
            json.dumps(snapshot),
            QOS_CRITICAL,
            True,
        )
        return snapshot

    def publish_result(self) -> Optional[Dict[str, Any]]:
        """Publish the latest round result to motion-rps/{session_id}/game/result."""
        if self.engine.last_result is None:
            return None
        res = self.engine.last_result
        payload = {
            "session_id": self.engine.session_id,
            "round_id": res.round_id,
            "outcome": res.outcome.value,
            "winner_id": res.winner_id,
            "winner_username": res.winner_username,
            "player_a_choice": res.player_a_choice,
            "player_b_choice": res.player_b_choice,
            "player_a_outcome": res.player_a_outcome,
            "player_b_outcome": res.player_b_outcome,
            "scores": res.scores,
            "reason": res.reason,
            "timestamp": time.time(),
        }
        self._publish_fn(
            self.topics.game_result(),
            json.dumps(payload),
            QOS_CRITICAL,
            False,
        )
        return payload

    def tick(self, now: Optional[float] = None) -> bool:
        """Periodic check for round countdown expiration and player telemetry heartbeat timeouts."""
        current_time = time.time() if now is None else float(now)
        state_changed = False

        if self.heartbeat_timeout_sec > 0:
            for pid, p_state in self.engine.players.items():
                if p_state.connected:
                    feats = self.last_telemetry_features.get(pid, {})
                    last_ts = feats.get("last_telemetry_ts")
                    if last_ts is not None and (current_time - float(last_ts)) > self.heartbeat_timeout_sec:
                        self.engine.set_player_connection(pid, False)
                        feats["diag_status"] = "OFFLINE"
                        feats["diag_reason"] = "HEARTBEAT_TIMEOUT"
                        state_changed = True

        result = self.engine.check_timeout(now=current_time)
        if result is not None:
            self.publish_result()
            self.publish_state()
            return True

        if state_changed:
            self.publish_state()
            return True
        return False

    def handle_raw_message(
        self,
        topic: str,
        raw_payload: bytes | str,
        now: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """Route and validate an incoming MQTT message by topic."""
        parts = topic.split("/")
        if len(parts) < 4 or parts[0] != "motion-rps":
            return False, "IGNORED_UNKNOWN_TOPIC"

        topic_session = parts[1]
        if topic_session != self.engine.session_id:
            return False, "SESSION_ID_MISMATCH"

        payload, err = parse_json_payload(raw_payload)
        if err or payload is None:
            self.publish_state(error_msg=err)
            return False, err or "INVALID_JSON"

        msg_id = payload.get("msg_id")
        if msg_id:
            if msg_id in self._seen_msg_ids:
                return False, "IGNORED_DUPLICATE_MSG_ID"
            self._seen_msg_ids.add(str(msg_id))

        if len(parts) == 5 and parts[2] == "player":
            player_id = parts[3]
            subtopic = parts[4]
            valid, reason = validate_session_and_player(payload, self.engine.session_id, player_id)
            if not valid:
                self.publish_state(error_msg=reason)
                return False, reason

            fresh_ok, fresh_reason = self._check_freshness_and_replay(player_id, payload, now=now)
            if not fresh_ok:
                self.publish_state(error_msg=fresh_reason)
                return False, fresh_reason

            if subtopic == "telemetry":
                return self._handle_telemetry(player_id, payload, now=now)
            if subtopic == "choice":
                return self._handle_choice(player_id, payload, now=now)

        if len(parts) == 4 and parts[2] == "game" and parts[3] == "command":
            valid, reason = validate_session_and_player(payload, self.engine.session_id)
            if not valid:
                self.publish_state(error_msg=reason)
                return False, reason
            return self._handle_command(payload, now=now)

        return False, "IGNORED_UNHANDLED_TOPIC"

    def _update_player_telemetry_cache(
        self,
        player_id: str,
        payload: Dict[str, Any],
        now: Optional[float] = None,
    ) -> None:
        recv_ts = float(now) if now is not None else time.time()
        prev = self.last_telemetry_features.get(player_id, {})
        accel = payload.get("accel", prev.get("accel", [0.0, 0.0, 0.0]))
        gyro = payload.get("gyro", prev.get("gyro", [0.0, 0.0, 0.0]))
        last_gest = payload.get("last_gesture") or payload.get("choice") or prev.get("last_gesture")

        self.last_telemetry_features[player_id] = {
            "motion_rms": float(payload.get("motion_rms", prev.get("motion_rms", 0.0))),
            "rotation_rad": float(payload.get("rotation_rad", prev.get("rotation_rad", 0.0))),
            "accel": accel if isinstance(accel, list) and len(accel) == 3 else [0.0, 0.0, 0.0],
            "gyro": gyro if isinstance(gyro, list) and len(gyro) == 3 else [0.0, 0.0, 0.0],
            "diag_status": str(payload.get("diag_status", prev.get("diag_status", "WAITING"))),
            "diag_reason": str(payload.get("diag_reason", prev.get("diag_reason", "TELEMETRY_RECEIVED"))),
            "last_gesture": last_gest,
            "triggered_axis": payload.get("triggered_axis", prev.get("triggered_axis")),
            "sequence": int(payload.get("sequence", prev.get("sequence", 0))),
            "last_telemetry_ts": recv_ts,
        }

    def _handle_telemetry(
        self,
        player_id: str,
        payload: Dict[str, Any],
        now: Optional[float] = None,
    ) -> Tuple[bool, str]:
        connected = bool(payload.get("connected", True))
        self._update_player_telemetry_cache(player_id, payload, now=now)
        self.engine.set_player_connection(player_id, connected)
        self.publish_state()
        return True, "TELEMETRY_OK"

    def _handle_choice(
        self,
        player_id: str,
        payload: Dict[str, Any],
        now: Optional[float] = None,
    ) -> Tuple[bool, str]:
        raw_choice = str(payload.get("choice", ""))
        round_id = payload.get("round_id")
        prev_state = self.engine.state

        # Record live diagnostic/gesture even if round is not in COUNTDOWN so dashboard shows it
        choice_diag_payload = dict(payload)
        choice_diag_payload["diag_status"] = "DETECTED"
        choice_diag_payload["last_gesture"] = raw_choice.strip().upper()
        self._update_player_telemetry_cache(player_id, choice_diag_payload, now=now)

        accepted, reason = self.engine.submit_choice(
            player_id=player_id,
            raw_choice=raw_choice,
            round_id=round_id,
            now=now,
        )
        if not accepted:
            self.last_telemetry_features[player_id]["diag_reason"] = f"CHOICE_{reason}"
            self.publish_state()
            return False, reason

        self.last_telemetry_features[player_id]["diag_reason"] = f"LOCKED_ROUND_{self.engine.round_id} ({raw_choice.strip().upper()})"
        if prev_state == GameState.COUNTDOWN and self.engine.state == GameState.FINISHED:
            self.publish_result()
        self.publish_state()
        return True, "CHOICE_ACCEPTED"

    def _handle_command(
        self,
        payload: Dict[str, Any],
        now: Optional[float] = None,
    ) -> Tuple[bool, str]:
        command = str(payload.get("command", "")).strip().upper()
        try:
            if command == "START_GAME":
                self.engine.start_round(now=now)
                self.publish_state()
                return True, "ROUND_STARTED"

            if command == "RESET_SCORE":
                confirm = bool(payload.get("confirm", False))
                self.engine.reset_scores(confirm=confirm)
                self.publish_state()
                return True, "SCORE_RESET"

            if command == "SET_USERNAME":
                player_id = str(payload.get("player_id", ""))
                username = str(payload.get("username", ""))
                self.engine.set_username(player_id, username)
                self.publish_state()
                return True, "USERNAME_UPDATED"

            return False, "UNKNOWN_COMMAND"
        except (ValueError, RuntimeError) as exc:
            self.publish_state(error_msg=str(exc))
            return False, str(exc)
