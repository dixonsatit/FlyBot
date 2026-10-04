"""Tune ControllerConfig against the simulated StackChan (``flybot.plant``).

    python -m flybot.tune                      # score the current config
    python -m flybot.tune --search             # grid search k_position / k_motion / k_heading
    python -m flybot.tune --set k_position=60 --plant sensor_latency_s=0.15

Score = mean absolute gaze error (deg; elevation weighted 0.5) over three scenarios
(smooth pursuit, darting target that stops between moves, body turned under a target),
averaged over plant variants (nominal, slow network, strict ego-motion gate).
"""
from __future__ import annotations

import argparse
import itertools
import logging
import math
from dataclasses import fields, replace

import numpy as np

from .controller import BrainController, ControllerConfig, SensorState, apply_overrides, load_connectome
from .looming import LoomingGains, derive_looming_gains
from .optic_lobe import CircuitGains, derive_gains
from .plant import PlantConfig, StackChanPlant, World

CONTROL_DT = 0.05  # mqtt_bridge default 20 Hz
SUBSTEPS = 5


def pursuit(t: float) -> World:
    return World(target_az=30.0 * math.sin(2 * math.pi * 0.2 * t),
                 target_el=15.0 + 8.0 * math.sin(2 * math.pi * 0.13 * t))


_DARTS = [(0.0, 10.0), (-20.0, 10.0), (5.0, 20.0), (-15.0, 5.0), (15.0, 15.0), (0.0, 25.0)]  # hops stay in the FOV


def darts(t: float) -> World:
    """Move 0.4 s to the next spot, then stand still (invisible to frame differencing) 1.6 s."""
    i, phase = divmod(t, 2.0)
    i = min(int(i), len(_DARTS) - 2)
    a, b = _DARTS[i], _DARTS[i + 1]
    f = min(phase / 0.4, 1.0)
    return World(target_az=a[0] + f * (b[0] - a[0]), target_el=a[1] + f * (b[1] - a[1]))


def body_turn(t: float) -> World:
    """Target jiggles near 20 deg while the body is turned 45 deg and back."""
    rate = 45.0 if 2.0 <= t < 3.0 else -45.0 if 6.0 <= t < 7.0 else 0.0
    return World(target_az=20.0 + 5.0 * math.sin(2 * math.pi * 0.5 * t), target_el=10.0, body_yaw_rate=rate)


SCENARIOS = {"pursuit": (pursuit, 10.0), "darts": (darts, 10.0), "body_turn": (body_turn, 9.0)}
PLANTS = {
    "nominal": {},
    "slow_net": {"sensor_latency_s": 0.15, "command_latency_s": 0.06},
    "strict_ego": {"ego_motion_deg_s": 20.0},
}


def run(cfg: ControllerConfig, gains: CircuitGains, plant_cfg: PlantConfig, scenario: str,
        looming: LoomingGains | None = None) -> dict:
    world_at, duration = SCENARIOS[scenario]
    brain = BrainController(cfg, gains=gains, looming=looming)
    plant = StackChanPlant(plant_cfg)
    sensors = SensorState()
    errs, pans = [], []
    sub = CONTROL_DT / SUBSTEPS
    try:
        for i in range(int(duration / sub)):
            t = (i + 1) * sub
            world = world_at(t)
            for kind, payload in plant.step(t, sub, world):
                sensors.update(kind, payload, now=t)
            if (i + 1) % SUBSTEPS == 0:
                plant.command(t, brain.step(sensors, CONTROL_DT, now=t))
                pans.append(plant.pan)
            err = plant.gaze_error(world)
            if err is not None and t > 0.5:
                errs.append((abs(err[0]), abs(err[1])))
    finally:
        brain.close()
    e = np.array(errs)
    jitter = float(np.mean(np.abs(np.diff(pans, 2)))) if len(pans) > 2 else 0.0
    return {"az": float(e[:, 0].mean()), "el": float(e[:, 1].mean()),
            "score": float(e[:, 0].mean() + 0.5 * e[:, 1].mean()), "jitter": jitter}


