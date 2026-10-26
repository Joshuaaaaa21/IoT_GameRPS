"""Virtual player simulator and MQTT publisher helper for Motion RPS IoT.

Portions adapted from PocketGuard IoT (https://github.com/ruthhhs/iot-projek)
under the MIT License — Copyright (c) 2026 PocketGuard IoT contributors.
"""

import argparse
import json
import ssl
import time
import uuid
from typing import Any, Callable, Dict, Optional, Tuple
import paho.mqtt.client as mqtt

from src.engine.game_engine import PLAYER_A, PLAYER_B
from src.engine.mqtt_protocol import (
    MQTTConfig,
    QOS_CRITICAL,
    QOS_TELEMETRY,
    TopicBuilder,
)


class VirtualPlayer:
    """Publishes edge telemetry (with live sensor axes & diagnostics) and gesture choices over MQTT."""

    def __init__(
        self,
        session_id: str,
        player_id: str,
        publish_fn: Optional[Callable[[str, str, int, bool], None]] = None,
        boot_id: Optional[str] = None,
    ) -> None:
        self.session_id = session_id
        self.player_id = player_id
        self.topics = TopicBuilder(session_id)
        self.boot_id = boot_id or uuid.uuid4().hex[:12]
        self.sequence: int = 0
        self._client: Optional[mqtt.Client] = None
        self._publish_fn = publish_fn or self._default_publish

    def _default_publish(self, topic: str, payload: str, qos: int, retain: bool) -> None:
        if self._client is not None:
            self._client.publish(topic, payload=payload, qos=qos, retain=retain)

    def bind_mqtt_client(self, config: MQTTConfig) -> mqtt.Client:
        """Configure paho-mqtt client with TLS enforcement and LWT (Last Will and Testament)."""
        config.validate_tls_policy()
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"{self.session_id}-{self.player_id}-{uuid.uuid4().hex[:8]}",
            protocol=mqtt.MQTTv311,
        )
        if config.username:
            client.username_pw_set(config.username, config.password)
        if config.use_tls:
            client.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)
        client.reconnect_delay_set(min_delay=1, max_delay=30)

        lwt_payload = json.dumps({
            "session_id": self.session_id,
            "player_id": self.player_id,
            "connected": False,
            "status": "DISCONNECTED_LWT",
            "timestamp": time.time(),
        })
        client.will_set(
            self.topics.player_telemetry(self.player_id),
            payload=lwt_payload,
            qos=QOS_CRITICAL,
            retain=False,
        )
        self._client = client
        return client

    def send_telemetry(
        self,
        connected: bool = True,
        battery: int = 100,
        motion_rms: float = 0.0,
        rotation_rad: float = 0.0,
        accel: Optional[Tuple[float, float, float]] = None,
        gyro: Optional[Tuple[float, float, float]] = None,
        diag_status: str = "WAITING",
        diag_reason: str = "IDLE",
        last_gesture: Optional[str] = None,
        triggered_axis: Optional[str] = None,
        mode: str = "SIMULATION",
        now_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Publish connection status, live 3-axis sensor values, and gesture diagnostic state."""
        self.sequence += 1
        sent_at_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        ax, ay, az = accel if accel is not None else (0.0, 0.0, 9.81)
        gx, gy, gz = gyro if gyro is not None else (0.0, 0.0, 0.0)
        payload: Dict[str, Any] = {
            "msg_id": uuid.uuid4().hex[:12],
            "session_id": self.session_id,
            "player_id": self.player_id,
            "boot_id": self.boot_id,
            "sequence": self.sequence,
            "sent_at_ms": sent_at_ms,
            "connected": bool(connected),
            "mode": mode,
            "motion_rms": round(float(motion_rms), 4),
            "rotation_rad": round(float(rotation_rad), 4),
            "accel": [round(float(ax), 3), round(float(ay), 3), round(float(az), 3)],
            "gyro": [round(float(gx), 3), round(float(gy), 3), round(float(gz), 3)],
            "diag_status": diag_status,
            "diag_reason": diag_reason,
            "last_gesture": last_gesture,
            "triggered_axis": triggered_axis,
            "battery": battery,
            "timestamp": sent_at_ms / 1000.0,
        }
        self._publish_fn(
            self.topics.player_telemetry(self.player_id),
            json.dumps(payload),
            QOS_TELEMETRY,
            False,
        )
        return payload

    def send_choice(
        self,
        choice: str,
        round_id: Optional[int] = None,
        msg_id: Optional[str] = None,
        motion_rms: float = 0.0,
        rotation_rad: float = 0.0,
        accel: Optional[Tuple[float, float, float]] = None,
        gyro: Optional[Tuple[float, float, float]] = None,
        triggered_axis: Optional[str] = None,
        diag_reason: str = "DETECTED",
        now_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Publish a gesture choice (ROCK, PAPER, or SCISSORS) with sequence & sensor context."""
        self.sequence += 1
        sent_at_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        payload: Dict[str, Any] = {
            "msg_id": msg_id or uuid.uuid4().hex[:12],
            "session_id": self.session_id,
            "player_id": self.player_id,
            "boot_id": self.boot_id,
            "sequence": self.sequence,
            "sent_at_ms": sent_at_ms,
            "choice": choice.strip().upper(),
            "round_id": round_id,
            "motion_rms": round(float(motion_rms), 4),
            "rotation_rad": round(float(rotation_rad), 4),
            "triggered_axis": triggered_axis,
            "diag_reason": diag_reason,
            "source": "SIMULATOR",
            "timestamp": sent_at_ms / 1000.0,
        }
        if accel is not None:
            payload["accel"] = [round(float(accel[0]), 3), round(float(accel[1]), 3), round(float(accel[2]), 3)]
        if gyro is not None:
            payload["gyro"] = [round(float(gyro[0]), 3), round(float(gyro[1]), 3), round(float(gyro[2]), 3)]

        self._publish_fn(
            self.topics.player_choice(self.player_id),
            json.dumps(payload),
            QOS_CRITICAL,
            False,
        )
        return payload


def run_local_simulation_demo(session_id: str = "sim-session-01") -> dict:
    """
    Run an end-to-end in-memory MQTT simulation of 2 virtual players and the GameEngine.
    Returns the final engine state snapshot.
    """
    from src.engine.game_engine import GameEngine
    from src.engine.mqtt_bridge import EngineMQTTBridge

    engine = GameEngine(session_id=session_id, countdown_seconds=5.0)
    published_messages: list[tuple[str, dict]] = []

    def record_publish(topic: str, payload: str, qos: int, retain: bool) -> None:
        published_messages.append((topic, json.loads(payload)))

    bridge = EngineMQTTBridge(engine=engine, publish_fn=record_publish)

    def route_to_bridge(topic: str, payload: str, qos: int, retain: bool) -> None:
        bridge.handle_raw_message(topic, payload)

    player_a = VirtualPlayer(session_id, PLAYER_A, publish_fn=route_to_bridge)
    player_b = VirtualPlayer(session_id, PLAYER_B, publish_fn=route_to_bridge)

    player_a.send_telemetry(connected=True, motion_rms=0.05, rotation_rad=0.01)
    player_b.send_telemetry(connected=True, motion_rms=0.04, rotation_rad=0.01)

    cmd_topic = TopicBuilder(session_id).game_command()
    bridge.handle_raw_message(
        cmd_topic,
        json.dumps({"session_id": session_id, "command": "START_GAME"}),
    )

    current_round = engine.round_id
    player_a.send_choice("ROCK", round_id=current_round, motion_rms=2.4, rotation_rad=0.8)
    player_b.send_choice("SCISSORS", round_id=current_round, motion_rms=2.1, rotation_rad=0.7)

    return engine.get_state_snapshot()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Motion RPS Virtual Player Simulator")
    parser.add_argument("--session", default="sim-session-01", help="Session ID")
    args = parser.parse_args()
    snapshot = run_local_simulation_demo(session_id=args.session)
    print(json.dumps(snapshot, indent=2))
