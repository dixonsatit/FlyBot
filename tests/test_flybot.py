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


# -- looming escape, phototaxis, personalities, events, game ---------------------

def test_giant_fiber_type_from_consolidated_table(tmp_path):
    import pandas as pd
    syn = synthetic_codex(side="R")
    write_codex_csv(syn, tmp_path)
    # Giant Fiber only named in the whole-brain table, like Codex v783
    visual = pd.read_csv(tmp_path / "visual_neuron_types.csv.gz")
    gf = visual["type"] == "DNp01"
    visual[~gf].to_csv(tmp_path / "visual_neuron_types.csv.gz", index=False)
    visual[gf].rename(columns={"type": "primary_type"}).to_csv(tmp_path / "consolidated_cell_types.csv.gz", index=False)
    con = load_codex(tmp_path, side="R")
    adj = con.group_adjacency(neuropils=None)
    assert adj.loc["LPLC2", "GF"] > 0 and adj.loc["LC4", "GF"] > 0
    assert con.group_adjacency().loc["LPLC2", "GF"] == 0  # PVLP is outside the optic-lobe default


def test_looming_gains_from_connectome():
    from flybot.looming import derive_looming_gains
    g = derive_looming_gains(synthetic_codex().group_adjacency(neuropils=None))
    assert g.lplc2 > 0 and g.lc4 > 0 and g.lplc2 + g.lc4 == pytest.approx(2.0)


def _feed_boxes(gf, boxes, dt=0.04):
    fired = False
    for i, box in enumerate(boxes):
        gf.observe(box, i * dt)
        fired |= gf.step(dt, i * dt)
    return fired


def test_giant_fiber_fires_on_expansion_not_translation():
    from flybot.looming import GiantFiber, LoomingGains
    expand = [(-h, -h, h, h) for h in np.geomspace(6, 30, 15)]
    slide = [(x - 15, -15, x + 15, 15) for x in np.linspace(-20, 20, 15)]
    assert _feed_boxes(GiantFiber(LoomingGains()), expand)
    assert not _feed_boxes(GiantFiber(LoomingGains()), slide)


def test_controller_escapes_from_looming_object(gains):
    brain = BrainController(ControllerConfig(backend="rate"), gains=gains)
    s, escaped = SensorState(), None
    for i in range(30):
        t = i * 0.05
        half = 10 * 1.12 ** i  # px, object right of centre rushing in
        s.update("camera", {"x": 220, "y": 120, "vx": 0, "vy": 0,
                            "box": [220 - half, 120 - half, 220 + half, 120 + half]}, now=t)
        cmd = brain.step(s, 0.05, now=t)
        events = brain.pop_events()
        if any(e["type"] == "escape" for e in events) and escaped is None:
            escaped, pan_at_escape = cmd, brain.pan
    assert escaped is not None and escaped["audio"]["volume"] == 200
    assert brain.pan < pan_at_escape  # turned away (left) from the object on the right


@pytest.mark.parametrize("name, sign", [("bold", 1), ("skittish", -1)])
def test_phototaxis_follows_personality(name, sign, gains):
    from dataclasses import replace
    from flybot.controller import personality
    brain = BrainController(replace(personality(name), backend="rate"), gains=gains)
    s = SensorState()
    for i in range(20):
        s.update("camera", {"detected": False, "lum": [60, 160, 100, 100]}, now=i * 0.05)  # light on the right
        brain.step(s, 0.05, now=i * 0.05)
    assert sign * brain.pan > 5


def test_unknown_personality():
    from flybot.controller import personality
    with pytest.raises(ValueError):
        personality("grumpy")


def test_attention_game_records_streak(gains):
    brain = BrainController(ControllerConfig(backend="rate", game=True), gains=gains)
    s = SensorState()
    for i in range(80):  # 2 s centred target, then nothing
        t = i * 0.05
        if i < 40:
            s.update("camera", camera(162, 121, vx=5.0), now=t)
        else:
            s.update("camera", {"detected": False}, now=t)
        cmd = brain.step(s, 0.05, now=t)
    records = [e for e in brain.pop_events() if e["type"] == "record"]
    assert records and records[0]["seconds"] >= 1.5
    assert "best" in cmd["text"] and cmd["game"]["best"] >= 1.5


