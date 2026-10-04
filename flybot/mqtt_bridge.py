"""MQTT bridge between a StackChan (M5Stack CoreS3) and the BrainController.

Subscribes ``<base>/sensor/{camera,imu,proximity}`` and publishes the command
JSON on ``<base>/command`` at a fixed rate, plus events (presence, escape, game
record) on ``<base>/event`` and optionally to a webhook.

    python -m flybot.mqtt_bridge --host 192.168.1.10 --data-dir data/codex
"""
from __future__ import annotations

import argparse
import json
import logging
import time

from .controller import (PERSONALITIES, BrainController, SensorState, apply_overrides, load_connectome,
                         personality)
from .events import EventSink
from .looming import derive_looming_gains
from .optic_lobe import derive_gains

log = logging.getLogger("flybot.mqtt")
SENSOR_KINDS = ("camera", "imu", "proximity")


class StackChanBridge:
    def __init__(self, controller: BrainController, host: str, port: int = 1883,
                 base_topic: str = "stackchan", rate_hz: float = 20.0,
                 username: str | None = None, password: str | None = None,
                 webhook_url: str | None = None, event_cooldown_s: float = 30.0):
        import paho.mqtt.client as mqtt

        self.controller = controller
        self.sensors = SensorState()
        self.base = base_topic.rstrip("/")
        self.period = 1.0 / rate_hz
        self.host, self.port = host, port
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="flybot-brain")
        if username:
            self.client.username_pw_set(username, password)
        self.events = EventSink(lambda topic, body: self.client.publish(topic, body, qos=1), self.base,
                                webhook_url, event_cooldown_s)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        log.info("Connected to %s:%d (%s)", self.host, self.port, reason_code)
        for kind in SENSOR_KINDS:
            client.subscribe(f"{self.base}/sensor/{kind}", qos=0)

    def _on_message(self, client, userdata, msg):
        kind = msg.topic.rsplit("/", 1)[-1]
        try:
            payload = json.loads(msg.payload)
        except (ValueError, UnicodeDecodeError):
            log.warning("Ignoring non-JSON message on %s", msg.topic)
            return
        if kind in SENSOR_KINDS and isinstance(payload, dict):
            self.sensors.update(kind, payload)

    def run(self) -> None:
        self.client.connect(self.host, self.port, keepalive=30)
        self.client.loop_start()
        topic = f"{self.base}/command"
        last = time.monotonic()
        try:
            while True:
                now = time.monotonic()
                cmd = self.controller.step(self.sensors, now - last, now=now)
                last = now
                self.client.publish(topic, json.dumps(cmd, separators=(",", ":")), qos=0)
                for event in self.controller.pop_events():
                    self.events.emit(event, now=now, brain=cmd.get("brain"))
                time.sleep(max(0.0, self.period - (time.monotonic() - now)))
        except KeyboardInterrupt:
            pass
        finally:
            self.client.loop_stop()
            self.client.disconnect()
            self.controller.close()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--username")
    ap.add_argument("--password")
    ap.add_argument("--base-topic", default="stackchan")
    ap.add_argument("--data-dir", help="folder with FlyWire Codex CSV downloads (synthetic data if omitted)")
    ap.add_argument("--side", default="R", choices=["L", "R"], help="optic lobe hemisphere")
    ap.add_argument("--rate", type=float, default=20.0, help="command rate (Hz)")
    ap.add_argument("--backend", default="nengo", choices=["nengo", "rate"])
    ap.add_argument("--no-telemetry", action="store_true")
    ap.add_argument("--personality", default="curious", choices=sorted(PERSONALITIES))
    ap.add_argument("--game", action="store_true", help="attention game: score shown on the robot")
    ap.add_argument("--webhook", help="POST every event as JSON to this URL")
    ap.add_argument("--event-cooldown", type=float, default=30.0, help="seconds between events of one type")
    ap.add_argument("--set", action="append", metavar="KEY=VALUE",
                    help="override a ControllerConfig field, e.g. --set k_position=60 --set tilt_limits=0,30")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    connectome = load_connectome(args.data_dir, side=args.side)
    gains = derive_gains(connectome.group_adjacency())
    looming = derive_looming_gains(connectome.group_adjacency(neuropils=None))
    log.info("Connectome: %s | gains on=%s off=%s", connectome.source,
             {k: round(v, 2) for k, v in gains.on.items()}, {k: round(v, 2) for k, v in gains.off.items()})
    try:
        base = personality(args.personality)
        cfg = apply_overrides(base, [f"backend={args.backend}", f"telemetry={not args.no_telemetry}",
                                     f"game={args.game}", *(args.set or [])])
    except ValueError as e:
        ap.error(str(e))
    log.info("Personality %s | looming LPLC2=%.2f LC4=%.2f", args.personality, looming.lplc2, looming.lc4)
    StackChanBridge(BrainController(cfg, gains=gains, looming=looming), args.host, args.port, args.base_topic,
                    args.rate, args.username, args.password, args.webhook, args.event_cooldown).run()


if __name__ == "__main__":
    main()
