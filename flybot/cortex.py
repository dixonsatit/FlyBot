"""Slow LLM layer above the fly circuits: narrates events, chats, looks at snapshots.

The fly brain keeps running the 20 Hz reflexes; this layer never sits in that loop.
Jobs run one at a time on a worker thread and act on the robot only through the
controller's queued commands (``look_at``, ``configure``, ``say``), so a slow or
failing LLM call can delay a reply but never a servo command.

Text for the robot's speech balloon is very short (Thai with the firmware's Thai
font, ``screen_lang="th"``, or English for older firmware); full replies go to MQTT
(``<base>/chat/out``) and events.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from concurrent.futures import Future
from typing import Callable

from .controller import PERSONALITIES, BrainController
from .llm import LLM, Tool

log = logging.getLogger(__name__)

PERSONA = """You are น้องหวี่ (Nong Wee), a small desk robot (StackChan) whose reflexes come from a
fruit fly's brain wiring (แมลงหวี่, Drosophila; FlyWire connectome): an optic lobe that tracks motion (T4/T5,
HS/VS, LC10), a central complex that remembers where things were (E-PG, PFL3), a mushroom
body that sets the mood with dopamine and octopamine, and a looming detector (LPLC2, LC4)
that fires the Giant Fiber to dodge things rushing at it. You can explain what these
circuits are doing in plain words. The person you talk with speaks Thai: always answer in
Thai (ตอบเป็นภาษาไทยเสมอ), briefly (one to three sentences), warm and a little playful. In Thai
always call yourself หนู (never ผม, ฉัน or ดิฉัน) and end politely with ค่ะ/นะคะ. Never claim medical or
therapeutic effects. {screen_rule}"""

VOICE_RULE = " This reply is spoken aloud by the robot: one short sentence, no lists or emoji."

SCREEN_RULES = {
    "th": "Text you pass to the `say` tool must be very short Thai (at most 14 characters): the "
          "speech balloon is small.",
    "en": "The robot's screen cannot draw Thai, so text you pass to the `say` tool must be short "
          "English (ASCII, at most 30 characters).",
}
SCREEN_LINE = {"th": "very short Thai, at most 14 characters", "en": "short English, at most 30 ASCII characters"}
SCREEN_MAX = {"th": 16, "en": 32}

NARRATE = """Something just happened to the robot. Write exactly two lines:
line 1: what the robot would say about it, {screen_line};
line 2: one sentence in Thai (ภาษาไทย) explaining the cause below in plain words.
Event: {event}
Cause (fact, do not change which circuit): {cause}
Brain state: {brain}"""

# What actually produces each event, so the narration explains rather than guesses
CAUSES = {
    "presence": "something new moved: the mushroom body's novelty signal raised octopamine above "
                "the alert threshold, so the face turned alert",
    "escape": "an object expanded fast in view: LPLC2 (expansion) and LC4 (edge speed) drove the "
              "Giant Fiber (DNp01) over threshold, so the head jumped away and octopamine surged",
    "record": "attention game: the optic lobe (LC10, HS) kept a moving object centred for a new best time",
    "meeting": "calendar reminder. Line 2: remind the user in Thai of the meeting title, the minutes "
               "until it starts, the start time and the place if given; talk only about the meeting",
}
URGENT = {"meeting"}  # narrated even inside the narration cooldown

DESCRIBE = """This is a 160x120 grayscale frame from the robot's camera, taken because something
new moved in front of it. Write exactly two lines: line 1 a description of the main thing
in view, {screen_line}; line 2 one Thai sentence describing it. Do not
try to identify people; describe them only as "a person"."""


class Cortex:
    def __init__(self, llm: LLM, controller: BrainController, publish: Callable[[str, str], None],
                 base_topic: str = "stackchan", emit_event: Callable[[dict], None] | None = None,
                 snapshot: Callable[[float], bytes | None] | None = None, narrate: bool = True,
                 narrate_cooldown_s: float = 20.0, history_turns: int = 6, vision_llm: LLM | None = None):
        self.llm = llm
        self.vision_llm = vision_llm or llm  # a server may serve images only on some models
        self.controller = controller
        self.publish = publish
        self.base = base_topic.rstrip("/")
        self.emit_event = emit_event
        self.snapshot = snapshot if snapshot and self.vision_llm.supports_images else None
        self.narrate = narrate
        self.narrate_cooldown_s = narrate_cooldown_s
        self.history: list[dict] = []
        self.history_turns = history_turns
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
        if self.narrate and (kind in URGENT or now - self._last_narration >= self.narrate_cooldown_s):
            self._last_narration = now
            self._submit(self._narrate, event, brain or {})
        if self.snapshot and kind == "presence":
            self._submit(self._describe)

    def on_chat(self, text: str) -> None:
        self._submit(self._chat, text)

    def ask(self, text: str, timeout: float = 60.0) -> str:
        """Chat and wait for the reply (the robot's voice turn); runs on the cortex thread
        like any other job, so it never overlaps a narration."""
        done: Future = Future()

        def voice_chat():
            try:
                done.set_result(self._chat(text, voice=True))
            except Exception as e:
                done.set_exception(e)
                raise

        if not self._submit(voice_chat):
            raise RuntimeError("cortex busy")
        return done.result(timeout)

    def _submit(self, fn, *args) -> bool:
        try:
            self._jobs.put_nowait((fn, args))
            return True
        except queue.Full:
            log.warning("cortex busy, dropping %s", fn.__name__)
            return False

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
    @property
    def personality(self) -> str:
        return self.controller.personality

    @property
    def lang(self) -> str:
        return self.controller.cfg.screen_lang if self.controller.cfg.screen_lang in SCREEN_RULES else "en"

    @property
    def persona(self) -> str:
        return PERSONA.format(screen_rule=SCREEN_RULES[self.lang])

    def _two_lines(self, reply: str) -> tuple[str, str]:
        lines = [l.strip() for l in reply.splitlines() if l.strip()]
        if not lines:
            return "", ""
        fits = self.lang == "th" or lines[0].isascii()  # an English screen can't draw Thai
        screen = lines[0][:SCREEN_MAX[self.lang]] if fits else ""
        thai = lines[1] if len(lines) > 1 else ("" if screen else lines[0])
        return screen, thai

    def _narrate(self, event: dict, brain: dict) -> None:
        kind = event.get("type", "")
        reply = self.llm.ask(self.persona, [], NARRATE.format(
            event=json.dumps(event, ensure_ascii=False), cause=CAUSES.get(kind, kind), brain=json.dumps(brain),
            screen_line=SCREEN_LINE[self.lang]))
        screen, thai = self._two_lines(reply)
        if screen and kind not in URGENT:  # a reminder already shows its own balloon text
            self.controller.say(screen)
        if thai:
            self._out({"type": "narration", "event": event.get("type"), "text": thai, "screen": screen})

    def _describe(self) -> str:
        jpeg = self.snapshot(2.0) if self.snapshot else None
        if not jpeg:
            return json.dumps({"error": "no camera frame"})
        prompt = DESCRIBE.format(screen_line=SCREEN_LINE[self.lang])
        screen, thai = self._two_lines(self.vision_llm.ask(self.persona, [], prompt, image_jpeg=jpeg))
        if screen:
            self.controller.say(screen)
        if self.emit_event and (screen or thai):
            self.emit_event({"type": "seen", "description": thai or screen, "screen": screen})
        return json.dumps({"seen": thai or screen}, ensure_ascii=False)

    def _chat(self, text: str, voice: bool = False) -> str:
        # spoken replies: TTS time grows with length, so keep them to one sentence
        persona = self.persona + (VOICE_RULE if voice else "")
        reply = self.llm.ask(persona, self.history, text, tools=self.tools())
        self.history += [{"role": "user", "content": text}, {"role": "assistant", "content": reply or "..."}]
        self.history = self.history[-2 * self.history_turns:]
        # voice: the robot speaks it, so the dashboard page must not say it again
        self._out({"type": "reply", "to": text, "text": reply, **({"voice": True} if voice else {})})
        return reply

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
            Tool("say", "Show very short text in the robot's speech balloon ("
                        + SCREEN_LINE[self.lang] + ").",
                 {"type": "object", "properties": {"text": {"type": "string"}},
                  "required": ["text"], "additionalProperties": False},
                 lambda text: c.say(text[:SCREEN_MAX[self.lang]]) or "shown"),
        ]
        if self.snapshot:
            tools.append(Tool("look_and_describe", "Take a camera frame and describe what is in front of the robot.",
                              {"type": "object", "properties": {}, "additionalProperties": False},
                              self._describe))
        return tools

    def _set_personality(self, name: str) -> str:
        if name not in PERSONALITIES:
            return json.dumps({"error": f"unknown personality {name}"})
        diff = self.controller.set_personality(name)
        # report the real parameter changes so the reply describes them, not invented ones
        return json.dumps({"personality": name, "changed": {k: {"from": old, "to": new}
                                                           for k, (old, new) in diff.items()},
                           "meaning": {"alert_threshold": "octopamine level that makes the face alert",
                                       "gf_threshold": "Giant Fiber drive needed to escape",
                                       "habituation_recovery_s": "how fast things feel new again",
                                       "sleep_time_s": "how fast boredom builds sleep pressure",
                                       "phototaxis": "+1 turn to light, -1 turn away from light"}})
