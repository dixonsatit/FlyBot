"""BrainController: StackChan sensors -> FlyWire circuits -> servo/face/audio JSON."""
from __future__ import annotations

import bisect
import math
import queue
import time
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

import numpy as np

from .central_complex import CentralComplex
from .connectome import Connectome, load_codex, synthetic_codex
from .looming import GiantFiber, LoomingGains, derive_looming_gains
from .mushroom_body import MushroomBody, Percept
from .optic_lobe import CircuitGains, build_optic_lobe, derive_gains
from .thai_text import shape

# M5.Speaker.tone(freq, ms) sequences for each expression change
AUDIO = {
    "alert": {"tones": [[1800, 80], [0, 40], [1800, 80]], "volume": 180},
    "curious": {"tones": [[600, 60], [900, 60], [1200, 90]], "volume": 120},
    "happy": {"tones": [[880, 90], [1320, 140]], "volume": 140},
    "sleepy": {"tones": [[500, 150], [350, 200], [250, 300]], "volume": 60},
    "startle": {"tones": [[2400, 40], [1200, 40], [2400, 60]], "volume": 200},
    "reminder": {"tones": [[988, 120], [0, 60], [1319, 120], [0, 60], [1568, 220]], "volume": 170},
}


@dataclass
class ControllerConfig:
    frame_width: int = 320  # CoreS3 GC0308 QVGA
    frame_height: int = 240
    hfov_deg: float = 62.0
    vfov_deg: float = 49.0
    v_max: float = 2.0  # image velocity (half-frames/s) mapped to full-scale input
    k_position: float = 90.0  # deg/s per unit LC10 output
    k_motion: float = 30.0  # deg/s per unit HS/VS output
    k_heading: float = 40.0  # deg/s per unit PFL3 steering (target lost)
    vor_gain: float = 1.0
    max_rate_deg_s: float = 200.0
    pan_limits: tuple[float, float] = (-90.0, 90.0)
    tilt_limits: tuple[float, float] = (-45.0, 45.0)
    deadband: float = 0.04
    target_timeout_s: float = 0.5
    sensor_latency_s: float = 0.1  # camera capture -> bridge (WiFi + broker); 0 for ideal sims
    imu_yaw_sign: float = -1.0  # gyro z is CCW-positive; internal yaw is CW-positive
    proximity_max_mm: float = 400.0
    proximity_raw_max: float = 2047.0  # LTR-553 PS counts
    backend: str = "nengo"
    n_neurons: int = 60
    telemetry: bool = True
    # mushroom body / emotion
    alert_threshold: float = 0.5
    habituation_recovery_s: float = 30.0
    sleep_time_s: float = 20.0
    als_dark: float = 20.0  # LTR-553 ALS counts below which the room counts as dark
    # looming escape (LPLC2 / LC4 -> Giant Fiber)
    gf_threshold: float = 1.0
    gf_refractory_s: float = 2.0
    escape_s: float = 0.5
    # phototaxis from the camera's left/right/top/bottom brightness: +1 seek light, -1 avoid
    phototaxis: float = 0.0
    k_light: float = 30.0  # deg/s at full left/right contrast
    # attention game: time the robot keeps a target centred
    game: bool = False
    game_lock: float = 0.3  # |x| (half-frames) counted as "looking at it"
    game_grace_s: float = 0.8  # gaps shorter than this keep the streak
    # reminders turn the head to where the user usually sits
    home_pan: float = 0.0
    home_tilt: float = 10.0
    # balloon text language: "th" needs the firmware's Thai font, "en" works on any firmware
    screen_lang: str = "th"


# Presets for --personality, applied before --set overrides
PERSONALITIES: dict[str, dict] = {
    "curious": {},
    "skittish": {"alert_threshold": 0.35, "gf_threshold": 0.6, "habituation_recovery_s": 10.0,
                 "phototaxis": -1.0},
    "bold": {"alert_threshold": 0.7, "gf_threshold": 1.6, "habituation_recovery_s": 60.0,
             "phototaxis": 1.0},
    "sleepy": {"sleep_time_s": 6.0, "alert_threshold": 0.6, "phototaxis": -0.5},
}


