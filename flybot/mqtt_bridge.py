"""MQTT bridge between a StackChan (M5Stack CoreS3) and the BrainController.

Subscribes ``<base>/sensor/{camera,imu,proximity}`` and publishes the command
JSON on ``<base>/command`` at a fixed rate, plus events (presence, escape, game
record) on ``<base>/event`` and optionally to a webhook. With ``--llm`` an LLM
cortex narrates events, answers ``<base>/chat/in`` on ``<base>/chat/out`` and
(``--llm-vision``) asks the robot for a JPEG on ``<base>/snapshot/request``.

Every option can also come from an environment variable ``FLYBOT_<OPTION>`` (e.g.
``FLYBOT_HOST``, ``FLYBOT_PASSWORD``, ``FLYBOT_CALENDAR``, ``FLYBOT_SET``), which is how
the Kubernetes manifests pass Secrets; list options take whitespace-separated values.

    python -m flybot.mqtt_bridge --host 192.168.1.10 --data-dir data/codex
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time

from .controller import (PERSONALITIES, BrainController, SensorState, apply_overrides, load_connectome,
                         personality)
from .cortex import Cortex
from .events import EventSink
from .gains_io import load_gains
from .llm import make_llm
from .meetings import MeetingReminder
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
        self.cortex: Cortex | None = None
        self.dashboard = None  # flybot.dashboard.Dashboard, optional
        self._jpeg: bytes | None = None
        self.presence = None  # flybot.presence.Presence when --presence is on
        self._last_face_turn = 0.0
        self._jpeg_ready = threading.Event()

    def attach_cortex(self, llm, narrate: bool = True, narrate_cooldown_s: float = 20.0,
                      vision: bool = False, vision_llm=None) -> None:
        self.cortex = Cortex(llm, self.controller, self._publish_cortex,
                             self.base, emit_event=lambda ev: self.events.emit(ev),
                             snapshot=self.snapshot if vision else None, narrate=narrate,
                             narrate_cooldown_s=narrate_cooldown_s, vision_llm=vision_llm)

    def _publish_cortex(self, topic: str, body: str) -> None:
        self.client.publish(topic, body, qos=1)
        if self.dashboard:
            self.dashboard.add("chat", json.loads(body))

    def snapshot(self, timeout: float) -> bytes | None:
        """Ask the robot for one JPEG frame (called from the cortex thread)."""
        self._jpeg_ready.clear()
        self.client.publish(f"{self.base}/snapshot/request", "1", qos=0)
        return self._jpeg if self._jpeg_ready.wait(timeout) else None

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:  # e.g. wrong username/password: paho keeps retrying
            log.error("MQTT connection to %s:%d refused: %s", self.host, self.port, reason_code)
            return
        log.info("Connected to %s:%d (%s)", self.host, self.port, reason_code)
        for kind in SENSOR_KINDS:
            client.subscribe(f"{self.base}/sensor/{kind}", qos=0)
        client.subscribe(f"{self.base}/selftest", qos=1)
        client.subscribe(f"{self.base}/sensor/face", qos=0)
        if self.cortex:
            client.subscribe(f"{self.base}/chat/in", qos=1)
            client.subscribe(f"{self.base}/snapshot", qos=0)

    def _on_message(self, client, userdata, msg):
        if msg.topic == f"{self.base}/selftest":
            self._log_selftest(msg.payload)
            return
        if msg.topic == f"{self.base}/sensor/face":
            self._on_face(msg.payload)
            return
        if msg.topic == f"{self.base}/snapshot":
            self._jpeg = bytes(msg.payload)
            self._jpeg_ready.set()
            return
        if msg.topic == f"{self.base}/chat/in" and self.cortex:
            text = msg.payload.decode(errors="replace").strip()
            try:  # plain text or {"text": ...}
                text = json.loads(text).get("text", "")
            except (ValueError, AttributeError):
                pass
            if text:
                self.cortex.on_chat(str(text))
            return
        kind = msg.topic.rsplit("/", 1)[-1]
        try:
            payload = json.loads(msg.payload)
        except (ValueError, UnicodeDecodeError):
            log.warning("Ignoring non-JSON message on %s", msg.topic)
            return
        if kind in SENSOR_KINDS and isinstance(payload, dict):
            self.sensors.update(kind, payload)
            if kind == "camera" and "x" in payload and self.presence:
                self.presence.note_motion()

    def _on_face(self, payload: bytes) -> None:
        """The robot's face detector (several times a second): keep the face centred."""
        try:
            f = json.loads(payload)
        except ValueError:
            return
        if not (f.get("found") and self.presence):
            return
        self.presence.note_face()
        dx, dy = f["x"] - 0.5, f["y"] - 0.5
        if abs(dx) < 0.08 and abs(dy) < 0.1:  # centred enough: hold still
            return
        now = time.monotonic()
        if now - self._last_face_turn < 0.6:  # let the last turn land before the next
            return
        self._last_face_turn = now
        c = self.controller
        # 0.7 of the offset per step from the pose the frame was taken at: converges without overshoot
        c.face(f["pan"] + 0.7 * dx * c.cfg.hfov_deg, f["tilt"] - 0.7 * dy * c.cfg.vfov_deg, hold_s=0.5)

    @staticmethod
    def _log_selftest(payload: bytes) -> None:
        try:
            report = json.loads(payload)
        except ValueError:
            log.warning("Unreadable self-test report")
            return
        items = report.get("items", [])
        failed = [f"{i.get('name')} ({i.get('detail')})" for i in items if not i.get("ok")]
        if failed:
            log.warning("Robot self-test FAILED: %s", "; ".join(failed))
        else:
            log.info("Robot self-test OK: %s", ", ".join(f"{i.get('name')} {i.get('detail')}" for i in items))

    def run(self, heartbeat: str | None = None) -> None:
        # async connect: a broker that is still starting (or restarting) is retried by paho
        self.client.connect_async(self.host, self.port, keepalive=30)
        self.client.loop_start()
        beat = 0.0
        topic = f"{self.base}/command"
        last = time.monotonic()
        try:
            while True:
                now = time.monotonic()
                cmd = self.controller.step(self.sensors, now - last, now=now)
                last = now
                self.client.publish(topic, json.dumps(cmd, separators=(",", ":")), qos=0)
                if self.dashboard:
                    self.dashboard.on_command(cmd)
                for event in self.controller.pop_events():
                    self.events.emit(event, now=now, brain=cmd.get("brain"))
                    if self.dashboard:
                        self.dashboard.add("event", {k: v for k, v in event.items() if k not in ("time", "key")})
                    if self.cortex:
                        self.cortex.on_event(event, self.controller.snapshot.get("brain"))
                if heartbeat and now - beat >= 1.0:  # liveness: the control loop is still turning
                    beat = now
                    with open(heartbeat, "w") as f:
                        f.write(str(time.time()))
                time.sleep(max(0.0, self.period - (time.monotonic() - now)))
        except KeyboardInterrupt:
            pass
        finally:
            self.client.loop_stop()
            self.client.disconnect()
            self.controller.close()


