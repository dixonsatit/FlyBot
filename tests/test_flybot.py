import numpy as np
import pytest

from flybot import BrainController, ControllerConfig, SensorState, derive_gains, load_codex, synthetic_codex
from flybot.central_complex import CentralComplex
from flybot.connectome import write_codex_csv
from flybot.mushroom_body import MushroomBody, Percept
from flybot.optic_lobe import CircuitGains, OpticLobeNetwork, RateOpticLobe


@pytest.fixture(scope="module")
def gains():
    return derive_gains(synthetic_codex().group_adjacency())


def test_codex_csv_roundtrip(tmp_path):
    syn = synthetic_codex(side="R")
    write_codex_csv(syn, tmp_path)
    loaded = load_codex(tmp_path, side="R")
    assert len(loaded.connections) == len(syn.connections)
    assert len(load_codex(tmp_path, side="L").connections) == 0
    adj = loaded.group_adjacency()
    assert adj.loc["Mi1", "T4a"] > 0 and adj.loc["L1", "T4a"] == 0
    signed = loaded.group_adjacency(signed=True)
    assert signed.loc["LPi", "HS"] < 0 < signed.loc["T4a", "HS"]


def test_gains_are_balanced(gains):
    feed = list(gains.on.values()) + list(gains.off.values())
    assert np.mean(feed) == pytest.approx(1.0)
    assert all(v > 0 for v in feed)
    assert gains.hs_exc > 0 and gains.hs_inh > 0


def test_missing_pathway_falls_back_to_uniform():
    syn = synthetic_codex()
    syn.connections = syn.connections.iloc[:0]
    g = derive_gains(syn.group_adjacency())
    assert g.on == CircuitGains.uniform().on


@pytest.mark.parametrize("cls", [RateOpticLobe, OpticLobeNetwork])
def test_optic_lobe_direction_selectivity(cls, gains):
    lobe = cls(gains)
    right = lobe.step(np.array([0, 0, 0.8, 0, 0]), 0.3)
    left = lobe.step(np.array([0, 0, -0.8, 0, 0]), 0.3)
    down = lobe.step(np.array([0, 0, 0, 0.8, 0]), 0.3)
    pos = lobe.step(np.array([0.5, -0.4, 0, 0, 0]), 0.3)
    lobe.close()
    assert right[0] > 0.3 and left[0] < -0.3
    assert down[1] > 0.3 and abs(down[0]) < 0.15
    assert pos[2] == pytest.approx(0.5, abs=0.15) and pos[3] == pytest.approx(-0.4, abs=0.15)


def test_central_complex_ring():
    cx = CentralComplex()
    for _ in range(10):
        cx.update_heading(30.0, 0.1)
    assert cx.heading == pytest.approx(30.0, abs=0.5)
    cx.set_goal(60.0)
    assert cx.steering(pan_deg=0.0) > 0  # goal to the right -> turn right
    assert cx.steering(pan_deg=60.0) < 0
    assert abs(cx.steering(pan_deg=30.0)) < 1e-6


def test_mushroom_body_habituates():
    mb = MushroomBody()
    p = Percept(motion=0.6, x=0.3, target=True)
    first = mb.step(p, 0.05)[0].novelty
    for _ in range(100):
        habituated = mb.step(p, 0.05)[0].novelty
    assert habituated < 0.5 * first
    assert mb.step(Percept(nearness=0.8, shake=0.0), 0.05)[0].novelty > habituated


def camera(x, y=120, vx=0.0, vy=0.0):
    return {"x": x, "y": y, "vx": vx, "vy": vy}


@pytest.mark.parametrize("backend", ["rate", "nengo"])
def test_controller_turns_towards_target(backend, gains):
    brain = BrainController(ControllerConfig(backend=backend), gains=gains)
    s = SensorState()
    t = 0.0
    for _ in range(10):
        s.update("camera", camera(260, 60), now=t)
        cmd = brain.step(s, 0.05, now=t)
        t += 0.05
    assert cmd["servo"]["pan_angle"] > 5  # target right of centre
    assert cmd["servo"]["tilt_angle"] > 2  # target above centre
    assert cmd["face"]["expression"] in {"curious", "alert", "sleepy", "happy"}
    brain.close()