@dataclass
class SensorState:
    """Latest sensor messages, updated asynchronously by the MQTT bridge."""

    camera: dict | None = None
    imu: dict | None = None
    proximity: dict | None = None
    stamps: dict = field(default_factory=dict)

    def update(self, kind: str, payload: dict, now: float | None = None) -> None:
        setattr(self, kind, payload)
        self.stamps[kind] = time.monotonic() if now is None else now


def apply_overrides(cfg: ControllerConfig, items: list[str] | None) -> ControllerConfig:
    """Return a copy of ``cfg`` with ``["key=value", ...]`` applied (tuples as ``a,b``)."""
    names = {f.name for f in fields(cfg)}
    changes = {}
    for item in items or []:
        key, sep, raw = item.partition("=")
        key = key.strip()
        if not sep or key not in names:
            raise ValueError(f"expected key=value with key in {sorted(names)}, got {item!r}")
        current = getattr(cfg, key)
        if isinstance(current, bool):
            changes[key] = raw.strip().lower() in ("1", "true", "yes", "on")
        elif isinstance(current, tuple):
            changes[key] = tuple(float(v) for v in raw.split(","))
        else:
            changes[key] = type(current)(raw)
    return replace(cfg, **changes)


def personality(name: str) -> ControllerConfig:
    if name not in PERSONALITIES:
        raise ValueError(f"unknown personality {name!r}; choose from {sorted(PERSONALITIES)}")
    return replace(ControllerConfig(), **PERSONALITIES[name])


def load_connectome(data_dir: str | Path | None, side: str = "R") -> Connectome:
    if data_dir and Path(data_dir).exists():
        return load_codex(data_dir, side=side)
    return synthetic_codex(side=side)


