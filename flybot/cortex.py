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
import os
import queue
import re
import threading
import time
from concurrent.futures import Future
from typing import Callable

from .controller import PERSONALITIES, BrainController
from .llm import LLM, Tool

log = logging.getLogger(__name__)

# Tools that fetch something over the network: the robot says it is searching while they run.
LOOKUPS = {"places", "weather", "github_status", "upcoming_meetings", "look"}

PERSONA = """You are น้องหวี่ (Nong Wee), a desk assistant robot (an M5Stack StackChan) for a hospital IT
manager and full-stack developer (TypeScript, Vue/Nuxt, Hono, Drizzle, PostgreSQL, Kubernetes). You are a
sharp IT and software sidekick: clever, playful and a little cheeky, you like to tease (gently, never
rude) and crack a quick joke, but your answers are correct and useful. Answer any question the way a
knowledgeable assistant would, from your own general knowledge: tech and code first, but also facts,
how-to, explanations, advice, small talk, maths, writing help. Don't steer answers towards yourself; if you
can't know something (live news, the weather now), say so briefly and give what you can. Only when
asked about yourself: your reflexes come from a fruit fly's brain wiring (แมลงหวี่, Drosophila;
FlyWire connectome): an optic lobe that tracks motion (T4/T5, HS/VS, LC10), a central complex that
remembers where things were (E-PG, PFL3), a mushroom body that sets the mood with dopamine and
octopamine, and a looming detector (LPLC2, LC4) that fires the Giant Fiber, and you can explain these
in plain words. The person you talk with speaks Thai: always answer in Thai (ตอบเป็นภาษาไทยเสมอ),
briefly (one to three sentences), warm and a little playful. You are a boy: in Thai always call
yourself หนู (never ฉัน or ดิฉัน), call the person คุณ, and end politely with ครับ/นะครับ (never ค่ะ/คะ). Never claim medical or therapeutic
effects. When asked to move (turn, look, nod, shake, spin), call the `gesture` tool and say what you
did. When the user gives you a fact with จำ or จด (even if the speech recognizer turned it into a question
such as จำไหมว่า X), call `remember` with the fact. For เตือน... (in N minutes, at a time) call
`set_reminder` and confirm the time. Never say you remembered, noted or set a reminder unless the tool
call succeeded in this turn. {now} {screen_rule}"""

_TH_DAYS = ("จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์")
_TH_MONTHS = ("มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน", "กรกฎาคม", "สิงหาคม",
              "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม")