def test_event_sink_cooldown_and_webhook():
    import http.server
    import json as _json
    import threading
    from flybot.events import EventSink
    received = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(_json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    published = []
    sink = EventSink(lambda topic, body: published.append((topic, body)), "stackchan",
                     f"http://127.0.0.1:{server.server_port}/hook", cooldown_s=10)
    assert sink.emit({"type": "presence"}, now=0.0)
    assert not sink.emit({"type": "presence"}, now=5.0)  # cooling down
    assert sink.emit({"type": "escape"}, now=5.0)  # other types are independent
    for _ in range(150):  # posts run on their own threads, in any order
        if len(received) == 2:
            break
        threading.Event().wait(0.02)
    server.shutdown()
    server.server_close()
    assert [t for t, _ in published] == ["stackchan/event", "stackchan/event"]
    assert sorted(r["type"] for r in received) == ["escape", "presence"]
    assert all(r["robot"] == "stackchan" and "time" not in r for r in received)


# -- LLM cortex (Claude / OpenAI-compatible) against local fake servers ------------

class _FakeLLMServer:
    """Serves scripted JSON responses in order and records requests (path, headers, body)."""

    def __init__(self, responses):
        import http.server
        import json as _json
        import threading
        self.requests, responses = [], list(responses)
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = _json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append((self.path, dict(self.headers), body))
                data = _json.dumps(responses.pop(0)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _look_tool(calls):
    from flybot.llm import Tool
    schema = {"type": "object", "properties": {"pan_deg": {"type": "number"}, "tilt_deg": {"type": "number"}},
              "required": ["pan_deg", "tilt_deg"], "additionalProperties": False}
    return Tool("look_at", "turn head", schema, lambda pan_deg, tilt_deg: calls.append((pan_deg, tilt_deg)) or "ok")


def test_openai_compatible_tool_loop():
    pytest.importorskip("openai")
    from flybot.llm import OpenAICompatLLM
    call = {"id": "call_1", "type": "function",
            "function": {"name": "look_at", "arguments": '{"pan_deg": 30, "tilt_deg": 10}'}}
    usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
    server = _FakeLLMServer([
        {"id": "c1", "object": "chat.completion", "created": 0, "model": "local", "usage": usage,
         "choices": [{"index": 0, "finish_reason": "tool_calls",
                      "message": {"role": "assistant", "content": None, "tool_calls": [call]}}]},
        {"id": "c2", "object": "chat.completion", "created": 0, "model": "local", "usage": usage,
         "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "หันขวาแล้ว"}}]},
    ])
    calls = []
    try:
        llm = OpenAICompatLLM("local", base_url=server.url + "/v1")
        reply = llm.ask("sys", [], "หันขวาหน่อย", tools=[_look_tool(calls)], image_jpeg=b"\xff\xd8fake")
    finally:
        server.close()
    assert reply == "หันขวาแล้ว" and calls == [(30, 10)]
    first, second = server.requests[0][2], server.requests[1][2]
    assert first["messages"][0] == {"role": "system", "content": "sys"}
    assert first["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert first["tools"][0]["function"]["name"] == "look_at"
    assert second["messages"][-1] == {"role": "tool", "tool_call_id": "call_1", "content": "ok"}


def test_anthropic_tool_loop_and_fallback_request():
    pytest.importorskip("anthropic")
    from flybot.llm import AnthropicLLM
    usage = {"input_tokens": 1, "output_tokens": 1}
    base = {"type": "message", "role": "assistant", "model": "claude-opus-5-5", "stop_sequence": None, "usage": usage}
    server = _FakeLLMServer([
        {**base, "id": "m1", "stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "look_at", "input": {"pan_deg": -20, "tilt_deg": 5}}]},
        {**base, "id": "m2", "stop_reason": "end_turn", "content": [{"type": "text", "text": "หันซ้ายแล้ว"}]},
    ])
    calls = []
    try:
        llm = AnthropicLLM(api_key="test", base_url=server.url)
        llm.fallbacks = True  # normally only on the real Claude API; check the request shape here
        reply = llm.ask("sys", [], "หันซ้าย", tools=[_look_tool(calls)])
    finally:
        server.close()
    assert reply == "หันซ้ายแล้ว" and calls == [(-20, 5)]
    (path, headers, first), (_, _, second) = server.requests
    assert path.startswith("/v1/messages")
    assert "server-side-fallback-2026-07-01" in headers.get("anthropic-beta", "")
    assert first["fallbacks"] == "default" and first["model"] == "claude-opus-5-5"
    assert first["output_config"] == {"effort": "low"}
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert first["tools"][0]["input_schema"]["required"] == ["pan_deg", "tilt_deg"]
    result = second["messages"][-1]["content"][0]
    assert result == {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}


class _ScriptedLLM:
    supports_images = True

    def __init__(self, script):
        self.script, self.prompts = script, []

    def ask(self, system, history, text, tools=None, image_jpeg=None, max_steps=4):
        self.prompts.append((text, image_jpeg))
        return self.script(text, {t.name: t for t in tools or []}, image_jpeg)


def _drain(cortex, timeout=2.0):
    import time as _time
    end = _time.monotonic() + timeout
    while _time.monotonic() < end and cortex._jobs.unfinished_tasks:
        _time.sleep(0.01)


def test_cortex_narrates_chats_and_describes(gains):
    import json as _json
    from flybot.cortex import Cortex

    def script(text, tools, image):
        if image:
            return "A coffee mug\nแก้วกาแฟ"
        if "Event:" in text:
            return "Whoa, too close!\nGiant Fiber ยิงเพราะ LPLC2 เห็นของขยายเร็ว"
        tools["look_at"].call({"pan_deg": 40, "tilt_deg": 10})
        tools["set_personality"].call({"name": "skittish"})
        return "หันขวาแล้ว และตอนนี้ขี้ตกใจนะ"

    brain = BrainController(ControllerConfig(backend="rate"), gains=gains)
    published, events = [], []
    cortex = Cortex(_ScriptedLLM(script), brain, lambda t, b: published.append((t, _json.loads(b))),
                    emit_event=events.append, snapshot=lambda timeout: b"\xff\xd8jpeg", narrate_cooldown_s=0)
    s = SensorState()
    cortex.on_event({"type": "escape"}, {"GF": 1.2})
    _drain(cortex)
    assert brain.step(s, 0.05, now=0.0)["text"] == "Whoa, too close!"
    assert published[-1][1]["type"] == "narration" and "LPLC2" in published[-1][1]["text"]

    cortex.on_chat("หันขวาหน่อย")
    _drain(cortex)
    for i in range(60):
        brain.step(s, 0.05, now=0.05 * (i + 1))
    assert brain.pan > 20 and brain.tilt > 5  # PFL3 steered to the requested goal
    assert brain.cfg.gf_threshold == 0.6 and cortex.personality == "skittish"
    assert published[-1] == ("stackchan/chat/out", {"type": "reply", "to": "หันขวาหน่อย",
                                                     "text": "หันขวาแล้ว และตอนนี้ขี้ตกใจนะ"})

    cortex.on_event({"type": "presence"})
    _drain(cortex)
    assert events and events[-1]["type"] == "seen" and events[-1]["description"] == "แก้วกาแฟ"
