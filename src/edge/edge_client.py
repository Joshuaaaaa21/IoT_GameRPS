"""Edge client script for Android Termux (or Simulation Mode) joining a Motion RPS session.

Includes continuous live telemetry streaming (throttled ~2.5 Hz + immediate on gesture)
and stage-by-stage diagnostic logging:
SENSOR_READ -> PAYLOAD_CREATED -> MQTT_PUBLISHED

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
    Coordinates pairing, MQTT state subscription, sensor calibration, continuous live telemetry,
    gesture detection, and publishing choices for a single Android phone (PLAYER_A or PLAYER_B).
    """

    def __init__(
        self,
        token: PairingToken,
        gesture_config: Optional[GestureConfig] = None,
        simulate: bool = False,
        debug_sensors: bool = False,
        telemetry_interval_sec: float = 0.4,
        publish_fn: Optional[Callable[[str, str, int, bool], None]] = None,
    ) -> None:
        token.validate()
        self.token = token
        self.session_id = token.session_id
        self.player_id = token.player_id
        self.simulate = simulate
        self.debug_sensors = debug_sensors
        self.telemetry_interval_sec = float(telemetry_interval_sec)
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
        self.last_detected_gesture: Optional[str] = None
        self.last_triggered_axis: Optional[str] = None
        self._last_telemetry_sent_at: float = 0.0

    def connect_and_announce(self) -> dict:
        """Announce initial connection status to the Game Engine via MQTT telemetry."""
        feats = self.classifier.last_features
        payload = self.virtual_player.send_telemetry(
            connected=True,
            motion_rms=feats.motion_rms,
            rotation_rad=feats.rotation_rad,
            diag_status="WAITING",
            diag_reason="CONNECTED_IDLE",
            last_gesture=self.last_detected_gesture,
            mode="SIMULATION" if self.simulate else "TERMUX_SENSOR",
        )
        if self.debug_sensors:
            print(f"[MQTT_PUBLISHED] Telemetry initial connect seq={payload['sequence']}", flush=True)
        return payload

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
                if self.debug_sensors:
                    print(f"[ROUND_ACTIVE] Countdown started for round_id={round_id}", flush=True)
            self.round_active = True
        else:
            self.round_active = False

    def feed_sensor_sample(self, sample: SensorSample, publish_live_telemetry: bool = True) -> Optional[Choice]:
        """
        Process a sensor sample:
        1. Classify gesture and update `self.classifier.last_diagnostic`.
        2. Publish live telemetry (throttled to `telemetry_interval_sec` or immediately when a gesture is detected)
           so the Dashboard always displays live accelerometer/gyroscope & gesture diagnostics even outside a round!
        3. If a round is active (`COUNTDOWN`) and hasn't locked a choice yet, publish the choice to Game Engine.
        """
        detected = self.classifier.process_sample(sample)
        diag = self.classifier.last_diagnostic
        feats = self.classifier.last_features

        if detected is not None:
            self.last_detected_gesture = detected.value
            self.last_triggered_axis = diag.triggered_axis

        if self.debug_sensors:
            print(
                f"[SENSOR_READ] [{self.player_id}] accel={tuple(round(v, 2) for v in sample.acceleration)} m/s² "
                f"gyro={tuple(round(v, 2) for v in sample.gyroscope)} rad/s "
                f"-> status={diag.status} reason={diag.reason}",
                flush=True,
            )

        now = sample.timestamp if sample.timestamp > 0 else time.time()
        should_send_telemetry = publish_live_telemetry and (
            detected is not None or (now - self._last_telemetry_sent_at) >= self.telemetry_interval_sec
        )

        if should_send_telemetry:
            self._last_telemetry_sent_at = now
            t_payload = self.virtual_player.send_telemetry(
                connected=True,
                motion_rms=feats.motion_rms,
                rotation_rad=feats.rotation_rad,
                accel=sample.acceleration,
                gyro=sample.gyroscope,
                diag_status=diag.status,
                diag_reason=diag.reason,
                last_gesture=self.last_detected_gesture,
                triggered_axis=self.last_triggered_axis or diag.triggered_axis,
                mode="SIMULATION" if self.simulate else "TERMUX_SENSOR",
            )
            if self.debug_sensors:
                print(
                    f"[PAYLOAD_CREATED -> MQTT_PUBLISHED] topic=telemetry seq={t_payload['sequence']} "
                    f"status={diag.status} last_gesture={self.last_detected_gesture}",
                    flush=True,
                )

        if detected is None:
            return None

        # Always give haptic feedback when a physical gesture is recognized
        vibrate_feedback(150)

        if self.round_active and self.active_round_id is not None:
            if self.choice_locked_for_round != self.active_round_id:
                self.choice_locked_for_round = self.active_round_id
                c_payload = self.virtual_player.send_choice(
                    choice=detected.value,
                    round_id=self.active_round_id,
                    motion_rms=feats.motion_rms,
                    rotation_rad=feats.rotation_rad,
                    accel=sample.acceleration,
                    gyro=sample.gyroscope,
                    triggered_axis=diag.triggered_axis,
                    diag_reason=diag.reason,
                )
                if self.debug_sensors:
                    print(
                        f"[PAYLOAD_CREATED -> MQTT_PUBLISHED] topic=choice round={self.active_round_id} "
                        f"choice={c_payload['choice']} axis={diag.triggered_axis}",
                        flush=True,
                    )
                return detected
        elif self.debug_sensors:
            print(
                f"[GESTURE_OUTSIDE_ROUND] Detected {detected.value} (sent via live telemetry; "
                "press START GAME on dashboard to lock as round choice).",
                flush=True,
            )

        return detected


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
    parser.add_argument(
        "--sim-gesture",
        choices=["STILL", "WEAK", "LEFT", "RIGHT", "UP"],
        help="Send one simulated gesture stream and exit",
    )
    parser.add_argument("--invert-x", action="store_true", help="Invert X-axis orientation for LEFT/RIGHT")
    parser.add_argument("--threshold", type=float, default=5.0, help="Acceleration spike threshold in m/s^2 (default: 5.0)")
    parser.add_argument(
        "--debug-sensors",
        action="store_true",
        help="Print stage-by-stage raw sensor & diagnostic logs.",
    )
    args = parser.parse_args()

    if args.pair_code:
        token = parse_pairing_input(args.pair_code)
    elif args.session and args.player:
        token = parse_pairing_input(f"{args.session}:{args.player}")
    else:
        token = interactive_pairing_prompt()

    cfg = GestureConfig(accel_threshold=args.threshold)
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
        detected_final = None
        for s in TermuxSensorReader.generate_simulated_gesture_stream(args.sim_gesture, base_time=10.0):
            res = client.feed_sensor_sample(s)
            if res:
                detected_final = res
        feats = client.classifier.last_features
        diag = client.classifier.last_diagnostic
        print(json.dumps({
            "session_id": client.session_id,
            "player_id": client.player_id,
            "detected_choice": detected_final.value if detected_final else None,
            "diag_status": diag.status,
            "diag_reason": diag.reason,
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
            print("Mode Simulasi Interaktif: ketik LEFT (ROCK), RIGHT (SCISSORS), UP (PAPER), WEAK, STILL, atau q untuk keluar.")
            client.classifier.calibrate([SensorSample(0.0, 0.0, 9.81, timestamp=1.0)])
            while True:
                cmd = input("Gesture [LEFT/RIGHT/UP/WEAK/STILL/q]> ").strip().upper()
                if cmd in ("Q", "QUIT", "EXIT"):
                    break
                if cmd in ("LEFT", "RIGHT", "UP", "WEAK", "STILL"):
                    for s in TermuxSensorReader.generate_simulated_gesture_stream(cmd, base_time=time.time()):
                        choice = client.feed_sensor_sample(s)
                        if choice:
                            print(f"-> Terdeteksi: {choice.value} (Ronde Aktif={client.round_active})")
        else:
            print("Kalibrasi sensor... Letakkan/pegang HP dengan tenang selama 1 detik.")
            cal_samples = list(TermuxSensorReader.stream_termux_sensors(limit=6))
            client.classifier.calibrate(cal_samples)
            print(f"Kalibrasi selesai (baseline={tuple(round(v, 2) for v in client.classifier.baseline)}). Siap mendeteksi gerakan!")
            for sample in TermuxSensorReader.stream_termux_sensors():
                choice = client.feed_sensor_sample(sample)
                if choice:
                    print(f"-> Gerakan terdeteksi & terkirim: {choice.value} (Ronde Aktif={client.round_active})")
    except KeyboardInterrupt:
        pass
    finally:
        client.virtual_player.send_telemetry(connected=False)
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