def env_defaults(ap: argparse.ArgumentParser, prefix: str = "FLYBOT_") -> None:
    """Let ``FLYBOT_<DEST>`` environment variables provide option defaults (empty = unset)."""
    for action in ap._actions:
        if not action.option_strings or action.dest == "help":
            continue
        raw = os.environ.get(prefix + action.dest.upper(), "").strip()
        if not raw:
            continue
        if action.nargs == 0:  # store_true flags
            action.default = raw.lower() in ("1", "true", "yes", "on")
        elif isinstance(action, argparse._AppendAction):
            action.default = raw.split()
        else:
            action.default = action.type(raw) if action.type else raw


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--username")
    ap.add_argument("--password")
    ap.add_argument("--base-topic", default="stackchan")
    ap.add_argument("--data-dir", help="folder with FlyWire Codex CSV downloads (synthetic data if omitted)")
    ap.add_argument("--gains", help="gains.json from `python -m flybot.gains_io` (instead of --data-dir)")
    ap.add_argument("--heartbeat", help="file touched every second while the control loop runs (liveness probe)")
    ap.add_argument("--side", default="R", choices=["L", "R"], help="optic lobe hemisphere")
    ap.add_argument("--rate", type=float, default=20.0, help="command rate (Hz)")
    ap.add_argument("--backend", default="nengo", choices=["nengo", "rate"])
    ap.add_argument("--no-telemetry", action="store_true")
    ap.add_argument("--personality", default="curious", choices=sorted(PERSONALITIES))
    ap.add_argument("--game", action="store_true", help="attention game: score shown on the robot")
    ap.add_argument("--webhook", help="POST every event as JSON to this URL")
    ap.add_argument("--event-cooldown", type=float, default=30.0, help="seconds between events of one type")
    cal = ap.add_argument_group("meeting reminders (iCalendar / ICS)")
    cal.add_argument("--calendar", action="append", metavar="URL_OR_FILE",
                     help="ICS feed (Google 'secret address in iCal format', Outlook published calendar, "
                          "webcal://, or a .ics file); repeatable")
    cal.add_argument("--remind-minutes", default="10,1", help="minutes before start, comma-separated")
    cal.add_argument("--calendar-refresh", type=float, default=300.0, help="seconds between feed reloads")
    cal.add_argument("--tz", help="time zone for floating times, e.g. Asia/Bangkok (default: system)")
    ap.add_argument("--state-dir", help="writable dir for the assistant's notes and reminders (a volume in k8s)")
    ap.add_argument("--location", default="16.43,102.83,ขอนแก่น",
                    help="lat,lon,name for the weather tool (Open-Meteo); empty to turn it off")
    ap.add_argument("--presence", action="store_true",
                    help="check camera frames for a person (Claude Haiku); hold announcements while away")
    gh = ap.add_argument_group("GitHub (failed CI, review requests)")
    gh.add_argument("--google-maps-key", help="Google Maps Platform key with Places API (New) for the places tool")
    gh.add_argument("--github-token", help="read-only token (fine-grained: Actions + Pull requests read)")
    gh.add_argument("--github-interval", type=float, default=120.0, help="seconds between polls")
    dash = ap.add_argument_group("web dashboard")
    dash.add_argument("--dashboard-port", type=int, help="serve the dashboard on this port (off if unset)")
    dash.add_argument("--dashboard-user", default="stackchan")
    dash.add_argument("--dashboard-password", help="HTTP basic auth password (no auth if unset)")
    dash.add_argument("--dashboard-sim", action="store_true",
                      help="start with the simulated robot on (turn off when a real robot is connected)")
    dash.add_argument("--tts-url", help="Wayu-TTS server for Thai speech, e.g. http://wayu-tts:7860")
    dash.add_argument("--tts-voice", help="default Wayu voice id (e.g. f_young_clear, m_young_clear)")
    dash.add_argument("--tts-speed", type=float, default=1.0, help="Wayu speaking rate (1.0 is quick; 0.8 calmer)")
    dash.add_argument("--stt-url", help="Thai ASR server (asr-typhoon), e.g. http://asr-typhoon:7871")
    dash.add_argument("--wake-name", default=r"^\s*(สวัสดี|หวัดดี|ฮัลโหล|ฮัลโล|เฮ้|hello|hi\b|hey)|^\s*(หวี|วี|wee)(?!ดี|ซ่|ไอ|ร)|น้อง\s*(หวี่|หวี|วี่|วี|v)|หวี",
                      help="regex the robot's hands-free speech must contain (as asr-typhoon spells the name)")
    dash.add_argument("--follow-up", type=float, default=20.0,
                      help="seconds after a spoken reply during which the next sentence needs no wake name")
    llm = ap.add_argument_group("LLM cortex (narration, chat, vision)")
    llm.add_argument("--llm", default="none", choices=["none", "anthropic", "openai"],
                     help="anthropic = Claude API; openai = any OpenAI-compatible endpoint")
    llm.add_argument("--llm-model", help="default claude-opus-5-5 for anthropic; required for openai")
    llm.add_argument("--llm-base-url", help="e.g. http://localhost:11434/v1 (Ollama), https://api.openai.com/v1")
    llm.add_argument("--llm-api-key-env", help="name of the env var holding the key "
                                               "(default ANTHROPIC_API_KEY / OPENAI_API_KEY)")
    llm.add_argument("--llm-vision", action="store_true",
                     help="send a camera JPEG to the LLM on presence / on request (privacy: images leave the robot)")
    llm.add_argument("--llm-max-tokens", type=int, help="output token cap (default 2048 Claude / 4096 OpenAI-compatible)")
    llm.add_argument("--llm-extra-body", help="JSON merged into OpenAI-compatible requests, e.g. "
                                              "'{\"chat_template_kwargs\": {\"enable_thinking\": false}}' (Qwen3 on vLLM)")
    llm.add_argument("--llm-vision-model", help="separate model for images (same provider/endpoint/key)")
    llm.add_argument("--no-narrate", action="store_true")
    llm.add_argument("--narrate-cooldown", type=float, default=20.0)
    ap.add_argument("--set", action="append", metavar="KEY=VALUE",
                    help="override a ControllerConfig field, e.g. --set k_position=60 --set tilt_limits=0,30")
    env_defaults(ap)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    if args.gains:
        gains, looming, meta = load_gains(args.gains)
        source = f"{args.gains} ({meta.get('source')}, side {meta.get('side')})"
    else:
        connectome = load_connectome(args.data_dir, side=args.side)
        gains = derive_gains(connectome.group_adjacency())
        looming = derive_looming_gains(connectome.group_adjacency(neuropils=None))
        source = connectome.source
    log.info("Connectome: %s | gains on=%s off=%s", source,
             {k: round(v, 2) for k, v in gains.on.items()}, {k: round(v, 2) for k, v in gains.off.items()})
    try:
        base = personality(args.personality)
        cfg = apply_overrides(base, [f"backend={args.backend}", f"telemetry={not args.no_telemetry}",
                                     f"game={args.game}", *(args.set or [])])
    except ValueError as e:
        ap.error(str(e))
    log.info("Personality %s | looming LPLC2=%.2f LC4=%.2f", args.personality, looming.lplc2, looming.lc4)
    try:
        api_key = os.environ.get(args.llm_api_key_env) if args.llm_api_key_env else None
        extra_body = json.loads(args.llm_extra_body) if args.llm_extra_body else None
        llm_options = dict(max_tokens=args.llm_max_tokens, extra_body=extra_body)
        model = make_llm(args.llm, args.llm_model, args.llm_base_url, api_key, vision=args.llm_vision, **llm_options)
        vision_model = (make_llm(args.llm, args.llm_vision_model, args.llm_base_url, api_key, **llm_options)
                        if args.llm_vision and args.llm_vision_model else None)
    except ValueError as e:  # includes invalid --llm-extra-body JSON
        ap.error(str(e))
    controller = BrainController(cfg, gains=gains, looming=looming)
    controller.personality = args.personality
    bridge = StackChanBridge(controller, args.host, args.port,
                             args.base_topic, args.rate, args.username, args.password, args.webhook,
                             args.event_cooldown)
    if model:
        log.info("LLM cortex: %s %s%s", args.llm, getattr(model, "model", ""), " + vision" if args.llm_vision else "")
        bridge.attach_cortex(model, narrate=not args.no_narrate, narrate_cooldown_s=args.narrate_cooldown,
                             vision=args.llm_vision, vision_llm=vision_model)
    if args.calendar:
        from zoneinfo import ZoneInfo
        try:
            leads = tuple(float(v) for v in args.remind_minutes.split(","))
            tz = ZoneInfo(args.tz) if args.tz else None
        except Exception as e:
            ap.error(f"--remind-minutes / --tz: {e}")
        def remind(r: dict) -> None:
            bridge.controller.remind(r)
            title = r.get("title") or "ประชุม"
            where = f" ที่ {r['location']}" if r.get("location") else ""
            when = f"อีก {r.get('minutes', 0)} นาทีมี" if r.get("minutes") else "ถึงเวลา"
            if bridge.dashboard:
                bridge.dashboard.announce(f"{when}ประชุม {title} ตอน {r.get('start', '')}{where} นะคะ")

        reminder = MeetingReminder(args.calendar, remind, leads, args.calendar_refresh, tz)
        reminder.start()
        if bridge.cortex:
            bridge.cortex.calendar = reminder
        log.info("Meeting reminders: %d calendar(s), %s min before", len(args.calendar), args.remind_minutes)
    if args.dashboard_port:
        from .dashboard import Dashboard
        tts = None
        if args.tts_url:
            from .tts import WayuTTS
            tts = WayuTTS(args.tts_url, args.tts_voice, speed=args.tts_speed)
        stt = None
        if args.stt_url:
            from .stt import AsrClient
            stt = AsrClient(args.stt_url)
        bridge.dashboard = Dashboard(bridge, args.dashboard_port, args.dashboard_user, args.dashboard_password,
                                     sim=args.dashboard_sim, tts=tts, stt=stt, wake_name=args.wake_name,
                                     follow_up_s=args.follow_up)
        bridge.dashboard.start()
    if args.presence and bridge.dashboard and args.llm == "anthropic":
        from .llm import AnthropicLLM
        from .presence import Presence
        eyes = AnthropicLLM("claude-haiku-5-5", api_key=api_key, max_tokens=512)  # room for its thinking

        def arrived(left_at: float, now: float) -> None:
            meetings = bridge.cortex.calendar.meetings if bridge.cortex and bridge.cortex.calendar else []
            line = bridge.dashboard.welcome_back(left_at, now, meetings)
            if line:
                bridge.dashboard.announce(line, force=True)

        pose_at_frame = [0.0, 0.0]  # head pose when the frame was asked for: it arrives seconds later

        def snapshot(timeout: float) -> bytes | None:
            pose_at_frame[:] = bridge.controller.pan, bridge.controller.tilt
            return bridge.snapshot(timeout)

        def look_at_person(x: float, y: float) -> None:  # face at (x, y) of the frame -> turn to it
            c = bridge.controller
            pan, tilt = pose_at_frame
            target = pan + (x - 0.5) * c.cfg.hfov_deg, tilt - (y - 0.5) * c.cfg.vfov_deg
            log.info("presence: head at pan %.0f tilt %.0f when the frame was taken -> %.0f, %.0f", pan, tilt, *target)
            c.face(*target)

        def search(scan: bool) -> None:  # nobody in view: face the desk, now and then look around
            c = bridge.controller
            c.gesture("spin") if scan else c.face(c.cfg.home_pan, c.cfg.home_tilt)

        bridge.presence = Presence(snapshot, eyes, arrived, on_person=look_at_person, on_search=search)
        bridge.presence.start()
        if bridge.cortex:
            bridge.cortex.presence = bridge.presence
        log.info("Presence: camera checks with %s", eyes.model)
    if args.location and bridge.cortex:
        from .weather import Weather
        lat, lon, *name = args.location.split(",")
        bridge.cortex.weather = Weather(float(lat), float(lon), name[0] if name else "")
        if args.google_maps_key:
            from .places import Places
            bridge.cortex.places = Places(args.google_maps_key, float(lat), float(lon))
    if args.state_dir and bridge.dashboard:
        bridge.dashboard.archive_dir = os.path.join(args.state_dir, "voice")
    if args.state_dir and bridge.cortex:
        from .assistant import AssistantStore
        store = AssistantStore(os.path.join(args.state_dir, "assistant.json"),
                               lambda line: bridge.dashboard and bridge.dashboard.announce(line), args.tz)
        store.start()
        bridge.cortex.store = store
        log.info("Assistant memory: %s (%d notes, %d reminders)", store.path, len(store.notes), len(store.reminders))
    if args.github_token:
        from .github import GitHubWatcher
        watcher = GitHubWatcher(args.github_token, lambda line: bridge.dashboard and bridge.dashboard.announce(line),
                                args.github_interval)
        watcher.start()
        if bridge.cortex:
            bridge.cortex.github = watcher
        log.info("GitHub: watching CI and review requests every %.0f s", args.github_interval)
    bridge.run(heartbeat=args.heartbeat)


if __name__ == "__main__":
    main()