class BrainController:
    def __init__(self, config: ControllerConfig | None = None, gains: CircuitGains | None = None,
                 connectome: Connectome | None = None, looming: LoomingGains | None = None):
        self.cfg = cfg = config or ControllerConfig()
        if gains is None or (looming is None and connectome is not None):
            connectome = connectome or synthetic_codex()
            gains = gains or derive_gains(connectome.group_adjacency())
            looming = looming or derive_looming_gains(connectome.group_adjacency(neuropils=None))
        self.gains = gains
        self.optic = build_optic_lobe(gains, backend=cfg.backend, n_neurons=cfg.n_neurons)
        self.cx = CentralComplex()
        self.mb = MushroomBody(recovery_s=cfg.habituation_recovery_s, alert_threshold=cfg.alert_threshold,
                               sleep_time_s=cfg.sleep_time_s)
        self.gf = GiantFiber(looming or LoomingGains(), threshold=cfg.gf_threshold,
                             refractory_s=cfg.gf_refractory_s)
        self._escape_until = -math.inf
        self._gesture: list[list] = []  # [pan, tilt, seconds, elapsed] waypoints a command plays
        self._escape_dir = 1.0
        self._tilt_home: float | None = None  # tilt to settle back to after an escape
        self.events: list[dict] = []
        self._game_start: float | None = None
        self._game_last = -math.inf
        self.game_best = 0.0
        # commands from other threads (the LLM cortex), applied at the start of a step
        self._inbox: queue.SimpleQueue = queue.SimpleQueue()
        self._say: tuple[str, float] | None = None
        self._text_shown = False
        self._audio: dict | None = None  # one-shot sound queued by a command
        self.snapshot: dict = {}  # latest expression / pose / brain state, for readers on other threads
        self.personality = "curious"
        self.pan = 0.0
        self.tilt = 0.0
        self._nearness = 0.0
        self._clock = 0.0
        self._last_frame: tuple[float, float, float] | None = None
        self._last_velocity = (0.0, 0.0)
        # (time, gaze azimuth = heading + pan, tilt, gaze rate, tilt rate) per step, so a
        # camera frame is compared with where the head was when it was captured
        self._history: list[tuple[float, float, float, float, float]] = []

    # -- sensor decoding --------------------------------------------------
    def _target(self, s: SensorState) -> dict | None:
        c = s.camera
        if not c or not c.get("detected", True):
            return None
        if self._clock - s.stamps.get("camera", -math.inf) > self.cfg.target_timeout_s:
            return None
        w = c.get("width", c.get("w", self.cfg.frame_width))
        h = c.get("height", c.get("h", self.cfg.frame_height))
        stamp = s.stamps.get("camera")
        if "vx" in c and "vy" in c:
            vx, vy = c["vx"], c["vy"]
        else:
            # no motion vector in the message: differentiate successive frames
            last = self._last_frame
            if last and last[2] != stamp and stamp - last[2] < self.cfg.target_timeout_s:
                vx = (c["x"] - last[0]) / (stamp - last[2])
                vy = (c["y"] - last[1]) / (stamp - last[2])
            else:
                vx, vy = self._last_velocity
        if not self._last_frame or self._last_frame[2] != stamp:
            self._last_frame = (c["x"], c["y"], stamp)
            self._last_velocity = (vx, vy)
        return {
            "x": (c["x"] - w / 2) / (w / 2),
            "y": (c["y"] - h / 2) / (h / 2),
            "vx": vx / (w / 2),
            "vy": vy / (h / 2),
            "polarity": c.get("polarity", 0.0),
            "box_deg": [v * self.cfg.hfov_deg / w for v in c["box"]] if "box" in c else None,
        }

    def _head_at(self, t: float) -> tuple[float, float, float, float, float]:
        """Head state (time, gaze, tilt, gaze rate, tilt rate) at or just before ``t``."""
        h = self._history
        if not h:
            return (t, self.cx.heading + self.pan, self.tilt, 0.0, 0.0)
        i = bisect.bisect_right(h, t, key=lambda e: e[0])
        return h[max(i - 1, 0)]

    def _light(self, s: SensorState) -> tuple[float, float] | None:
        """(right-left, top-bottom) brightness contrast in -1..1 from the latest frame."""
        c = s.camera or {}
        lum = c.get("lum")
        if not lum or self._clock - s.stamps.get("camera", -math.inf) > self.cfg.target_timeout_s:
            return None
        left, right, top, bottom = (float(v) for v in lum)
        return (right - left) / (right + left + 1.0), (top - bottom) / (top + bottom + 1.0)

    # -- thread-safe commands (queued, applied on the control thread) --------
    def look_at(self, pan_deg: float, tilt_deg: float) -> None:
        """Steer the gaze through the FC2 goal (PFL3 turns there while no target is seen)."""
        def apply():
            pan = float(np.clip(pan_deg, *self.cfg.pan_limits))
            self.cx.set_goal(self.cx.heading + pan)
            self._tilt_home = float(np.clip(tilt_deg, *self.cfg.tilt_limits))
        self._inbox.put(apply)

    def configure(self, **changes) -> None:
        """Change ControllerConfig fields live (personality, game, thresholds)."""
        def apply():
            self.cfg = replace(self.cfg, **changes)
            self.mb.alert_threshold = self.cfg.alert_threshold
            self.mb.recovery_s = self.cfg.habituation_recovery_s
            self.mb.sleep_time_s = self.cfg.sleep_time_s
            self.gf.threshold = self.cfg.gf_threshold
            self.gf.refractory_s = self.cfg.gf_refractory_s
        self._inbox.put(apply)

    def set_personality(self, name: str) -> dict:
        """Apply a PERSONALITIES preset live; returns {field: (old, new)} for what changed."""
        if name not in PERSONALITIES:
            raise ValueError(f"unknown personality {name!r}")
        default = ControllerConfig()
        # every field any preset touches: this preset's value or the plain default
        changes = {k: PERSONALITIES[name].get(k, getattr(default, k))
                   for k in {k for preset in PERSONALITIES.values() for k in preset}}
        diff = {k: (getattr(self.cfg, k), v) for k, v in changes.items() if getattr(self.cfg, k) != v}
        self.configure(**changes)
        self.personality = name
        return diff

    GESTURES = ("left", "right", "up", "down", "center", "nod", "shake", "spin")

    def gesture(self, name: str) -> None:
        """Play a head movement asked for by voice, over the reflexes. A turn ends with the FC2
        goal there, so the head stays looking that way once no target pulls it elsewhere."""
        if name not in self.GESTURES:
            raise ValueError(f"unknown gesture {name!r}")

        def apply():
            lo, hi = self.cfg.pan_limits
            tlo, thi = self.cfg.tilt_limits
            pan, home = self.pan, float(np.clip(self.cfg.home_tilt, tlo, thi))
            side = min(45.0, -lo, hi)
            moves = {
                "left": [(-side, home, 1.2)],
                "right": [(side, home, 1.2)],
                "up": [(pan, thi, 1.0)],
                "down": [(pan, tlo, 1.0)],
                "center": [(0.0, home, 1.2)],
                "nod": [(pan, min(thi, home + 15), 0.35), (pan, max(tlo, home - 8), 0.35)] * 2 + [(pan, home, 0.4)],
                "shake": [(pan - 20, home, 0.35), (pan + 20, home, 0.35)] * 2 + [(pan, home, 0.4)],
                "spin": [(lo, home, 1.2), (hi, home, 1.8), (0.0, home, 1.2)],
            }[name]
            self._gesture = [[float(np.clip(p_, lo, hi)), float(np.clip(t_, tlo, thi)), d, 0.0] for p_, t_, d in moves]
        self._inbox.put(apply)

    def say(self, text: str, seconds: float = 6.0) -> None:
        """Show ``text`` in the speech balloon for ``seconds`` (overrides the game score)."""
        self._inbox.put(lambda: setattr(self, "_say", (text, self._clock + seconds)))

    def pet(self, hold_s: float = 4.0) -> None:
        """Head stroked (the base's touch strip): a happy face, like the petting reward."""
        self._inbox.put(lambda: self.mb.hold("happy", hold_s))

    def remind(self, reminder: dict, hold_s: float = 8.0) -> None:
        """Meeting reminder: look at the user, alert face, chime, balloon, and an event."""
        def apply():
            self.cx.set_goal(self.cx.heading + self.cfg.home_pan)
            self._tilt_home = self.cfg.home_tilt
            self.mb.hold("alert", hold_s)
            self._audio = AUDIO["reminder"]
            minutes = reminder.get("minutes", 0)
            if self.cfg.screen_lang == "th":
                screen = f"ประชุมใน {minutes} นาที" if minutes else "ได้เวลาประชุม!"
            else:
                screen = f"Meeting in {minutes} min ({reminder.get('start', '')})" if minutes else "Meeting now!"
            self._say = (screen, self._clock + max(hold_s, 15.0))
            title = reminder.get("title") or "ประชุม"
            where = f" ที่ {reminder['location']}" if reminder.get("location") else ""
            when = f"อีก {minutes} นาที" if minutes else "ถึงเวลาแล้ว"
            self.events.append({"type": "meeting", **reminder,
                                "key": f"meeting:{reminder.get('uid')}:{reminder.get('lead')}",
                                "message": f"StackChan: {when} ประชุม {title} เวลา {reminder.get('start', '')}{where}"})
        self._inbox.put(apply)

    def _apply_inbox(self) -> None:
        while True:
            try:
                self._inbox.get_nowait()()
            except queue.Empty:
                return

    def pop_events(self) -> list[dict]:
        events, self.events = self.events, []
        return events

    def _game(self, target_x: float | None, target_y: float | None) -> dict:
        cfg, now = self.cfg, self._clock
        locked = target_x is not None and abs(target_x) < cfg.game_lock and abs(target_y) < 1.5 * cfg.game_lock
        if locked:
            if self._game_start is None:
                self._game_start = now
            self._game_last = now
        elif self._game_start is not None and now - self._game_last > cfg.game_grace_s:
            streak = self._game_last - self._game_start
            if streak > self.game_best:
                self.game_best = streak
                self.events.append({"type": "record", "seconds": round(streak, 1)})
            self._game_start = None
        current = self._game_last - self._game_start if self._game_start is not None else 0.0
        return {"streak": round(current, 1), "best": round(max(self.game_best, current), 1)}

    def _darkness(self, s: SensorState) -> float:
        als = (s.proximity or {}).get("als")
        return float(np.clip(1.0 - als / self.cfg.als_dark, 0.0, 1.0)) if als is not None else 0.0

    def _nearness_from(self, s: SensorState) -> float:
        p = s.proximity or {}
        if "distance_mm" in p:
            return float(np.clip(1.0 - p["distance_mm"] / self.cfg.proximity_max_mm, 0.0, 1.0))
        if "ps" in p:
            return float(np.clip(p["ps"] / self.cfg.proximity_raw_max, 0.0, 1.0))
        return self._nearness

    # -- main loop --------------------------------------------------------
    def step(self, sensors: SensorState, dt: float, now: float | None = None) -> dict:
        cfg = self.cfg
        self._clock = time.monotonic() if now is None else now
        self._apply_inbox()
        target = self._target(sensors)

        # optic lobe: remove self-induced image motion with an efference copy taken at the
        # frame's capture time, and move the target to where it is relative to the head now
        if target:
            then = self._head_at(sensors.stamps["camera"] - cfg.sensor_latency_s)
            x = target["x"] - (self.cx.heading + self.pan - then[1]) / (cfg.hfov_deg / 2)
            y = target["y"] + (self.tilt - then[2]) / (cfg.vfov_deg / 2)
            vx = target["vx"] + then[3] / (cfg.hfov_deg / 2)
            vy = target["vy"] - then[4] / (cfg.vfov_deg / 2)
            stim = np.array([
                x, y,
                np.clip(vx / cfg.v_max, -1, 1), np.clip(vy / cfg.v_max, -1, 1),
                np.clip(target["polarity"], -1, 1),
            ])
        else:
            stim = np.zeros(5)
        hs, vs, px, py = self.optic.step(stim, dt)
        hs, vs, px, py = (0.0 if abs(v) < cfg.deadband else float(v) for v in (hs, vs, px, py))

        # image +x is right (pan +), image +y is down (tilt -)
        pan_rate = cfg.k_position * px + cfg.k_motion * hs
        tilt_rate = -(cfg.k_position * py + cfg.k_motion * vs)

        # looming: LPLC2 / LC4 -> Giant Fiber; an escape turns away from the object and up
        nearness = self._nearness_from(sensors)
        approach = (nearness - self._nearness) / dt if dt > 0 else 0.0
        self._nearness = nearness
        if target and target["box_deg"] is not None:
            self.gf.observe(target["box_deg"], sensors.stamps["camera"])
        elif not target:
            self.gf.observe(None, None)
        escape = self.gf.step(dt, self._clock, approach)
        if escape:
            self._escape_until = self._clock + cfg.escape_s
            self._escape_dir = -1.0 if (stim[0] if target else self.pan) >= 0 else 1.0
            if self._tilt_home is None:
                self._tilt_home = self.tilt
            self.mb.startle()
            self.events.append({"type": "escape", "from": "right" if self._escape_dir < 0 else "left"})
        escaping = self._clock < self._escape_until
        if escaping:
            pan_rate = self._escape_dir * cfg.max_rate_deg_s
            tilt_rate = 0.5 * cfg.max_rate_deg_s
        elif target:
            self._tilt_home = None
        else:
            if self._tilt_home is not None:
                tilt_rate += 2.0 * (self._tilt_home - self.tilt)  # settle back down
            light = self._light(sensors) if cfg.phototaxis else None
            if light:
                pan_rate += cfg.k_light * cfg.phototaxis * light[0]
                tilt_rate += cfg.k_light * cfg.phototaxis * light[1]

        # central complex: heading ring, VOR counter-rotation, goal memory
        imu = sensors.imu or {}
        gyro = imu.get("gyro")
        yaw_rate = cfg.imu_yaw_sign * gyro[2] if gyro else None
        yaw = cfg.imu_yaw_sign * imu["yaw"] if "yaw" in imu else None
        d_heading = self.cx.update_heading(yaw_rate, dt, yaw)
        if target and not escaping:
            self.cx.set_goal(self.cx.heading + self.pan + stim[0] * cfg.hfov_deg / 2)
        elif not escaping:
            pan_rate += cfg.k_heading * self.cx.steering(self.pan)

        if self._gesture:  # a commanded movement overrides the reflexes until it has played
            step = self._gesture[0]
            step[3] += dt
            pan_rate, tilt_rate = 6.0 * (step[0] - self.pan), 6.0 * (step[1] - self.tilt)
            if step[3] >= step[2]:
                self._gesture.pop(0)
                if not self._gesture:
                    self.cx.set_goal(self.cx.heading + step[0])
                    self._tilt_home = step[1]

        pan_rate = float(np.clip(pan_rate, -cfg.max_rate_deg_s, cfg.max_rate_deg_s))
        tilt_rate = float(np.clip(tilt_rate, -cfg.max_rate_deg_s, cfg.max_rate_deg_s))
        gaze_before, tilt_before = self.cx.heading - d_heading + self.pan, self.tilt
        self.pan = float(np.clip(self.pan + pan_rate * dt - cfg.vor_gain * d_heading, *cfg.pan_limits))
        self.tilt = float(np.clip(self.tilt + tilt_rate * dt, *cfg.tilt_limits))
        gaze = self.cx.heading + self.pan
        if dt > 0:
            self._history.append((self._clock, gaze, self.tilt, (gaze - gaze_before) / dt, (self.tilt - tilt_before) / dt))
            horizon = self._clock - max(1.0, 2 * cfg.sensor_latency_s + cfg.target_timeout_s)
            while self._history and self._history[0][0] < horizon:
                self._history.pop(0)

        # mushroom body / monoamines
        accel = imu.get("accel")
        shake = 0.0
        if accel:
            shake += abs(float(np.linalg.norm(accel)) - 1.0)  # g units
        if gyro:
            shake += float(np.linalg.norm(gyro)) / 300.0
        percept = Percept(
            motion=float(np.clip(np.hypot(*stim[2:4]), 0, 1)),
            x=float(stim[0]), y=float(stim[1]), target=target is not None,
            nearness=nearness, approach=float(approach), shake=float(min(shake, 1.0)),
            centered=float(max(0.0, 1.0 - np.hypot(*stim[:2]))) if target else 0.0,
            dark=self._darkness(sensors),
        )
        mods, expression, changed = self.mb.step(percept, dt)
        if changed and expression == "alert" and not escape:
            self.events.append({"type": "presence"})

        cmd = {
            "servo": {"pan_angle": round(self.pan, 2), "tilt_angle": round(self.tilt, 2)},
            "face": {"expression": expression},
            "audio": AUDIO["startle"] if escape else self._audio or (AUDIO[expression] if changed else None),
        }
        self._audio = None
        text = ""
        if cfg.game:
            game = self._game(stim[0] if target else None, stim[1] if target else None)
            cmd["game"] = game
            text = f"{game['streak']:.1f}s / best {game['best']:.1f}s"
        if self._say and self._clock < self._say[1]:
            text = self._say[0]
        if text or self._text_shown:  # one empty text clears the balloon
            cmd["text"] = shape(text)  # Thai marks pre-positioned for the bitmap font
        self._text_shown = bool(text)
        brain = {
            "HS": round(float(hs), 3), "VS": round(float(vs), 3),
            "LC10": [round(float(px), 3), round(float(py), 3)],
            "heading": round(self.cx.heading, 1),
            "dopamine": round(mods.dopamine, 3), "octopamine": round(mods.octopamine, 3),
            "novelty": round(mods.novelty, 3), "sleep_pressure": round(mods.sleep_pressure, 3),
            "LPLC2": round(self.gf.lplc2, 3), "LC4": round(self.gf.lc4, 3), "GF": round(self.gf.v, 3),
        }
        if cfg.telemetry:
            cmd["brain"] = brain
        self.snapshot = {"expression": expression, "pan": round(self.pan, 1), "tilt": round(self.tilt, 1),
                         "text": text,  # unshaped, for displays with real Thai shaping
                         "target_visible": target is not None, "brain": brain,
                         **({"game": cmd["game"]} if "game" in cmd else {})}
        for event in self.events:
            event.setdefault("time", round(self._clock, 3))
        return cmd

    def close(self) -> None:
        self.optic.close()
