"""Edge client script for Android Termux (or Simulation Mode) joining a Motion RPS session.

Supports:
1. Direct pairing via `--pair-code <session_id>:<player_id>` or QR URI.
2. Zero-typing Auto-Pairing mode (`--auto-pair`) that listens via MQTT (`motion-rps/pairing/...`)
   and/or HTTP (`/api/pair/assignment`) so scanning the QR in the phone browser automatically
   routes Termux sensor telemetry to the active session and player slot.
3. Continuous live telemetry streaming (~2.5 Hz + immediate on gesture) and periodic heartbeat.
4. Termux wake-lock management so sensor streaming survives while switching to the browser.

Portions adapted from PocketGuard IoT (`shared/edge_runner.py`, https://github.com/ruthhhs/iot-projek)
under the MIT License — Copyright (c) 2026 PocketGuard IoT contributors.
"""

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, Optional
from urllib.request import urlopen

from src.edge.sensor_gesture import (
    GestureClassifier,
    GestureConfig,
    SensorSample,
    TermuxSensorReader,
    vibrate_feedback,
)
from src.edge.simulator import VirtualPlayer
from src.engine.game_engine import Choice, PLAYER_A, PLAYER_B
from src.engine.mqtt_protocol import MQTTConfig, QOS_CRITICAL, TopicBuilder, parse_json_payload
from src.engine.pairing import PairingToken, parse_pairing_input

DEFAULT_EDGE_CONFIG_PATH = os.path.expanduser("~/.motion_rps_edge.json")


def load_saved_edge_config(path: str = DEFAULT_EDGE_CONFIG_PATH) -> Dict[str, Any]:
    """Load saved non-secret local device preferences (player slot, device_id, server_url)."""
    for candidate in (path, ".edge_config.json"):
        if os.path.isfile(candidate):
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
    return {}


def save_edge_config(config_data: Dict[str, Any], path: str = DEFAULT_EDGE_CONFIG_PATH) -> str:
    """Save non-secret device preferences for one-command launch on subsequent runs."""
    safe_keys = {"player_id", "device_id", "server_url", "threshold", "invert_x"}
    filtered = {k: v for k, v in config_data.items() if k in safe_keys and v is not None}
    target = path
    try:
        with open(target, "w", encoding="utf-8") as f:
            json.dump(filtered, f, indent=2)
        return target
    except OSError:
        target = ".edge_config.json"
        with open(target, "w", encoding="utf-8") as f:
            json.dump(filtered, f, indent=2)
        return target


def set_termux_wakelock(acquire: bool = True) -> bool:
    """Acquire or release Android wake-lock via Termux (`termux-wake-lock` / `termux-wake-unlock`)."""
    binary = "termux-wake-lock" if acquire else "termux-wake-unlock"
    if not shutil.which(binary):
        return False
    try:
        subprocess.run([binary], capture_output=True, timeout=5, check=False)
        return True
    except Exception:
        return False


