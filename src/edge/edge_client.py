"""Edge client script for Android Termux (or Simulation Mode) joining a Motion RPS session.

Portions adapted from PocketGuard IoT (`shared/edge_runner.py`, https://github.com/ruthhhs/iot-projek)
under the MIT License — Copyright (c) 2026 PocketGuard IoT contributors.
"""

import argparse
import json
import sys
import time
from typing import Callable, Optional

from src.edge.sensor_gesture import (
    GestureClassifier,
    GestureConfig,
    SensorSample,
    TermuxSensorReader,
    vibrate_feedback,
)
from src.edge.simulator import VirtualPlayer
from src.engine.game_engine import Choice
from src.engine.mqtt_protocol import MQTTConfig, QOS_CRITICAL, TopicBuilder, parse_json_payload
from src.engine.pairing import PairingToken, parse_pairing_input


class EdgeDeviceClient:
    """
    Coordinates pairing, MQTT state subscription, sensor calibration, gesture detection,
    and publishing choices for a single Android phone (PLAYER_A or PLAYER_B).
    """

    def __init__(
        self,
        token: PairingToken,
        gesture_config: Optional[GestureConfig] = None,
        simulate: bool = False,
        debug_sensors: bool = False,
        publish_fn: Optional[Callable[[str, str, int, bool], None]] = None,
    ) -> None:
        token.validate()
        self.token = token
        self.session_id = token.session_id
        self.player_id = token.player_id
        self.simulate = simulate
        self.debug_sensors = debug_sensors
        self.topics = TopicBuilder(self.session_id)
        self.classifier = GestureClassifier(config=gesture_config)
        self.virtual_player = VirtualPlayer(
            session_id=self.session_id,
            player_id=self.player_id,
            publish_fn=publish_fn,
        )
        self.active_round_id: Optional[int] = None
        self.round_active: bool = False
        self.choice_locked_for_round: Optional[int] = None

    def connect_and_announce(self) -> dict:
        """Announce connection status to the Game Engine via MQTT telemetry."""
        feats = self.classifier.last_features
        return self.virtual_player.send_telemetry(
            connected=True,
            motion_rms=feats.motion_rms,
            rotation_rad=feats.rotation_rad,
        )

    def handle_game_state_message(self, raw_payload: bytes | str) -> None:
        """Update local round tracking when Game Engine publishes a state snapshot."""
        data, err = parse_json_payload(raw_payload)
        if err or data is None:
            return
        if data.get("session_id") != self.session_id:
            return

        state = data.get("state")
        round_id = data.get("round_id")
        if state == "COUNTDOWN" and isinstance(round_id, int):
            if self.active_round_id != round_id:
                self.active_round_id = round_id
                self.choice_locked_for_round = None
                self.classifier.reset_lock()
            self.round_active = True
        else:
            self.round_active = False

    def feed_sensor_sample(self, sample: SensorSample) -> Optional[Choice]:
        """
        Process a sensor sample; if a round is active and hasn't locked a choice yet,
        publish the detected gesture choice to MQTT exactly once for this round.
        """
        if self.debug_sensors:
            print(
                f"[{self.player_id} | RAW] accel={sample.acceleration} m/s² "
                f"gyro={sample.gyroscope} rad/s",
                flush=True,
            )
        detected = self.classifier.process_sample(sample)
        if detected is None:
            return None

        if self.round_active and self.active_round_id is not None:
            if self.choice_locked_for_round != self.active_round_id:
                self.choice_locked_for_round = self.active_round_id
                feats = self.classifier.last_features
                self.virtual_player.send_choice(
                    choice=detected.value,
                    round_id=self.active_round_id,
                    motion_rms=feats.motion_rms,
                    rotation_rad=feats.rotation_rad,
                )
                vibrate_feedback(200)
                return detected
        return None


