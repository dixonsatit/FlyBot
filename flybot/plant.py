"""Simulated StackChan hardware for tuning ``ControllerConfig`` before the robot exists.

Mirrors ``firmware/stackchan`` rather than an ideal camera:

* SG90 servos: slew-rate limit plus first-order lag, clamped to the mechanical range
  (the tilt servo only travels 60..90 deg, i.e. tilt 0..30 with ``SERVO_Y_SIGN = -1``).
* Frame-differencing detector at ``camera_hz``: a target is only seen while it moves in
  the image faster than ``min_image_speed_deg_s``; the changed-pixel centroid sits between
  the old and new positions (half a frame late); frames taken while the head turns faster
  than ``ego_motion_deg_s`` are dropped (``MAX_MOTION_FRACTION``); ``vx, vy`` use the
  firmware's smoothed finite difference; ``box`` is the bounding box of the changed
  pixels (old + new blob) and ``lum`` the mean brightness of the left/right/top/bottom
  halves, which a light source off-axis makes unequal.
* WiFi/broker latency on sensor messages and commands.

Angles are degrees in the world frame, positive = right / up; body yaw is clockwise-positive
and the IMU reports gyro z counter-clockwise-positive like the BMI270.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np


@dataclass
class PlantConfig:
    servo_speed_deg_s: float = 500.0  # SG90 ~0.12 s/60 deg at 5 V
    servo_tau_s: float = 0.04
    pan_range: tuple[float, float] = (-90.0, 90.0)
    tilt_range: tuple[float, float] = (0.0, 30.0)
    frame: tuple[int, int] = (160, 120)
    hfov_deg: float = 62.0
    vfov_deg: float = 49.0
    camera_hz: float = 25.0
    sensor_latency_s: float = 0.08  # capture + WiFi + broker
    command_latency_s: float = 0.03
    min_image_speed_deg_s: float = 4.0
    ego_motion_deg_s: float = 40.0
    noise_px: float = 1.5
    imu_hz: float = 30.0
    proximity_hz: float = 10.0
    seed: int = 0


@dataclass
class World:
    """Ground truth at one instant."""

    target_az: float | None = None  # None = no target in the scene
    target_el: float = 0.0
    target_size_deg: float = 6.0  # angular size; grows as the object approaches
    light_az: float | None = None  # a lamp in the world frame, None = even lighting
    light_el: float = 0.0
    als: float = 200.0  # ambient light sensor counts
    body_yaw_rate: float = 0.0  # deg/s, clockwise-positive
    distance_mm: float = 1000.0


class StackChanPlant:
    def __init__(self, cfg: PlantConfig | None = None):
        self.cfg = cfg or PlantConfig()
        self.rng = np.random.default_rng(self.cfg.seed)
        self._box_rng = np.random.default_rng(self.cfg.seed + 1)  # keeps the tracking noise sequence independent
        self.pan = self.tilt = 0.0  # actual servo angles
        self.pan_rate = self.tilt_rate = 0.0
        self.body_yaw = 0.0
        self._cmd = (0.0, 0.0)
        self._cmd_queue: deque[tuple[float, tuple[float, float]]] = deque()
        self._out: deque[tuple[float, str, dict]] = deque()
        self._next = {"camera": 0.0, "imu": 0.0, "proximity": 0.0}
        self._prev_target: tuple[float, float, float] | None = None  # image x, y, size at the last frame
        self._fw_last: tuple[float, float, float] | None = None  # firmware lastX, lastY, lastSeen
        self._fw_v = (0.0, 0.0)

    # -- commands -----------------------------------------------------------
    def command(self, t: float, cmd: dict) -> None:
        servo = cmd.get("servo") or {}
        self._cmd_queue.append((t + self.cfg.command_latency_s,
                                (servo.get("pan_angle", 0.0), servo.get("tilt_angle", 0.0))))

    def _move_servos(self, t: float, dt: float) -> None:
        while self._cmd_queue and self._cmd_queue[0][0] <= t:
            self._cmd = self._cmd_queue.popleft()[1]
        c = self.cfg
        targets = (np.clip(self._cmd[0], *c.pan_range), np.clip(self._cmd[1], *c.tilt_range))
        new = []
        for pos, goal in zip((self.pan, self.tilt), targets):
            step = (goal - pos) * (1.0 - math.exp(-dt / c.servo_tau_s))
            limit = c.servo_speed_deg_s * dt
            new.append(pos + float(np.clip(step, -limit, limit)))
        self.pan_rate, self.tilt_rate = (new[0] - self.pan) / dt, (new[1] - self.tilt) / dt
        self.pan, self.tilt = new

    # -- sensors ----------------------------------------------------------------
    def _emit(self, t: float, kind: str, payload: dict) -> None:
        self._out.append((t + self.cfg.sensor_latency_s, kind, payload))

    def _lum(self, w: World) -> list[float]:
        if w.light_az is None:
            return [100.0] * 4
        a = w.light_az - self.body_yaw - self.pan
        e = w.light_el - self.tilt
        return [100 - 60 * math.tanh(a / 20), 100 + 60 * math.tanh(a / 20),
                100 + 60 * math.tanh(e / 20), 100 - 60 * math.tanh(e / 20)]

    def _camera_frame(self, t: float, w: World) -> None:
        c = self.cfg
        if w.target_az is None:
            image = None
        else:
            image = (w.target_az - self.body_yaw - self.pan, w.target_el - self.tilt, w.target_size_deg)
            if abs(image[0]) > c.hfov_deg / 2 or abs(image[1]) > c.vfov_deg / 2:
                image = None
        prev, self._prev_target = self._prev_target, image
        head_speed = math.hypot(self.pan_rate - w.body_yaw_rate, self.tilt_rate)
        if head_speed > c.ego_motion_deg_s:
            return  # whole image changed: the firmware skips the frame
        lum = [round(v, 1) for v in self._lum(w)]
        moving = (image is not None and prev is not None and
                  max(math.hypot(image[0] - prev[0], image[1] - prev[1]), abs(image[2] - prev[2]) / 2)
                  * c.camera_hz > c.min_image_speed_deg_s)
        if not moving:
            self._fw_last = None
            self._emit(t, "camera", {"detected": False, "lum": lum})
            return
        width, height = c.frame
        px_x, px_y = width / c.hfov_deg, height / c.vfov_deg
        mid = ((image[0] + prev[0]) / 2, (image[1] + prev[1]) / 2)  # centroid of old+new blobs
        x = width / 2 + mid[0] * px_x + self.rng.normal(0, c.noise_px)
        y = height / 2 - mid[1] * px_y + self.rng.normal(0, c.noise_px)
        if self._fw_last and t - self._fw_last[2] < 0.3:
            dt = t - self._fw_last[2]
            vx = 0.6 * self._fw_v[0] + 0.4 * (x - self._fw_last[0]) / dt
            vy = 0.6 * self._fw_v[1] + 0.4 * (y - self._fw_last[1]) / dt
        else:
            vx = vy = 0.0
        self._fw_last, self._fw_v = (x, y, t), (vx, vy)
        polarity = 1.0  # bright-on-dark target
        # bounding box of the changed pixels = union of the old and new blob (px, 1 px noise per edge)
        edges = []
        for (cx, cy, size) in (image, prev):
            half = size / 2
            edges.append((width / 2 + (cx - half) * px_x, height / 2 - (cy + half) * px_y,
                          width / 2 + (cx + half) * px_x, height / 2 - (cy - half) * px_y))
        box = [min(edges[0][0], edges[1][0]), min(edges[0][1], edges[1][1]),
               max(edges[0][2], edges[1][2]), max(edges[0][3], edges[1][3])]
        box = [round(float(np.clip(v + self._box_rng.normal(0, 1.0), 0, lim)), 1)
               for v, lim in zip(box, (width - 1, height - 1, width - 1, height - 1))]
        self._emit(t, "camera", {"x": x, "y": y, "vx": vx, "vy": vy, "box": box,
                                 "width": width, "height": height, "polarity": polarity, "lum": lum})

    def step(self, t: float, dt: float, world: World) -> list[tuple[str, dict]]:
        """Advance to ``t``; return sensor messages that reach the bridge by then."""
        self._move_servos(t, dt)
        self.body_yaw += world.body_yaw_rate * dt
        c = self.cfg
        if t >= self._next["camera"]:
            self._next["camera"] += 1.0 / c.camera_hz
            self._camera_frame(t, world)
        if t >= self._next["imu"]:
            self._next["imu"] += 1.0 / c.imu_hz
            gz = -world.body_yaw_rate + self.rng.normal(0, 0.5)
            self._emit(t, "imu", {"gyro": [0.0, 0.0, gz], "accel": [0.0, 0.0, 1.0]})
        if t >= self._next["proximity"]:
            self._next["proximity"] += 1.0 / c.proximity_hz
            self._emit(t, "proximity", {"distance_mm": world.distance_mm, "als": world.als})
        out = []
        while self._out and self._out[0][0] <= t:
            _, kind, payload = self._out.popleft()
            out.append((kind, payload))
        return out

    def gaze_error(self, world: World) -> tuple[float, float] | None:
        """(azimuth, elevation) error between where the head points and the target."""
        if world.target_az is None:
            return None
        return (world.target_az - self.body_yaw - self.pan, world.target_el - self.tilt)
