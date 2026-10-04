"""Slow LLM layer above the fly circuits: narrates events, chats, looks at snapshots.

The fly brain keeps running the 20 Hz reflexes; this layer never sits in that loop.
Jobs run one at a time on a worker thread and act on the robot only through the
controller's queued commands (``look_at``, ``configure``, ``say``), so a slow or
failing LLM call can delay a reply but never a servo command.

The robot's balloon font has no Thai glyphs, so text for the screen is short
English; Thai replies go to MQTT (``<base>/chat/out``) and events.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Callable

from .controller import PERSONALITIES, BrainController
from .llm import LLM, Tool

log = logging.getLogger(__name__)

PERSONA = """You are the voice of StackChan, a small desk robot whose reflexes come from a
fruit fly's brain wiring (FlyWire connectome): an optic lobe that tracks motion (T4/T5,
HS/VS, LC10), a central complex that remembers where things were (E-PG, PFL3), a mushroom
body that sets the mood with dopamine and octopamine, and a looming detector (LPLC2, LC4)
that fires the Giant Fiber to dodge things rushing at it. You can explain what these
circuits are doing in plain words. The person you talk with speaks Thai: answer in Thai,
briefly (one to three sentences), warm and a little playful. Never claim medical or
therapeutic effects. The robot's screen cannot draw Thai, so text you pass to the `say`
tool must be short English (ASCII, at most 40 characters)."""

NARRATE = """Something just happened to the robot. Write exactly two lines:
line 1: what the robot would say about it, short English, at most 30 ASCII characters;
line 2: one Thai sentence explaining which fly circuit caused this and why.
Event: {event}
Brain state: {brain}"""

DESCRIBE = """This is a 160x120 grayscale frame from the robot's camera, taken because something
new moved in front of it. Write exactly two lines: line 1 a short English description
(at most 30 ASCII characters) of the main thing in view; line 2 the same in Thai. Do not
try to identify people; describe them only as "a person"."""


class Cortex:
    def __init__(self, llm: LLM, controller: BrainController, publish: Callable[[str, str], None],
                 base_topic: str = "stackchan", emit_event: Callable[[dict], None] | None = None,
                 snapshot: Callable[[float], bytes | None] | None = None, narrate: bool = True,
                 narrate_cooldown_s: float = 20.0, history_turns: int = 6):
        self.llm = llm
        self.controller = controller
        self.publish = publish
        self.base = base_topic.rstrip("/")
        self.emit_event = emit_event
        self.snapshot = snapshot if snapshot and llm.supports_images else None
        self.narrate = narrate
        self.narrate_cooldown_s = narrate_cooldown_s
        self.history: list[dict] = []
        self.history_turns = history_turns
        self.personality = "curious"
        self.event_counts: dict[str, int] = {}
        self._last_narration = -float("inf")
        self._jobs: queue.Queue = queue.Queue(maxsize=8)
        self._thread = threading.Thread(target=self._run, name="cortex", daemon=True)
        self._thread.start()

    # -- inputs (called from the bridge thread; never block) --------------------
    def on_event(self, event: dict, brain: dict | None = None) -> None:
        kind = event.get("type", "event")
        self.event_counts[kind] = self.event_counts.get(kind, 0) + 1
        now = time.monotonic()
        if self.narrate and now - self._last_narration >= self.narrate_cooldown_s:
            self._last_narration = now
            self._submit(self._narrate, event, brain or {})
        if self.snapshot and kind == "presence":
            self._submit(self._describe)

    def on_chat(self, text: str) -> None:
        self._submit(self._chat, text)

    def _submit(self, fn, *args) -> None:
        try:
            self._jobs.put_nowait((fn, args))
        except queue.Full:
            log.warning("cortex busy, dropping %s", fn.__name__)

    def _run(self) -> None:
        while True:
            fn, args = self._jobs.get()
            try:
                fn(*args)
            except Exception:  # an LLM/network failure must not kill the worker
                log.exception("cortex job %s failed", fn.__name__)
            finally:
                self._jobs.task_done()

    # -- jobs ---------------------------------------------------------------------
    @staticmethod
    def _two_lines(reply: str) -> tuple[str, str]:
        lines = [l.strip() for l in reply.splitlines() if l.strip()]
        if not lines:
            return "", ""
        screen = lines[0] if lines[0].isascii() else ""
        thai = lines[1] if len(lines) > 1 else ("" if screen else lines[0])
        return screen[:40], thai

    def _narrate(self, event: dict, brain: dict) -> None:
        reply = self.llm.ask(PERSONA, [], NARRATE.format(event=json.dumps(event, ensure_ascii=False),
                                                         brain=json.dumps(brain)))
        screen, thai = self._two_lines(reply)
        if screen:
            self.controller.say(screen)
        if thai:
            self._out({"type": "narration", "event": event.get("type"), "text": thai, "screen": screen})

    def _describe(self) -> str:
        jpeg = self.snapshot(2.0) if self.snapshot else None
        if not jpeg:
            return json.dumps({"error": "no camera frame"})
        screen, thai = self._two_lines(self.llm.ask(PERSONA, [], DESCRIBE, image_jpeg=jpeg))
        if screen:
            self.controller.say(screen)
        if self.emit_event and (screen or thai):
            self.emit_event({"type": "seen", "description": thai or screen, "screen": screen})
        return json.dumps({"seen": thai or screen}, ensure_ascii=False)

    def _chat(self, text: str) -> None:
        reply = self.llm.ask(PERSONA, self.history, text, tools=self.tools())
        self.history += [{"role": "user", "content": text}, {"role": "assistant", "content": reply or "..."}]
        self.history = self.history[-2 * self.history_turns:]
        self._out({"type": "reply", "to": text, "text": reply})

    def _out(self, payload: dict) -> None:
        self.publish(f"{self.base}/chat/out", json.dumps(payload, ensure_ascii=False))

    # -- tools the LLM may call in a chat -------------------------------------------
    def tools(self) -> list[Tool]:
        c = self.controller
        tools = [
            Tool("look_at", "Turn the robot's head. pan_deg: -90 (left) .. 90 (right); tilt_deg: 0 .. 30 (up).",
                 {"type": "object", "properties": {"pan_deg": {"type": "number"}, "tilt_deg": {"type": "number"}},
                  "required": ["pan_deg", "tilt_deg"], "additionalProperties": False},
                 lambda pan_deg, tilt_deg: c.look_at(pan_deg, tilt_deg) or "turning"),
            Tool("set_personality", "Change the robot's temperament: " + ", ".join(sorted(PERSONALITIES)) + ".",
                 {"type": "object", "properties": {"name": {"type": "string", "enum": sorted(PERSONALITIES)}},
                  "required": ["name"], "additionalProperties": False},
                 self._set_personality),
            Tool("set_game", "Start (true) or stop (false) the attention game: the robot times how long "
                             "it keeps a slowly moving object centred.",
                 {"type": "object", "properties": {"on": {"type": "boolean"}},
                  "required": ["on"], "additionalProperties": False},
                 lambda on: c.configure(game=bool(on)) or f"game {'on' if on else 'off'}"),
            Tool("brain_state", "Current expression, head pose, fly-circuit activity and event counts.",
                 {"type": "object", "properties": {}, "additionalProperties": False},
                 lambda: {**c.snapshot, "personality": self.personality, "events_so_far": self.event_counts}),
            Tool("say", "Show short English text (ASCII, <= 40 chars) in the robot's speech balloon.",
                 {"type": "object", "properties": {"text": {"type": "string"}},
                  "required": ["text"], "additionalProperties": False},
                 lambda text: c.say(text[:40]) or "shown"),
        ]
        if self.snapshot:
            tools.append(Tool("look_and_describe", "Take a camera frame and describe what is in front of the robot.",
                              {"type": "object", "properties": {}, "additionalProperties": False},
                              self._describe))
        return tools

    def _set_personality(self, name: str) -> str:
        if name not in PERSONALITIES:
            return json.dumps({"error": f"unknown personality {name}"})
        # every field any preset touches: this preset's value or the plain default
        default = type(self.controller.cfg)()
        changes = {k: PERSONALITIES[name].get(k, getattr(default, k))
                   for k in {k for preset in PERSONALITIES.values() for k in preset}}
        self.controller.configure(**changes)
        self.personality = name
        return f"personality {name}"
