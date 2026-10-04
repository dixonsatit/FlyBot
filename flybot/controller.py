"""BrainController: StackChan sensors -> FlyWire circuits -> servo/face/audio JSON."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .central_complex import CentralComplex
from .connectome import Connectome, load_codex, synthetic_codex
from .mushroom_body import MushroomBody, Percept
from .optic_lobe import CircuitGains, build_optic_lobe, derive_gains

# M5.Speaker.tone(freq, ms) sequences for each expression change
AUDIO = {
    "alert": {"tones": [[1800, 80], [0, 40], [1800, 80]], "volume": 180},
    "curious": {"tones": [[600, 60], [900, 60], [1200, 90]], "volume": 120},
    "happy": {"tones": [[880, 90], [1320, 140]], "volume": 140},
    "sleepy": {"tones": [[500, 150], [350, 200], [250, 300]], "volume": 60},
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
    imu_yaw_sign: float = -1.0  # gyro z is CCW-positive; internal yaw is CW-positive
    proximity_max_mm: float = 400.0
    proximity_raw_max: float = 2047.0  # LTR-553 PS counts
    backend: str = "nengo"
    n_neurons: int = 60
    telemetry: bool = True


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


def load_connectome(data_dir: str | Path | None, side: str = "R") -> Connectome:
    if data_dir and Path(data_dir).exists():
        return load_codex(data_dir, side=side)
    return synthetic_codex(side=side)


class BrainController:
    def __init__(self, config: ControllerConfig | None = None, gains: CircuitGains | None = None,
                 connectome: Connectome | None = None):
        self.cfg = config or ControllerConfig()
        if gains is None:
            gains = derive_gains((connectome or synthetic_codex()).group_adjacency())
        self.gains = gains
        self.optic = build_optic_lobe(gains, backend=self.cfg.backend, n_neurons=self.cfg.n_neurons)
        self.cx = CentralComplex()
        self.mb = MushroomBody()
        self.pan = 0.0
        self.tilt = 0.0
        self._rates = (0.0, 0.0)
        self._nearness = 0.0
        self._clock = 0.0
        self._last_frame: tuple[float, float, float] | None = None
        self._last_velocity = (0.0, 0.0)

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
        }

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
        target = self._target(sensors)

        # optic lobe: remove self-induced image motion (efference copy of the last command)
        pan_rate, tilt_rate = self._rates
        if target:
            vx = target["vx"] + pan_rate / (cfg.hfov_deg / 2)
            vy = target["vy"] - tilt_rate / (cfg.vfov_deg / 2)
            stim = np.array([
                target["x"], target["y"],
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

        # central complex: heading ring, VOR counter-rotation, goal memory
        imu = sensors.imu or {}
        gyro = imu.get("gyro")
        yaw_rate = cfg.imu_yaw_sign * gyro[2] if gyro else None
        yaw = cfg.imu_yaw_sign * imu["yaw"] if "yaw" in imu else None
        d_heading = self.cx.update_heading(yaw_rate, dt, yaw)
        if target:
            self.cx.set_goal(self.cx.heading + self.pan + target["x"] * cfg.hfov_deg / 2)
        else:
            pan_rate += cfg.k_heading * self.cx.steering(self.pan)

        pan_rate = float(np.clip(pan_rate, -cfg.max_rate_deg_s, cfg.max_rate_deg_s))
        tilt_rate = float(np.clip(tilt_rate, -cfg.max_rate_deg_s, cfg.max_rate_deg_s))
        self.pan = float(np.clip(self.pan + pan_rate * dt - cfg.vor_gain * d_heading, *cfg.pan_limits))
        self.tilt = float(np.clip(self.tilt + tilt_rate * dt, *cfg.tilt_limits))
        self._rates = (pan_rate, tilt_rate)

        # mushroom body / monoamines
        nearness = self._nearness_from(sensors)
        approach = (nearness - self._nearness) / dt if dt > 0 else 0.0
        self._nearness = nearness
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
        )
        mods, expression, changed = self.mb.step(percept, dt)

        cmd = {
            "servo": {"pan_angle": round(self.pan, 2), "tilt_angle": round(self.tilt, 2)},
            "face": {"expression": expression},
            "audio": AUDIO[expression] if changed else None,
        }
        if cfg.telemetry:
            cmd["brain"] = {
                "HS": round(float(hs), 3), "VS": round(float(vs), 3),
                "LC10": [round(float(px), 3), round(float(py), 3)],
                "heading": round(self.cx.heading, 1),
                "dopamine": round(mods.dopamine, 3), "octopamine": round(mods.octopamine, 3),
                "novelty": round(mods.novelty, 3), "sleep_pressure": round(mods.sleep_pressure, 3),
            }
        return cmd

    def close(self) -> None:
        self.optic.close()
