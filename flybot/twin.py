"""Hospital digital twin patrolled by the whole fly brain (FlyWire connectome, LIF).

    python -m flybot.twin --data-dir data/codex --port 8090

The world is a schematic hospital: departments with queues that rise and fall over the
day and sometimes surge. A fly flies over the map:

1. Input converter (ours): crisis points to the fly's left / right drive the left / right
   eye's photoreceptors (R1-6) at Poisson rates that grow with severity and closeness.
2. Brain (the connectome): the whole brain runs 100 ms per step; the steering signal is the
   right-minus-left synaptic drive of the descending neurons DNa01/DNa02. Their response is
   asymmetric and noisy, so each side is calibrated at start-up; reading "more drive on the
   right" as "turn toward the left stimulus" is our mapping, not something the fly decided.
3. Decision (ours, a rule): reaching a department in crisis opens an extra service counter
   for a while. The brain only navigates.

Two shadow worlds run on the same arrivals for comparison: an autopilot that flies straight
at the worst department, and a world where nobody acts. Assumptions are shown on the page.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

import numpy as np

log = logging.getLogger(__name__)

# Schematic layout (metres) - NOT the real hospital plan.
DEPARTMENTS = [
    # id, name, x, y, counters, minutes per patient per counter, base arrivals per hour
    ("er", "ฉุกเฉิน", 30, 30, 3, 12, 14),
    ("opd_med", "OPD อายุรกรรม", 90, 25, 6, 8, 40),
    ("opd_sur", "OPD ศัลยกรรม", 150, 30, 4, 9, 22),
    ("ped", "กุมารเวช", 175, 80, 3, 10, 14),
    ("pharm", "ห้องยา", 110, 70, 5, 4, 70),
    ("lab", "ห้องแล็บ", 60, 85, 4, 5, 45),
    ("xray", "เอกซเรย์", 20, 95, 2, 7, 15),
    ("ward", "หอผู้ป่วยใน", 140, 110, 3, 20, 7),
]
MAP_W, MAP_H = 200, 130
ASSUMPTIONS = [
    "แผนผังและตัวเลขคิวเป็นข้อมูลสมมติ ไม่ใช่ข้อมูลจริงของโรงพยาบาล",
    "สมองแมลงหวี่ (FlyWire ~138k เซลล์, LIF, ไม่เรียนรู้) ทำหน้าที่ 'นำทาง' เท่านั้น",
    "ตัวแปลงข้อมูลเข้า (วิกฤตซ้าย/ขวา → ตาซ้าย/ขวา) และการตีความสัญญาณเลี้ยวเป็นการออกแบบของเรา",
    "สัญญาณเลี้ยวของสมองไม่สมมาตร: ตาขวาต้องกระตุ้นถึง ~240 Hz จึงได้ทิศที่ถูก จึงส่งแค่ 'ทิศ' ไม่ส่งระดับความรุนแรง",
    "การตัดสินใจเมื่อถึงจุด (เปิดช่องบริการเพิ่ม) เป็นกฎง่าย ๆ ไม่ได้มาจากสมอง",
    "ผลเปรียบเทียบใช้ดูแนวโน้มในโลกจำลองนี้ ไม่ใช่หลักฐานว่าใช้บริหารงานจริงได้",
]


@dataclass
class Dept:
    id: str
    name: str
    x: float
    y: float
    counters: int
    service_min: float
    base_rate: float
    queue: float = 0.0
    extra_until: float = -1.0  # minute until which an extra counter is open

    def capacity_per_min(self, minute: float) -> float:
        n = self.counters + (1 if minute < self.extra_until else 0)
        return n / self.service_min

    @property
    def crisis(self) -> float:
        """0 = fine, 1 = queue as long as an hour of normal capacity."""
        return min(2.0, self.queue / (60.0 * self.counters / self.service_min))


class Hospital:
    """Queues with a daytime arrival curve and random surges; identical across worlds via the seed."""

    def __init__(self, seed: int = 1):
        self.depts = [Dept(*d) for d in DEPARTMENTS]
        self.rng = np.random.default_rng(seed)
        self.surge: dict[str, tuple[float, float]] = {}  # id -> (until minute, multiplier)

    def clone(self) -> "Hospital":
        h = Hospital.__new__(Hospital)
        h.depts = [Dept(**{k: getattr(d, k) for k in d.__dataclass_fields__}) for d in self.depts]
        h.rng = np.random.default_rng()
        h.rng.bit_generator.state = self.rng.bit_generator.state
        h.surge = dict(self.surge)
        return h

    def step(self, minute: float) -> None:
        hour = (minute / 60.0) % 24
        day_curve = max(0.05, math.exp(-((hour - 10.0) / 3.5) ** 2) + 0.4 * math.exp(-((hour - 14.0) / 2.0) ** 2))
        if self.rng.random() < 0.004:  # a surge somewhere (bus accident, clinic backlog...)
            d = self.depts[self.rng.integers(len(self.depts))]
            self.surge[d.id] = (minute + self.rng.uniform(30, 90), self.rng.uniform(2.0, 4.0))
        for d in self.depts:
            until, mult = self.surge.get(d.id, (0.0, 1.0))
            rate = d.base_rate / 60.0 * day_curve * (mult if minute < until else 1.0)
            d.queue = max(0.0, d.queue + self.rng.poisson(rate) - d.capacity_per_min(minute))

    def index(self) -> float:
        return float(np.mean([d.crisis for d in self.depts]))

    def act(self, dept: Dept, minute: float) -> bool:
        if dept.crisis > 0.35 and minute >= dept.extra_until:
            dept.extra_until = minute + 45.0  # open an extra counter for 45 min
            return True
        return False


class ReplayHospital(Hospital):
    """Arrivals replayed minute by minute from aggregated HIS queue counts (no identifiers).

    The file holds, per service point, how many queue tickets were issued each minute of
    one day. Service capacity is not in the data (several points never mark a ticket done),
    so each point is given the capacity to serve its busy-hour rate (85th percentile of its
    30-minute arrival rate); surges above it build a queue. An extra counter adds 25%.
    """

    COLUMNS = 5

    def __init__(self, path: str):
        data = json.loads(open(path, encoding="utf-8").read())
        self.date, self.source = data.get("date"), data.get("source")
        points = sorted(data["points"], key=lambda p: -sum(p["arrivals"].values()))
        self.arrivals: dict[str, np.ndarray] = {}
        self.depts = []
        rows = math.ceil(len(points) / self.COLUMNS)
        for i, p in enumerate(points):
            per_min = np.zeros(24 * 60)
            for minute, n in p["arrivals"].items():
                per_min[int(minute)] += n
            window = np.convolve(per_min, np.ones(30) / 30, mode="same")
            busy = window[window > 0]
            capacity = max(0.05, float(np.percentile(busy, 85))) if len(busy) else 0.1
            col, row = i % self.COLUMNS, i // self.COLUMNS
            x = 20 + col * (MAP_W - 40) / max(1, self.COLUMNS - 1)
            y = 20 + row * (MAP_H - 40) / max(1, rows - 1)
            # 4 counters: one extra counter = +25% capacity
            self.depts.append(Dept(str(p["sp"]), p["name"], x, y, 4, 4.0 / capacity, 0.0))
            self.arrivals[str(p["sp"])] = per_min
        self.rng = np.random.default_rng(0)  # unused by the replay; kept for clone()
        self.surge = {}

    def clone(self) -> "ReplayHospital":
        h = ReplayHospital.__new__(ReplayHospital)
        h.date, h.source, h.arrivals, h.rng, h.surge = self.date, self.source, self.arrivals, self.rng, {}
        h.depts = [Dept(**{k: getattr(d, k) for k in d.__dataclass_fields__}) for d in self.depts]
        return h

    def step(self, minute: float) -> None:
        m = int(minute) % (24 * 60)
        for d in self.depts:
            d.queue = max(0.0, d.queue + self.arrivals[d.id][m] - d.capacity_per_min(minute))


REPLAY_ASSUMPTIONS = [
    "จำนวนผู้มารับบริการรายนาทีเป็นข้อมูลจริงจากระบบคิว Q4U (นับรวมต่อจุดบริการ ไม่มีข้อมูลรายบุคคล) เล่นซ้ำทั้งวัน",
    "ตำแหน่งบนแผนผังเป็นการจัดวางสมมติ ไม่ใช่ตำแหน่งจริงของห้อง",
    "ความเร็วในการให้บริการไม่มีในข้อมูล (หลายจุดไม่บันทึกว่าเสร็จ) จึงประมาณจากช่วงที่คนมามากของจุดนั้น — คิวในภาพเป็นค่าประมาณ",
]


@dataclass
class Agent:
    name: str
    x: float = MAP_W / 2
    y: float = MAP_H - 10
    heading: float = -90.0  # degrees, 0 = +x (east), -90 = up (north)
    speed: float = 12.0  # metres per simulated minute
    distance: float = 0.0
    actions: dict = field(default_factory=dict)
    trail: list = field(default_factory=list)
    busy_until: float = -1.0  # minute until which it stays at the department it just helped
    resting: bool = False

    def move(self, turn_deg: float) -> None:
        self.heading = (self.heading + turn_deg + 180) % 360 - 180
        nx = self.x + self.speed * math.cos(math.radians(self.heading))
        ny = self.y + self.speed * math.sin(math.radians(self.heading))
        if not (3 <= nx <= MAP_W - 3 and 3 <= ny <= MAP_H - 3):  # bounce off the walls
            self.heading = (self.heading + 360) % 360 - 180  # reversed (h + 180), normalised to [-180, 180)
            nx, ny = self.x, self.y
        self.distance += math.hypot(nx - self.x, ny - self.y)
        self.x, self.y = nx, ny
        self.trail = (self.trail + [(round(self.x, 1), round(self.y, 1))])[-150:]


def bearing(agent: Agent, d: Dept) -> tuple[float, float]:
    """(distance, bearing relative to heading in degrees; positive = to the right)."""
    dx, dy = d.x - agent.x, d.y - agent.y
    b = math.degrees(math.atan2(dy, dx)) - agent.heading
    return math.hypot(dx, dy), (b + 180) % 360 - 180


class FlyNavigator:
    """Whole fly brain as the steering controller (input converter + calibrated readout)."""

    # Measured on the v783 connectome: below ~100 Hz the eye drive does not reach the DNs, and
    # the right eye only gives a reliable (correct-sign) response near 240 Hz, so the converter
    # encodes direction (which side is worse) rather than graded severity.
    BASE_HZ, FULL_HZ, MIN_WEIGHT, AHEAD_DEG = 20.0, 240.0, 0.05, 20.0

    def __init__(self, data_dir: str, window_ms: float = 100.0, seed: int = 0):
        from .wholebrain import LIFParams, WholeBrain
        self.brain = WholeBrain.load(data_dir, LIFParams(), )
        self.brain.rng = np.random.default_rng(seed)
        self.window = window_ms
        b = self.brain
        self.eye = {s: b.select(type="R1-6", side=s) for s in "LR"}
        self.dn = {s: b.select(type="DNa02", side=s) + b.select(type="DNa01", side=s) for s in "LR"}
        self.probe = self.dn["L"] + self.dn["R"]
        self.last = {"left_hz": 0.0, "right_hz": 0.0, "drive_l": 0.0, "drive_r": 0.0, "steer": 0.0, "turn": 0.0}
        self.calibrate()

    def _drive(self, left_hz: float, right_hz: float) -> tuple[float, float]:
        self.brain.reset()
        self.brain.run(self.window, {self.eye["L"]: left_hz, self.eye["R"]: right_hz}, probe=self.probe)
        n = len(self.dn["L"])
        return float(self.brain.drive[:n].mean()), float(self.brain.drive[n:].mean())

    def calibrate(self, repeats: int = 4) -> None:
        """Per-side scale of the right-minus-left drive (left eye -> positive, right eye -> negative)."""
        full = self.FULL_HZ
        pos = [np.subtract(*self._drive(full, self.BASE_HZ)[::-1]) for _ in range(repeats)]
        neg = [np.subtract(*self._drive(self.BASE_HZ, full)[::-1]) for _ in range(repeats)]
        self.scale_pos = max(1e-3, float(np.mean(pos)))
        self.scale_neg = max(1e-3, float(-np.mean(neg)))
        self.calibration = {"left_eye": round(float(np.mean(pos)), 2), "right_eye": round(float(np.mean(neg)), 2)}
        log.info("fly calibration (R-L drive, mV): %s", self.calibration)

    def turn(self, agent: Agent, depts: list[Dept]) -> float:
        left = right = 0.0
        target = max(depts, key=lambda d: d.crisis / (1.0 + bearing(agent, d)[0] / 40.0))
        target_bearing = bearing(agent, target)[1]
        for d in depts:
            dist, b = bearing(agent, d)
            w = d.crisis / (1.0 + dist / 40.0)
            side = math.sin(math.radians(b))
            if abs(b) > 90:  # behind: the fly turns toward whichever side it is on
                side = math.copysign(1.0, b)
            right += w * max(0.0, side)
            left += w * max(0.0, -side)
        if left + right < self.MIN_WEIGHT or abs(target_bearing) < self.AHEAD_DEG:
            # nothing worth flying to, or the worst place is already ahead: both eyes equal, fly straight
            # (driving one eye at full rate always would make the fly circle its target)
            left_hz = right_hz = self.BASE_HZ
        else:  # the worse side at full drive, the other side scaled by how close it is
            strong, weak = max(left, right), min(left, right)
            hi, lo = self.FULL_HZ, self.BASE_HZ + (self.FULL_HZ - self.BASE_HZ) * 0.5 * weak / strong
            left_hz, right_hz = (hi, lo) if left >= right else (lo, hi)
        dl, dr = self._drive(left_hz, right_hz)
        s = dr - dl
        steer = s / self.scale_pos if s > 0 else s / self.scale_neg  # +1 ~ "left eye", -1 ~ "right eye"
        turn = float(np.clip(-steer, -1.5, 1.5)) * 25.0  # our mapping: turn toward the stimulated eye
        self.last = {"left_hz": round(left_hz), "right_hz": round(right_hz), "drive_l": round(dl, 2),
                     "drive_r": round(dr, 2), "steer": round(float(steer), 2), "turn": round(turn, 1)}
        return turn


def autopilot_turn(agent: Agent, depts: list[Dept]) -> float:
    worst = max(depts, key=lambda d: d.crisis / (1.0 + bearing(agent, d)[0] / 40.0))
    return float(np.clip(bearing(agent, worst)[1], -25.0, 25.0))


class Twin:
    def __init__(self, navigator: FlyNavigator | None, seed: int = 1, minute: float = 7 * 60,
                 replay: str | None = None):
        self.minute = minute
        base = ReplayHospital(replay) if replay else Hospital(seed)
        self.replay = base if replay else None
        self.worlds = {"fly": base, "autopilot": base.clone(), "none": base.clone()}
        self.agents = {"fly": Agent("แมลงหวี่"), "autopilot": Agent("autopilot")}
        self.nav = navigator
        self.history: list[dict] = []
        self.log: list[dict] = []
        self.lock = threading.Lock()

    DWELL_MIN = 10.0  # minutes spent at a department after acting there
    REST_BELOW = 0.2  # no department this bad: stay put instead of patrolling

    def _stays(self, key: str) -> bool:
        """Same rule for every agent, so distances compare fairly: stay while helping, rest when calm."""
        agent, world = self.agents[key], self.worlds[key]
        agent.resting = max(d.crisis for d in world.depts) < self.REST_BELOW
        return self.minute < agent.busy_until or agent.resting

    def tick(self) -> None:
        fly_stays = self._stays("fly")
        # the brain only runs while the fly is flying
        turn_fly = self.nav.turn(self.agents["fly"], self.worlds["fly"].depts) if self.nav and not fly_stays else 0.0
        with self.lock:
            for world in self.worlds.values():
                world.step(self.minute)
            for key in ("fly", "autopilot"):
                agent, world = self.agents[key], self.worlds[key]
                if fly_stays if key == "fly" else self._stays(key):
                    continue
                agent.move(turn_fly if key == "fly" else autopilot_turn(agent, world.depts))
                for d in world.depts:
                    if math.hypot(d.x - agent.x, d.y - agent.y) < 10 and world.act(d, self.minute):
                        agent.busy_until = self.minute + self.DWELL_MIN
                        agent.actions[d.name] = agent.actions.get(d.name, 0) + 1
                        if key == "fly":
                            self.log = (self.log + [{"at": self.clock(), "dept": d.name, "action": "เปิดช่องบริการเพิ่ม 45 นาที",
                                                     "km": round(agent.distance / 1000, 2)}])[-30:]
            self.history = (self.history + [{"t": self.minute, **{k: round(w.index(), 3) for k, w in self.worlds.items()}}])[-600:]
            self.minute += 1

    def clock(self) -> str:
        m = int(self.minute) % (24 * 60)
        return f"วัน {int(self.minute // 1440) + 1} {m // 60:02d}:{m % 60:02d}"

    def state(self) -> dict:
        with self.lock:
            fly_world = self.worlds["fly"]
            return {
                "clock": self.clock(), "map": {"w": MAP_W, "h": MAP_H},
                "depts": [{"id": d.id, "name": d.name, "x": d.x, "y": d.y, "crisis": round(d.crisis, 2),
                           "queue": round(d.queue), "extra": self.minute < d.extra_until} for d in fly_world.depts],
                "agents": {k: {"x": a.x, "y": a.y, "heading": a.heading, "km": round(a.distance / 1000, 2),
                               "state": "busy" if self.minute < a.busy_until else "resting" if a.resting else "flying",
                               "actions": sum(a.actions.values()), "trail": a.trail} for k, a in self.agents.items()},
                "index": {k: round(w.index(), 3) for k, w in self.worlds.items()},
                "mean_index": {k: round(float(np.mean([h[k] for h in self.history])), 3) if self.history else 0.0
                               for k in self.worlds},
                "history": self.history[-240:], "log": self.log[-12:],
                "brain": self.nav.last if self.nav else None,
                "calibration": self.nav.calibration if self.nav else None,
                "assumptions": (REPLAY_ASSUMPTIONS + ASSUMPTIONS[1:]) if self.replay else ASSUMPTIONS,
                "data": ({"source": self.replay.source, "date": self.replay.date} if self.replay else None),
            }

    def run_forever(self, tick_s: float = 0.0) -> None:
        """One simulated minute per tick; ``tick_s`` is the minimum wall time per tick (pacing)."""
        while True:
            start = time.monotonic()
            try:
                self.tick()
            except Exception:
                log.exception("twin tick failed")
                time.sleep(1.0)
            time.sleep(max(0.0, tick_s - (time.monotonic() - start)))


def serve(twin: Twin, port: int) -> ThreadingHTTPServer:
    page = resources.files("flybot").joinpath("web/twin.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/twin"):
                return self._send(200, page, "text/html; charset=utf-8")
            if self.path == "/api/state":
                return self._send(200, json.dumps(twin.state(), ensure_ascii=False).encode(), "application/json")
            if self.path == "/healthz":
                return self._send(200, b"ok", "text/plain")
            self._send(404, b"not found", "text/plain")

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, name="twin-http", daemon=True).start()
    return server


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default="data/codex", help="FlyWire Codex CSVs (whole brain cached to wholebrain.npz)")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--no-brain", action="store_true", help="skip the fly (autopilot and no-action worlds only)")
    ap.add_argument("--replay", help="JSON of real per-minute arrivals per service point (aggregated HIS queue counts)")
    ap.add_argument("--tick-seconds", type=float, default=1.0,
                    help="minimum wall seconds per simulated minute (0 = as fast as the brain runs)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    nav = None if args.no_brain else FlyNavigator(args.data_dir)
    twin = Twin(nav, seed=args.seed, minute=6 * 60 if args.replay else 7 * 60, replay=args.replay)
    serve(twin, args.port)
    log.info("Twin on http://0.0.0.0:%d", args.port)
    twin.run_forever(args.tick_seconds)


if __name__ == "__main__":
    main()