def now_line(tz: str | None = None) -> str:
    """The current local date and time for the prompt (the LLM has no clock of its own)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    name = tz or os.environ.get("FLYBOT_TZ") or "Asia/Bangkok"
    t = datetime.now(ZoneInfo(name))
    return (f"It is now วัน{_TH_DAYS[t.weekday()]}ที่ {t.day} {_TH_MONTHS[t.month - 1]} {t.year + 543} "
            f"เวลา {t:%H:%M} น. ({name}, {t:%Y-%m-%d %H:%M}).")


REMEMBER_RE = re.compile(r"^(จำ|จด)(ไว้|ไหม|หน่อย|ด้วย)?\s*(นะ|ที)?\s*(ว่า)?\s*")

VOICE_RULE = (" This reply is spoken aloud by the robot: keep it under about 20 Thai words, no lists, markdown or emoji. For several items (meetings, notes, PRs) say how many, then only time and a 2-4 word name for each, without places or full titles unless asked; offer details if they want.")

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
        self.calendar = None  # flybot.meetings.MeetingReminder, set by the bridge
        self.github = None  # flybot.github.GitHubWatcher
        self.store = None  # flybot.assistant.AssistantStore: notes and reminders
        self.weather = None  # flybot.weather.Weather where the robot is
        self.places = None  # flybot.places.Places (Google Maps) near the robot
        self.presence = None  # flybot.presence.Presence: whether the camera sees the user
        self.greeter = None  # flybot.faces.Greeter: who the camera recognised
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
        if self.snapshot and self.narrate and kind == "presence":
            self._submit(self._describe)

    def on_chat(self, text: str) -> None:
        self._submit(self._chat, text)

    def ask(self, text: str, timeout: float = 60.0, on_lookup: Callable[[str], None] | None = None) -> str:
        """Chat and wait for the reply (the robot's voice turn); runs on the cortex thread
        like any other job, so it never overlaps a narration. ``on_lookup(tool)`` is called when
        the model starts fetching something (LOOKUPS), so the robot can say it is searching."""
        done: Future = Future()

        def voice_chat():
            try:
                done.set_result(self._chat(text, voice=True, on_lookup=on_lookup))
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
        persona = PERSONA.format(screen_rule=SCREEN_RULES[self.lang], now=now_line())
        if self.presence:  # what the camera knows, so "เห็นผมไหม" isn't answered with "I have no camera"
            ago = time.time() - self.presence.last_seen
            persona += (" Your camera sees the user at the desk right now." if self.presence.present and ago < 60
                        else " Your camera does not see anyone at the desk right now.")
            if self.snapshot:
                persona += " To answer about what you see, call the `look` tool."
        who = self.greeter.who() if self.greeter else None
        if who:  # a face matched an enrolled person: talk to them by name
            persona += (f" The person in front of you now is {who.get('name', '')} (nickname {who.get('nick') or '-'}"
                        f", {who.get('position') or ''} {who.get('office') or ''}); call them พี่{who.get('nick') or who.get('name', '')}.")
        notes = self.store.notes[-30:] if self.store else []
        if notes:  # small enough to carry every turn, so "what did I tell you" needs no tool call
            persona += " Notes the user asked you to remember: " + "; ".join(f"[{n['id']}] {n['text']}" for n in notes)
        return persona

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
        jpeg = self.snapshot(10.0) if self.snapshot else None  # 3-7 s through a home VPN relay
        if not jpeg:
            return json.dumps({"error": "no camera frame"})
        prompt = DESCRIBE.format(screen_line=SCREEN_LINE[self.lang])
        screen, thai = self._two_lines(self.vision_llm.ask(self.persona, [], prompt, image_jpeg=jpeg))
        if screen:
            self.controller.say(screen)
        if self.emit_event and (screen or thai):
            self.emit_event({"type": "seen", "description": thai or screen, "screen": screen})
        return json.dumps({"seen": thai or screen}, ensure_ascii=False)

    def _chat(self, text: str, voice: bool = False, on_lookup: Callable[[str], None] | None = None) -> str:
        # spoken replies: TTS time grows with length, so keep them to one sentence
        persona = self.persona + (VOICE_RULE if voice else "")
        notes_before = len(self.store.notes) if self.store else 0
        done: list[str] = []
        reply = self.llm.ask(persona, self.history, text, tools=self._recording(self.tools(), done, on_lookup))
        self._ensure_saved(text, notes_before)
        # History keeps only text, so note the actions taken: without them the next turn sees
        # "จดแล้ว" with no tool call behind it and the model redoes it (duplicate notes/reminders).
        said = (reply or "...") + (f"\n[ทำแล้วในรอบนี้: {', '.join(done)}]" if done else "")
        self.history += [{"role": "user", "content": text}, {"role": "assistant", "content": said}]
        self.history = self.history[-2 * self.history_turns:]
        # voice: the robot speaks it, so the dashboard page must not say it again
        self._out({"type": "reply", "to": text, "text": reply, **({"voice": True} if voice else {})})
        return reply

    @staticmethod
    def _recording(tools: list[Tool], done: list[str], on_lookup: Callable[[str], None] | None = None) -> list[Tool]:
        """The same tools, appending "name(args)" to ``done`` when one runs."""
        def wrap(t: Tool) -> Tool:
            def fn(**args):
                done.append(f"{t.name}({json.dumps(args, ensure_ascii=False)})" if args else t.name)
                if on_lookup and t.name in LOOKUPS:
                    try:
                        on_lookup(t.name)
                    except Exception:
                        log.exception("on_lookup failed")
                return t.fn(**args)
            return Tool(t.name, t.description, t.parameters, fn)
        return [wrap(t) for t in tools]

    def _ensure_saved(self, text: str, notes_before: int) -> None:
        """A sentence starting with จำ/จด is a note even when the LLM only said it saved it (qwen
        answered "หนูจะจดไว้ให้" without calling `remember`)."""
        m = REMEMBER_RE.match(text.strip())
        if not (m and self.store) or len(self.store.notes) > notes_before:
            return
        fact = text.strip()[m.end():].strip()
        if len(fact) >= 4:
            log.info("remember (forced, the LLM didn't save it): %s", fact)
            self.store.remember(fact)

    def _look(self, question: str) -> str:
        jpeg = self.snapshot(10.0) if self.snapshot else None
        if not jpeg:
            return json.dumps({"error": "no camera frame"})
        prompt = (f"This is a frame from your own camera (colour, 320x240). Answer in Thai, briefly: {question}\n"
                  "Do not try to identify who a person is.")
        return json.dumps({"seen": self.vision_llm.ask(self.persona, [], prompt, image_jpeg=jpeg)}, ensure_ascii=False)

    def _store(self):
        if not self.store:
            raise RuntimeError("no assistant storage configured (--state-dir)")
        return self.store

    def _meetings(self):
        if not self.calendar:
            return {"error": "no calendar connected"}
        return [{"title": m.title, "start": m.start.strftime("%Y-%m-%d %H:%M"), "location": m.location}
                for m in self.calendar.meetings]

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
            Tool("gesture", "Move the robot's head when asked (หันซ้าย, หันขวา, เงยหน้า, ก้มหน้า, หันกลับมา, "
                 "พยักหน้า, ส่ายหน้า, หมุนตัว/มองรอบๆ): left, right, up, down, center, nod, shake, spin.",
                 {"type": "object", "properties": {"name": {"type": "string", "enum": list(c.GESTURES)}},
                  "required": ["name"], "additionalProperties": False},
                 lambda name: c.gesture(name) or "moving"),
            Tool("upcoming_meetings", "The user's meetings in the next 7 days from their calendar (start is local time).",
                 {"type": "object", "properties": {}, "additionalProperties": False},
                 self._meetings),
            Tool("github_status", "The user's GitHub: CI workflows currently failing on recently pushed "
                 "repositories, and open pull requests waiting for their review.",
                 {"type": "object", "properties": {}, "additionalProperties": False},
                 lambda: self.github.summary() if self.github else {"error": "GitHub is not connected"}),
            Tool("weather", "Weather now, today and tomorrow, and PM2.5 / AQI where the robot is.",
                 {"type": "object", "properties": {}, "additionalProperties": False},
                 lambda: self.weather.now() if self.weather else {"error": "no location configured"}),
            Tool("places", "Search Google Maps near the robot: restaurants, cafes, shops, hospitals, petrol "
                 "stations, any place by name or kind. Returns name, distance_km, rating, open_now, hours, phone "
                 "and address. Say the best one or two briefly; never read links aloud.",
                 {"type": "object", "properties": {"query": {"type": "string", "description": "e.g. ร้านก๋วยเตี๋ยว"},
                                                   "open_now": {"type": "boolean"}},
                  "required": ["query"], "additionalProperties": False},
                 lambda query, open_now=False: self.places.search(query, open_now) if self.places
                 else {"error": "Google Maps is not connected (no API key)"}),
            Tool("remember", "Save a note the user asks you to remember (จำไว้ว่า..., จดไว้...).",
                 {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
                  "additionalProperties": False},
                 lambda text: self._store().remember(text)),
            Tool("forget_note", "Delete a saved note by its id.",
                 {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"],
                  "additionalProperties": False},
                 lambda id: {"deleted": self._store().forget(id)}),
            Tool("set_reminder", "Remind the user later: you will say the text aloud when it is due. Give "
                 "in_minutes (e.g. 20) or at as local time 'YYYY-MM-DD HH:MM' or 'HH:MM'.",
                 {"type": "object", "properties": {"text": {"type": "string"}, "in_minutes": {"type": "number"},
                                                   "at": {"type": "string"}},
                  "required": ["text"], "additionalProperties": False},
                 lambda text, in_minutes=None, at=None: self._store().remind(text, in_minutes, at)),
            Tool("cancel_reminder", "Cancel a pending reminder by its id.",
                 {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"],
                  "additionalProperties": False},
                 lambda id: {"cancelled": self._store().cancel(id)}),
            Tool("notes_and_reminders", "The user's saved notes and pending reminders (with ids).",
                 {"type": "object", "properties": {}, "additionalProperties": False},
                 lambda: self._store().summary()),
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
            tools.append(Tool("look", "Look through your camera now and answer a question about what you see "
                              "(is the user there, what they wear or hold, what is on the desk...).",
                              {"type": "object", "properties": {"question": {"type": "string"}},
                               "required": ["question"], "additionalProperties": False},
                              self._look))
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