def interactive_pairing_prompt() -> PairingToken:
    """Fallback interactive prompt in Termux if CLI flags or QR intent were not provided."""
    print("=== Motion RPS IoT — Pairing Perangkat ===")
    print("Masukkan QR URI, Kode Pairing singkat (contoh: rps-a1b2c3:A), atau Session ID:")
    raw = input("> ").strip()
    if ":" in raw or "session=" in raw or raw.startswith("{"):
        return parse_pairing_input(raw)
    slot = input("Pilih slot pemain [A/B]: ").strip()
    return parse_pairing_input(f"{raw}:{slot}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Motion RPS Edge Device Client (Termux / Simulator)")
    parser.add_argument("--pair-code", help="Pairing URI or compact code '<session_id>:<player_id>'")
    parser.add_argument("--session", help="Session ID (if not using --pair-code)")
    parser.add_argument("--player", help="Player slot: A or B (if not using --pair-code)")
    parser.add_argument("--simulate", action="store_true", help="Run without hardware sensors")
    parser.add_argument("--sim-gesture", choices=["LEFT", "RIGHT", "UP"], help="Send one simulated gesture and exit")
    parser.add_argument("--invert-x", action="store_true", help="Invert X-axis orientation for LEFT/RIGHT")
    parser.add_argument(
        "--debug-sensors",
        action="store_true",
        help="Print raw accelerometer/gyroscope values before processing (adapted from PocketGuard).",
    )
    args = parser.parse_args()

    if args.pair_code:
        token = parse_pairing_input(args.pair_code)
    elif args.session and args.player:
        token = parse_pairing_input(f"{args.session}:{args.player}")
    else:
        token = interactive_pairing_prompt()

    cfg = GestureConfig()
    if args.invert_x:
        cfg.left_sign = 1
        cfg.right_sign = -1

    use_sim = args.simulate or not TermuxSensorReader.is_termux_sensor_available()
    client = EdgeDeviceClient(
        token=token,
        gesture_config=cfg,
        simulate=use_sim,
        debug_sensors=args.debug_sensors,
    )

    if args.sim_gesture:
        client.classifier.calibrate([SensorSample(0.0, 0.0, 9.81, timestamp=1.0)])
        client.round_active = True
        client.active_round_id = 1
        for s in TermuxSensorReader.generate_simulated_gesture_stream(args.sim_gesture, base_time=10.0):
            res = client.feed_sensor_sample(s)
            if res:
                feats = client.classifier.last_features
                print(json.dumps({
                    "session_id": client.session_id,
                    "player_id": client.player_id,
                    "detected_choice": res.value,
                    "motion_rms": feats.motion_rms,
                    "rotation_rad": feats.rotation_rad,
                }))
        return 0

    mqtt_cfg = MQTTConfig.from_env()
    mqtt_client = client.virtual_player.bind_mqtt_client(mqtt_cfg)

    def on_connect(c, userdata, flags, reason_code, properties=None):
        c.subscribe(client.topics.game_state(), qos=QOS_CRITICAL)
        client.connect_and_announce()
        print(f"[CONNECTED] Session={client.session_id} Slot={client.player_id} Mode={'SIM' if use_sim else 'TERMUX_SENSOR'}")

    def on_message(c, userdata, msg):
        if msg.topic == client.topics.game_state():
            client.handle_game_state_message(msg.payload)

    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    mqtt_client.connect(mqtt_cfg.host, mqtt_cfg.port, keepalive=30)
    mqtt_client.loop_start()

    try:
        if use_sim:
            print("Mode Simulasi Interaktif: ketik LEFT (ROCK), RIGHT (SCISSORS), UP (PAPER), atau q untuk keluar.")
            client.classifier.calibrate([SensorSample(0.0, 0.0, 9.81, timestamp=1.0)])
            while True:
                cmd = input("Gesture [LEFT/RIGHT/UP/q]> ").strip().upper()
                if cmd in ("Q", "QUIT", "EXIT"):
                    break
                if cmd in ("LEFT", "RIGHT", "UP"):
                    for s in TermuxSensorReader.generate_simulated_gesture_stream(cmd, base_time=time.time()):
                        choice = client.feed_sensor_sample(s)
                        if choice:
                            print(f"-> Terkirim: {choice.value} (Ronde {client.active_round_id})")
        else:
            print("Kalibrasi sensor... Letakkan/pegang HP dengan tenang selama 1 detik.")
            cal_samples = list(TermuxSensorReader.stream_termux_sensors(limit=10))
            client.classifier.calibrate(cal_samples)
            print(f"Kalibrasi selesai (baseline={client.classifier.baseline}). Siap mendeteksi gerakan!")
            for sample in TermuxSensorReader.stream_termux_sensors():
                choice = client.feed_sensor_sample(sample)
                if choice:
                    print(f"-> Gerakan terdeteksi & terkirim: {choice.value} (Ronde {client.active_round_id})")
    except KeyboardInterrupt:
        pass
    finally:
        client.virtual_player.send_telemetry(connected=False)
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