class EdgeDeviceClient:
    """
    Coordinates pairing (static or dynamic QR auto-pairing), MQTT state subscription,
    sensor calibration, continuous live telemetry, periodic heartbeat, gesture detection,
    and publishing choices for a single Android phone (PLAYER_A or PLAYER_B).
    """

    def __init__(
        self,
        token: PairingToken,
        gesture_config: Optional[GestureConfig] = None,
        simulate: bool = False,
        debug_sensors: bool = False,
        telemetry_interval_sec: float = 0.4,
        heartbeat_interval_sec: float = 2.0,
        device_id: Optional[str] = None,
        standby_waiting_pair: bool = False,
        publish_fn: Optional[Callable[[str, str, int, bool], None]] = None,
    ) -> None:
        token.validate()
        self.lock = threading.Lock()
        self.token = token
        self.session_id = token.session_id
        self.player_id = token.player_id
        self.device_id = device_id or f"dev-{secrets.token_hex(4)}"
        self.simulate = simulate
        self.debug_sensors = debug_sensors
        self.telemetry_interval_sec = float(telemetry_interval_sec)
        self.heartbeat_interval_sec = float(heartbeat_interval_sec)
        self.standby_waiting_pair = bool(standby_waiting_pair)
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
        self._last_sample: Optional[SensorSample] = None

    def connect_and_announce(self) -> dict:
        """Announce initial connection status to the Game Engine via MQTT telemetry."""
        self.standby_waiting_pair = False
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
        self._last_telemetry_sent_at = time.time()
        if self.debug_sensors:
            print(f"[MQTT_PUBLISHED] Telemetry initial connect seq={payload['sequence']}", flush=True)
        return payload

    def reconfigure_session(self, new_session_id: str, new_player_id: Optional[str] = None) -> bool:
        """
        Dynamically switch the running edge client to a new session_id (and optional player_id)
        received from a Web QR Pairing event without restarting Termux!
        """
        with self.lock:
            clean_sid = (new_session_id or "").strip()
            clean_pid = (new_player_id or self.player_id).strip()
            new_tok = PairingToken(session_id=clean_sid, player_id=clean_pid)
            new_tok.validate()

            if (
                clean_sid == self.session_id
                and clean_pid == self.player_id
                and not self.standby_waiting_pair
            ):
                return False

            old_topics = self.topics
            if not self.standby_waiting_pair and self.virtual_player._client is not None:
                try:
                    self.virtual_player.send_telemetry(
                        connected=False,
                        diag_status="OFFLINE",
                        diag_reason="SWITCHED_SESSION",
                    )
                except Exception:
                    pass

            self.token = new_tok
            self.session_id = clean_sid
            self.player_id = clean_pid
            self.topics = TopicBuilder(clean_sid)
            self.virtual_player.session_id = clean_sid
            self.virtual_player.player_id = clean_pid
            self.virtual_player.topics = self.topics
            self.active_round_id = None
            self.round_active = False
            self.choice_locked_for_round = None
            self.standby_waiting_pair = False

            mqtt_cli = self.virtual_player._client
            if mqtt_cli is not None:
                try:
                    mqtt_cli.unsubscribe(old_topics.game_state())
                    mqtt_cli.subscribe(self.topics.game_state(), qos=QOS_CRITICAL)
                except Exception:
                    pass

            self.connect_and_announce()
            print(
                f"[PAIRING_ACTIVE] Sensor beralih ke Session={self.session_id} Slot={self.player_id}",
                flush=True,
            )
            return True

    def handle_pairing_assignment_message(self, raw_payload: bytes | str) -> bool:
        """
        Handle incoming MQTT message on `motion-rps/pairing/slot/{player_id}` or
        `motion-rps/pairing/device/{device_id}`.
        """
        data, err = parse_json_payload(raw_payload)
        if err or data is None:
            return False
        action = str(data.get("action") or "").upper()
        sid = str(data.get("session_id") or "").strip()
        pid = str(data.get("player_id") or self.player_id).strip()

        if action == "LEAVE_SESSION":
            if sid == self.session_id and pid == self.player_id and not self.standby_waiting_pair:
                self.virtual_player.send_telemetry(
                    connected=False,
                    diag_status="OFFLINE",
                    diag_reason="LEFT_VIA_WEB_PAIRING",
                )
                self.standby_waiting_pair = True
                self.round_active = False
                print(f"[STANDBY] Keluar dari sesi {sid}. Menunggu scan QR berikutnya...", flush=True)
                return True
            return False

        if action in ("ASSIGN_SESSION", "SESSION_CHANGED") and sid:
            return self.reconfigure_session(sid, pid)

        return False

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

    def send_heartbeat_if_due(self, now: Optional[float] = None) -> Optional[dict]:
        """Publish a periodic telemetry heartbeat if no telemetry was sent within `heartbeat_interval_sec`."""
        if self.standby_waiting_pair:
            return None
        current_time = time.time() if now is None else float(now)
        if (current_time - self._last_telemetry_sent_at) < self.heartbeat_interval_sec:
            return None

        self._last_telemetry_sent_at = current_time
        feats = self.classifier.last_features
        diag = self.classifier.last_diagnostic
        accel = self._last_sample.acceleration if self._last_sample else (0.0, 0.0, 9.81)
        gyro = self._last_sample.gyroscope if self._last_sample else (0.0, 0.0, 0.0)
        return self.virtual_player.send_telemetry(
            connected=True,
            motion_rms=feats.motion_rms,
            rotation_rad=feats.rotation_rad,
            accel=accel,
            gyro=gyro,
            diag_status=diag.status,
            diag_reason=diag.reason if diag.reason != "BELOW_THRESHOLD" else "HEARTBEAT_OK",
            last_gesture=self.last_detected_gesture,
            triggered_axis=self.last_triggered_axis,
            mode="SIMULATION" if self.simulate else "TERMUX_SENSOR",
        )

    def feed_sensor_sample(self, sample: SensorSample, publish_live_telemetry: bool = True) -> Optional[Choice]:
        """
        Process a sensor sample:
        1. Classify gesture and update `self.classifier.last_diagnostic`.
        2. Publish live telemetry (throttled to `telemetry_interval_sec` or immediately when a gesture is detected)
           so the Dashboard always displays live accelerometer/gyroscope & gesture diagnostics even outside a round!
        3. If a round is active (`COUNTDOWN`) and hasn't locked a choice yet, publish the choice to Game Engine.
        """
        self._last_sample = sample
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

        if self.standby_waiting_pair:
            return detected

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


