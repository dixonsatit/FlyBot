"""Offline demo: a scripted scene drives the controller and prints command JSON.

    python -m flybot.sim [--data-dir data/codex] [--backend rate]

Scene: 0-4 s a target moves left/right, 4-5 s the robot body is rotated 30 deg,
6-9 s a hand hovers close (petting), 9-30 s nothing happens.
"""
from __future__ import annotations

import argparse
import json
import math

from .controller import BrainController, ControllerConfig, SensorState, apply_overrides, load_connectome
from .optic_lobe import derive_gains


def scene(t: float, cfg: ControllerConfig, pan: float) -> dict:
    s = {}
    if t < 4.0:
        world = 35.0 * math.sin(2 * math.pi * 0.25 * t)  # target direction (deg)
        world_v = 35.0 * 2 * math.pi * 0.25 * math.cos(2 * math.pi * 0.25 * t)
        px_per_deg = cfg.frame_width / cfg.hfov_deg
        s["camera"] = {"x": cfg.frame_width / 2 + (world - pan) * px_per_deg, "y": 120,
                       "vx": world_v * px_per_deg, "vy": 0.0, "detected": True}
    else:
        s["camera"] = {"detected": False}
    s["imu"] = {"gyro": [0.0, 0.0, -30.0 if 4.0 <= t < 5.0 else 0.0], "accel": [0.0, 0.0, 1.0]}
    s["proximity"] = {"distance_mm": 80.0 if 6.0 <= t < 9.0 else 1000.0}
    return s


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir")
    ap.add_argument("--backend", default="nengo", choices=["nengo", "rate"])
    ap.add_argument("--duration", type=float, default=30.0)
    ap.add_argument("--dt", type=float, default=0.05)
    ap.add_argument("--every", type=int, default=10, help="print every N steps")
    ap.add_argument("--set", action="append", metavar="KEY=VALUE",
                    help="override a ControllerConfig field, e.g. --set k_position=60 --set tilt_limits=0,30")
    args = ap.parse_args(argv)

    try:
        # scene() builds frames from the current pan, so there is no latency to compensate
        cfg = apply_overrides(ControllerConfig(backend=args.backend, sensor_latency_s=0.0), args.set)
    except ValueError as e:
        ap.error(str(e))
    brain = BrainController(cfg, gains=derive_gains(load_connectome(args.data_dir).group_adjacency()))
    sensors = SensorState()
    for i in range(int(args.duration / args.dt)):
        t = i * args.dt
        for kind, payload in scene(t, cfg, brain.pan).items():
            sensors.update(kind, payload, now=t)
        cmd = brain.step(sensors, args.dt, now=t)
        if i % args.every == 0 or cmd["audio"]:
            print(json.dumps({"t": round(t, 2), **cmd}, ensure_ascii=False))
    brain.close()


if __name__ == "__main__":
    main()