def test_controller_limits_and_vor(gains):
    cfg = ControllerConfig(backend="rate")
    brain = BrainController(cfg, gains=gains)
    s = SensorState()
    for i in range(200):
        s.update("camera", camera(320, 240), now=i * 0.05)
        cmd = brain.step(s, 0.05, now=i * 0.05)
    assert cmd["servo"]["pan_angle"] <= 90 and cmd["servo"]["tilt_angle"] >= -45

    brain = BrainController(cfg, gains=gains)
    s.update("camera", {"detected": False}, now=0)
    # body rotates 20 deg clockwise (gyro z is CCW-positive)
    for i in range(10):
        s.update("imu", {"gyro": [0, 0, -40.0]}, now=i * 0.05)
        brain.step(s, 0.05, now=i * 0.05)
    assert brain.pan == pytest.approx(-20.0, abs=1.0)


def test_controller_estimates_velocity_without_motion_vector(gains):
    brain = BrainController(ControllerConfig(backend="rate"), gains=gains)
    s = SensorState()
    for i in range(4):
        s.update("camera", {"x": 160 + 20 * i, "y": 120}, now=i * 0.05)
        cmd = brain.step(s, 0.05, now=i * 0.05)
    assert cmd["brain"]["HS"] > 0


def test_v783_type_names_map_to_groups():
    import pandas as pd
    from flybot.connectome import Connectome
    names = ["T4a", "Mi1", "LPi03", "HSE", "VS1", "VSm", "LC10a", "H1", "Dm3q"]
    con = Connectome(pd.DataFrame(), pd.Series(names, index=range(len(names))), "test")
    assert con.groups().tolist() == ["T4a", "Mi1", "LPi", "HS", "VS", "VS", "LC10"]


def test_new_object_after_idle_triggers_alert(gains):
    brain = BrainController(ControllerConfig(backend="rate"), gains=gains)
    s = SensorState()
    for i in range(600):  # 30 s with no sensor data habituates the empty scene
        brain.step(s, 0.05, now=i * 0.05)
    faces = []
    for i in range(40):
        t = 30 + i * 0.05
        s.update("camera", camera(160 + 60 * np.sin(np.pi * i * 0.05), vx=280.0), now=t)
        faces.append(brain.step(s, 0.05, now=t)["face"]["expression"])
    assert "alert" in faces


def test_apply_overrides():
    from flybot.controller import apply_overrides
    cfg = apply_overrides(ControllerConfig(), ["k_position=60", "tilt_limits=0,30", "telemetry=false"])
    assert cfg.k_position == 60.0 and cfg.tilt_limits == (0.0, 30.0) and cfg.telemetry is False
    with pytest.raises(ValueError):
        apply_overrides(ControllerConfig(), ["no_such_field=1"])


@pytest.mark.parametrize("scenario", ["pursuit", "darts", "body_turn"])
def test_tracks_simulated_stackchan(scenario, gains):
    from flybot.plant import PlantConfig
    from flybot.tune import run
    r = run(ControllerConfig(backend="rate", telemetry=False), gains, PlantConfig(), scenario)
    assert r["az"] < 10 and r["el"] < 6


def test_latency_compensation_helps_on_slow_network(gains):
    from flybot.plant import PlantConfig
    from flybot.tune import run
    slow = PlantConfig(sensor_latency_s=0.15, command_latency_s=0.06)
    on = run(ControllerConfig(backend="rate", sensor_latency_s=0.2), gains, slow, "darts")
    off = run(ControllerConfig(backend="rate", sensor_latency_s=0.0), gains, slow, "darts")
    assert on["score"] < off["score"]
