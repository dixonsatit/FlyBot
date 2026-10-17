"""Head commands in Thai, parsed and played without the LLM.

"หันซ้ายนิดนึง", "เงย 20 องศา", "มองมุมขวาบน", "หันซ้ายแล้วหันขวา", "อีก", "กลับที่เดิม",
"ค้างไว้", "ช้าๆ", "หยุด". The LLM took seconds, sometimes said it moved without moving, and
the ASR turns long sentences into something else; a command says exactly what the head does.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

NUDGE, SOME, MUCH = 8.0, 15.0, 30.0  # นิดนึง, (หน่อย / no amount), มาก
DEFAULT_HOLD_S = 30.0  # then face tracking may move the head again
HOLD_ON_S = 3600.0  # "ค้างไว้"

# direction words, with what the ASR has written for them
_DIRS = [
    ("left", r"ซ้าย|ซ้ายมือ"),
    ("right", r"ขวา|ขวามือ"),
    ("up", r"เงย|แหงน|ขึ้น|บน"),
    ("down", r"ก้ม|ลง|ล่าง"),
]
_DIR_RE = re.compile("|".join(f"(?P<{n}>{p})" for n, p in _DIRS))
_GESTURES = [
    ("nod", re.compile(r"พยัก")),
    ("shake", re.compile(r"ส่าย")),
    ("spin", re.compile(r"หมุน|มองรอบ|ดูรอบ")),
]
_HOME_RE = re.compile(r"(หัน|มอง|หน้า)\s*(กลับ\s*)?(ตรง|กลาง)|หัน\s*(หน้า\s*)?กลับ|กลับ\s*(มา\s*)?(ตรง|ท่าเดิม)|หัน\s*(หน้า\s*)?มา\s*(ทาง\s*)?(นี้|นี่|ผม|หนู|ฉัน)"
                      r"|มอง\s*(ทาง\s*)?(ผม|ฉัน|หนู)|^\s*กลับ\s*มา")
_BACK_RE = re.compile(r"ที่เดิม|ท่าก่อน|อย่างเดิม")
_AGAIN_RE = re.compile(r"^\s*(อีก|เพิ่ม)")
_HOLD_RE = re.compile(r"ค้าง|อยู่\s*(ตรง)?\s*(นั้น|นี้)|นิ่ง\s*ๆ?|ไม่\s*ต้อง\s*ขยับ")
_SLOW_RE = re.compile(r"ช้า\s*ๆ|ค่อย\s*ๆ")
_STOP_RE = re.compile(r"^\s*(หยุด|พอ|เสร็จ|เลิก)")
_NAME_RE = re.compile(r"^\s*(น้อง\s*)?(หวี่|หวี|วี่|วี|อี้|อุ๋ย)?\s*(?=\S)")  # "น้องหวี่ หยุด" -> "หยุด"
_SEQ_RE = re.compile(r"แล้ว\s*(ก็\s*)?|ต่อ\s*ด้วย|จากนั้น")
_CORNER_RE = re.compile(r"มุม")
_DEG_RE = re.compile(r"(\d{1,3})\s*(องศา|°)?")
_NUM_WORDS = {"หนึ่ง": 1, "เอ็ด": 1, "สอง": 2, "ยี่": 2, "สาม": 3, "สี่": 4, "ห้า": 5, "หก": 6, "เจ็ด": 7, "แปด": 8,
              "เก้า": 9}
_THAI_NUM_RE = re.compile(r"((?:หนึ่ง|ยี่|สอง|สาม|สี่|ห้า|หก|เจ็ด|แปด|เก้า)?\s*สิบ\s*(?:เอ็ด|หนึ่ง|สอง|สาม|สี่|ห้า|หก|เจ็ด|แปด|เก้า)?"
                          r"|หนึ่ง|สอง|สาม|สี่|ห้า|หก|เจ็ด|แปด|เก้า)\s*องศา")
_AMOUNTS = [
    ("max", re.compile(r"สุด")),
    ("nudge", re.compile(r"นิด|เล็ก\s*น้อย|หน่อย\s*นึง")),
    ("much", re.compile(r"มาก|เยอะ|เต็ม")),
]
# what is left of a pure head command once the commands are taken out
_FILLER_RE = re.compile(r"น้อง|หวี่|หวี|วี่|ครับ|คับ|ค่ะ|คะ|นะ|หน่อย|ให้|ดู|สิ|ซิ|ที|ด้วย|ไป|มา|ทาง|ด้าน|หัน|หน้า|มอง|ตัว|"
                        r"ขยับ|หัว|ลอง|อีก|ที่|ซะ|เลย|จ้า|จ๊ะ|ไหม|ได้|ผม|ฉัน|หนู|แล้ว|ก็|องศา|กลับ|ไว้|\d|\s|[.,!?]")
PURE_MAX_LEFT = 6  # characters of anything else: then the sentence is more than a head command


@dataclass
class Move:
    kind: str  # "by" | "home" | "back" | "gesture" | "again" | "stop"
    pan: float = 0.0  # degrees, + = the robot's right
    tilt: float = 0.0  # + = up
    amount: str = ""  # "max": to the limit in that direction
    gesture: str = ""


@dataclass
class Plan:
    moves: list[Move] = field(default_factory=list)
    hold: bool = False
    slow: bool = False
    pure: bool = False  # nothing but head commands in the sentence

    def __bool__(self) -> bool:
        return bool(self.moves)


def _thai_number(s: str) -> float | None:
    s = s.replace(" ", "").replace("องศา", "")
    if "สิบ" in s:
        tens, _, ones = s.partition("สิบ")
        return float(_NUM_WORDS.get(tens, 1) * 10 + _NUM_WORDS.get(ones, 0))
    return float(_NUM_WORDS[s]) if s in _NUM_WORDS else None


def _amount(chunk: str) -> tuple[float, str]:
    m = _DEG_RE.search(chunk)
    if m and int(m.group(1)) <= 180:
        return float(m.group(1)), ""
    m = _THAI_NUM_RE.search(chunk)
    if m and (n := _thai_number(m.group(1))) is not None:
        return n, ""
    for name, rx in _AMOUNTS:
        if rx.search(chunk):
            return {"max": 180.0, "nudge": NUDGE, "much": MUCH}[name], name
    return SOME, ""


def _step(direction: str, deg: float) -> tuple[float, float]:
    return {"left": (-deg, 0.0), "right": (deg, 0.0), "up": (0.0, deg), "down": (0.0, -deg)}[direction]


def parse(text: str) -> Plan:
    """The head moves a sentence asks for, in order (empty Plan when it asks for none)."""
    text = _NAME_RE.sub("", text)
    plan = Plan(hold=bool(_HOLD_RE.search(text)), slow=bool(_SLOW_RE.search(text)))
    if _STOP_RE.search(text):
        plan.moves.append(Move("stop"))
        plan.pure = len(_FILLER_RE.sub("", _STOP_RE.sub("", text))) <= PURE_MAX_LEFT
        return plan
    used: list[tuple[int, int]] = []
    for part in _SEQ_RE.split(text):
        if not part or _SEQ_RE.fullmatch(part):
            continue
        corner = bool(_CORNER_RE.search(part))
        found = [(m.lastgroup, m) for m in _DIR_RE.finditer(part)]
        g = next(((name, rx) for name, rx in _GESTURES if rx.search(part)), None)
        if g:
            plan.moves.append(Move("gesture", gesture=g[0]))
            continue
        if _BACK_RE.search(part):
            plan.moves.append(Move("back"))
            continue
        if _HOME_RE.search(part) and not found:
            plan.moves.append(Move("home"))
            continue
        if not found:
            if _AGAIN_RE.search(part):  # "อีก", "อีกนิด": the last direction again
                deg, amount = _amount(part)
                plan.moves.append(Move("again", pan=deg, amount=amount))
            continue
        # "ก้มลง", "เงยขึ้น", "ขึ้นบน": one direction said twice is one move
        merged = []
        for direction, m in found:
            if merged and merged[-1][0] == direction and len(part[merged[-1][1].end():m.start()].strip()) <= 3:
                continue
            merged.append((direction, m))
        found = merged
        moves = []
        for i, (direction, m) in enumerate(found):
            end = found[i + 1][1].start() if i + 1 < len(found) else len(part)
            deg, amount = _amount(part[m.end():end])
            pan, tilt = _step(direction, deg)
            moves.append(Move("by", pan=pan, tilt=tilt, amount=amount))
        if corner or (len(moves) == 2 and (moves[0].pan == 0) != (moves[1].pan == 0)):
            # "มุมขวาบน", "ขวาบน": both axes in one move
            moves = [Move("by", pan=sum(x.pan for x in moves), tilt=sum(x.tilt for x in moves),
                          amount="max" if any(x.amount == "max" for x in moves) else "")]
        plan.moves += moves
    if plan.moves:
        left = text
        for rx in (_HOME_RE, _BACK_RE, _THAI_NUM_RE, _HOLD_RE, _SLOW_RE, _AGAIN_RE, *[r for _, r in _GESTURES],
                   *[r for _, r in _AMOUNTS], _DIR_RE, _CORNER_RE, _SEQ_RE, _FILLER_RE):
            left = rx.sub("", left)
        plan.pure = len(left) <= PURE_MAX_LEFT
    elif plan.hold:  # "ค้างไว้" alone: keep the head where it is
        plan.moves.append(Move("by"))
        plan.pure = True
    return plan


class HeadDriver:
    """Plays a Plan on the controller: one move at a time, face tracking paused meanwhile."""

    GESTURE_S = {"nod": 2.2, "shake": 2.2, "spin": 4.4}

    def __init__(self, controller, tracker=None, say=None):
        self.c, self.tracker, self.say = controller, tracker, say
        self.previous: tuple[float, float] | None = None
        self.last_dir: tuple[float, float] = (0.0, 0.0)  # unit step of the last relative move
        self._run = 0  # a new plan (or "หยุด") cancels the one playing
        self._lock = threading.Lock()

    def limits(self) -> tuple[tuple[float, float], tuple[float, float]]:
        return self.c.cfg.pan_limits, self.c.cfg.tilt_limits

    def execute(self, plan: Plan, wait: bool = False) -> dict:
        """Start the plan; returns what the robot should say ("สุดแล้ว" when a move hit a limit)."""
        with self._lock:
            self._run += 1
            run = self._run
        if plan.moves[0].kind == "stop":
            self._pause(DEFAULT_HOLD_S)
            self.c.face(self.c.pan, self.c.tilt, hold_s=DEFAULT_HOLD_S)
            return {"said": "หยุดแล้วครับ", "clamped": False}
        targets, clamped = self._targets(plan)
        hold = HOLD_ON_S if plan.hold else DEFAULT_HOLD_S
        self._pause(hold + 5.0 * len(targets))
        if self.say and targets and targets[-1][0] != "gesture":
            pan, tilt = targets[-1][1]
            self.say(f"pan {pan:+.0f}° tilt {tilt:.0f}°", 4.0)
        t = threading.Thread(target=self._play, args=(run, targets, plan.slow, hold), name="head", daemon=True)
        t.start()
        if wait:
            t.join()
        return {"said": "สุดแล้วครับ" if clamped else "ได้ครับ", "clamped": clamped,
                "moves": [x[1] for x in targets]}

    def _pause(self, seconds: float) -> None:
        if self.tracker:
            self.tracker.pause(seconds)

    def _targets(self, plan: Plan) -> tuple[list, bool]:
        (plo, phi), (tlo, thi) = self.limits()
        pan, tilt = self.c.pan, self.c.tilt
        out, clamped = [], False
        for m in plan.moves:
            if m.kind == "gesture":
                out.append(("gesture", m.gesture))
                continue
            if m.kind == "home":
                want = (self.c.cfg.home_pan, self.c.cfg.home_tilt)
            elif m.kind == "back":
                want = self.previous or (self.c.cfg.home_pan, self.c.cfg.home_tilt)
            else:
                dp, dt = (m.pan, m.tilt) if m.kind == "by" else (self.last_dir[0] * m.pan, self.last_dir[1] * m.pan)
                if m.amount == "max":
                    dp = 999.0 * (dp > 0) - 999.0 * (dp < 0)
                    dt = 999.0 * (dt > 0) - 999.0 * (dt < 0)
                if m.kind == "by" and (dp or dt):
                    n = max(abs(dp), abs(dt))
                    self.last_dir = (dp / n if dp else 0.0, dt / n if dt else 0.0)
                want = (pan + dp, tilt + dt)
            new = (min(phi, max(plo, want[0])), min(thi, max(tlo, want[1])))
            # asked to go further than the head can: say so, unless it was "สุด" on purpose
            if m.amount != "max" and (abs(new[0] - want[0]) > 0.5 or abs(new[1] - want[1]) > 0.5):
                clamped = True
            if m.amount == "max" and (pan, tilt) == new:
                clamped = True  # already there
            out.append(("pose", new))
            pan, tilt = new
        return out, clamped

    def _play(self, run: int, targets: list, slow: bool, hold: float) -> None:
        self.previous = (self.c.pan, self.c.tilt)
        for kind, what in targets:
            if run != self._run:
                return
            if kind == "gesture":
                self.c.gesture(what)
                time.sleep(self.GESTURE_S.get(what, 2.0))
                continue
            start = (self.c.pan, self.c.tilt)
            steps = max(1, int(max(abs(what[0] - start[0]), abs(what[1] - start[1])) / 6)) if slow else 1
            for i in range(1, steps + 1):
                if run != self._run:
                    return
                f = i / steps
                pose = (start[0] + (what[0] - start[0]) * f, start[1] + (what[1] - start[1]) * f)
                self.c.face(pose[0], pose[1], hold_s=hold)
                time.sleep(0.35 if slow else 0.0)
            # wait for the head to get there before the next move of a sequence
            time.sleep(min(1.5, 0.4 + max(abs(what[0] - start[0]), abs(what[1] - start[1])) / 90.0))
        log.info("head: done %s", [w for _, w in targets])