def evaluate(cfg: ControllerConfig, gains: CircuitGains, plant_cfg: PlantConfig,
             plants: dict[str, dict] = PLANTS, looming: LoomingGains | None = None) -> dict:
    table = {(p, s): run(cfg, gains, replace(plant_cfg, **overrides), s, looming)
             for p, overrides in plants.items() for s in SCENARIOS}
    return {"score": float(np.mean([r["score"] for r in table.values()])),
            "jitter": float(np.mean([r["jitter"] for r in table.values()])), "table": table}


def _print_table(result: dict) -> None:
    print(f"{'plant':<11}{'scenario':<11}{'az err':>8}{'el err':>8}{'jitter':>8}")
    for (p, s), r in result["table"].items():
        print(f"{p:<11}{s:<11}{r['az']:8.2f}{r['el']:8.2f}{r['jitter']:8.3f}")
    print(f"score {result['score']:.2f} deg, jitter {result['jitter']:.3f}")


def _plant_overrides(base: PlantConfig, items: list[str] | None) -> PlantConfig:
    names = {f.name: f for f in fields(base)}
    changes = {}
    for item in items or []:
        key, _, raw = item.partition("=")
        if key not in names:
            raise ValueError(f"unknown plant field {key!r}; choose from {sorted(names)}")
        cur = getattr(base, key)
        changes[key] = tuple(type(v)(x) for v, x in zip(cur, raw.split(","))) if isinstance(cur, tuple) else type(cur)(raw)
    return replace(base, **changes)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir")
    ap.add_argument("--backend", default="rate", choices=["rate", "nengo"],
                    help="rate is ~20x faster; the best config is re-checked with nengo")
    ap.add_argument("--set", action="append", metavar="KEY=VALUE", help="ControllerConfig override")
    ap.add_argument("--plant", action="append", metavar="KEY=VALUE", help="PlantConfig override")
    ap.add_argument("--search", action="store_true", help="grid search the steering gains")
    ap.add_argument("--top", type=int, default=5)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    try:
        cfg = apply_overrides(ControllerConfig(backend=args.backend, telemetry=False), args.set)
        plant_cfg = _plant_overrides(PlantConfig(), args.plant)
    except ValueError as e:
        ap.error(str(e))
    cfg = replace(cfg, hfov_deg=plant_cfg.hfov_deg, vfov_deg=plant_cfg.vfov_deg)
    connectome = load_connectome(args.data_dir)
    gains = derive_gains(connectome.group_adjacency())
    looming = derive_looming_gains(connectome.group_adjacency(neuropils=None))

    base = evaluate(cfg, gains, plant_cfg, looming=looming)
    print("== current config:", {k: getattr(cfg, k) for k in ("k_position", "k_motion", "k_heading", "tilt_limits")})
    _print_table(base)
    if not args.search:
        return

    grid = {"k_position": [30, 45, 60, 90, 120, 160], "k_motion": [0, 15, 30, 60], "k_heading": [20, 40, 80]}
    results = []
    for values in itertools.product(*grid.values()):
        trial = replace(cfg, **dict(zip(grid, map(float, values))))
        r = evaluate(trial, gains, plant_cfg, looming=looming)
        results.append((r["score"], r["jitter"], dict(zip(grid, values))))
    results.sort(key=lambda r: r[0])
    print(f"\n== top {args.top} of {len(results)} (score deg, jitter)")
    for score, jitter, values in results[: args.top]:
        print(f"{score:6.2f} {jitter:7.3f}  " + " ".join(f"--set {k}={v}" for k, v in values.items()))

    best = replace(cfg, **{k: float(v) for k, v in results[0][2].items()})
    if args.backend != "nengo":
        try:
            import nengo  # noqa: F401
        except ImportError:
            return
        print("\n== best config re-checked with the nengo backend")
        _print_table(evaluate(replace(best, backend="nengo"), gains, plant_cfg, looming=looming))


if __name__ == "__main__":
    main()