def poll_http_assignment_once(
    server_url: str,
    player_id: str,
    device_id: str,
    timeout_sec: float = 2.0,
) -> Optional[Dict[str, Any]]:
    """Query the dashboard server `/api/pair/assignment` for dynamic session assignment."""
    clean_url = server_url.rstrip("/")
    url = f"{clean_url}/api/pair/assignment?player_id={player_id}&device_id={device_id}"
    try:
        with urlopen(url, timeout=timeout_sec) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
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
    parser.add_argument("--pair-code", help="Pairing URI, URL, or compact code '<session_id>:<player_id>'")
    parser.add_argument("--session", help="Session ID (if not using --pair-code)")
    parser.add_argument("--player", help="Player slot: A or B (if not using --pair-code)")
    parser.add_argument(
        "--auto-pair",
        action="store_true",
        help="Listen for QR pairing assignments automatically without typing session ID each game.",
    )
    parser.add_argument("--device-id", help="Persistent device identifier for QR pairing")
    parser.add_argument("--server-url", help="Optional Dashboard HTTP URL (e.g. http://192.168.1.10:8080) for LAN polling")
    parser.add_argument("--save-config", action="store_true", help="Save player/device/server settings to ~/.motion_rps_edge.json")
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

    saved_cfg = load_saved_edge_config()
    device_id = args.device_id or saved_cfg.get("device_id") or f"dev-{secrets.token_hex(4)}"
    server_url = args.server_url or saved_cfg.get("server_url") or os.getenv("DASHBOARD_URL")
    default_slot = args.player or saved_cfg.get("player_id") or "A"

    if args.pair_code:
        token = parse_pairing_input(args.pair_code, default_player=default_slot)
        standby_mode = False
    elif args.session and (args.player or default_slot):
        token = parse_pairing_input(f"{args.session}:{args.player or default_slot}")
        standby_mode = False
    elif args.auto_pair:
        initial_sid = os.getenv("PROJECT_ID") or os.getenv("DEFAULT_SESSION_ID") or "rps_standby"
        token = parse_pairing_input(f"{initial_sid}:{default_slot}")
        standby_mode = True
    else:
        token = interactive_pairing_prompt()
        standby_mode = False

    if args.save_config or args.auto_pair:
        save_edge_config({
            "player_id": token.player_id,
            "device_id": device_id,
            "server_url": server_url,
            "threshold": args.threshold,
            "invert_x": args.invert_x,
        })

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
        device_id=device_id,
        standby_waiting_pair=standby_mode,
    )

    if args.sim_gesture:
        client.standby_waiting_pair = False
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

    wakelock_held = set_termux_wakelock(acquire=True)
    if wakelock_held:
        print("[TERMUX] Wake-lock aktif (sensor tetap berjalan saat pindah ke browser HP).", flush=True)

    mqtt_cfg = MQTTConfig.from_env()
    mqtt_client = client.virtual_player.bind_mqtt_client(mqtt_cfg)

    def on_connect(c, userdata, flags, reason_code, properties=None):
        c.subscribe(client.topics.game_state(), qos=QOS_CRITICAL)
        c.subscribe(TopicBuilder.pairing_slot(client.player_id), qos=QOS_CRITICAL)
        c.subscribe(TopicBuilder.pairing_device(client.device_id), qos=QOS_CRITICAL)
        if not client.standby_waiting_pair:
            client.connect_and_announce()
            print(
                f"[CONNECTED] Session={client.session_id} Slot={client.player_id} "
                f"Device={client.device_id} Mode={'SIM' if use_sim else 'TERMUX_SENSOR'}",
                flush=True,
            )
        else:
            print(
                f"[AUTO-PAIR STANDBY] Slot={client.player_id} Device={client.device_id} siap! "
                "Silakan Scan QR di browser HP dan klik Join Session.",
                flush=True,
            )

    def on_message(c, userdata, msg):
        if msg.topic == client.topics.game_state():
            client.handle_game_state_message(msg.payload)
        elif msg.topic in (
            TopicBuilder.pairing_slot(client.player_id),
            TopicBuilder.pairing_device(client.device_id),
        ):
            client.handle_pairing_assignment_message(msg.payload)

    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    mqtt_client.connect(mqtt_cfg.host, mqtt_cfg.port, keepalive=30)
    mqtt_client.loop_start()

    stop_event = threading.Event()

    def background_maintenance_loop() -> None:
        while not stop_event.is_set():
            if server_url:
                info = poll_http_assignment_once(server_url, client.player_id, client.device_id)
                if info and info.get("ok"):
                    sid = info.get("session_id")
                    pid = info.get("player_id") or client.player_id
                    if sid and (info.get("assigned") or not args.auto_pair):
                        client.reconfigure_session(sid, pid)
                elif info and not info.get("session_active", True) and not client.standby_waiting_pair:
                    client.handle_pairing_assignment_message(json.dumps({
                        "action": "LEAVE_SESSION",
                        "session_id": client.session_id,
                        "player_id": client.player_id,
                    }))
            client.send_heartbeat_if_due()
            stop_event.wait(1.5)

    maint_thread = threading.Thread(target=background_maintenance_loop, daemon=True)
    maint_thread.start()

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
        stop_event.set()
        try:
            client.virtual_player.send_telemetry(connected=False, diag_status="OFFLINE", diag_reason="CLIENT_EXIT")
        except Exception:
            pass
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        if wakelock_held:
            set_termux_wakelock(acquire=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
