"""Mushroom body and monoamine state -> robot emotion.

* Projection neurons carry a small sensory feature vector.
* Kenyon cells (KC) form a sparse random expansion with APL-like k-winner-take-all.
* A familiarity MBON reads KC activity; its KC->MBON synapses depress when a
  pattern repeats (habituation) and slowly recover, so ``novelty`` drops for
  repeated stimuli.
* Octopamine (arousal) follows novelty x intensity, looming and shaking.
* Dopamine (positive valence) follows gentle close interaction and successful
  tracking, and gates faster recovery of the KC->MBON synapses.
* Sleep pressure builds while nothing new happens.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

EXPRESSIONS = ("curious", "alert", "sleepy", "happy")


@dataclass
class Percept:
    motion: float = 0.0  # |image velocity|, 0..1
    x: float = 0.0  # target position, -1..1
    y: float = 0.0
    target: bool = False
    nearness: float = 0.0  # 0 far .. 1 touching
    approach: float = 0.0  # d(nearness)/dt, >0 approaching
    shake: float = 0.0  # IMU disturbance, 0..1
    centered: float = 0.0  # 1 when the target is in the middle of the frame


@dataclass
class Neuromodulators:
    dopamine: float = 0.0
    octopamine: float = 0.0
    sleep_pressure: float = 0.0
    novelty: float = 1.0


class MushroomBody:
    def __init__(
        self,
        n_kc: int = 400,
        inputs_per_kc: int = 4,
        sparsity: float = 0.05,
        depression: float = 0.4,
        recovery_s: float = 30.0,
        hold_s: float = 1.0,
        alert_threshold: float = 0.5,
        seed: int = 3,
    ):
        rng = np.random.default_rng(seed)
        self.n_pn = 10
        self.j = np.zeros((n_kc, self.n_pn))
        for k in range(n_kc):
            self.j[k, rng.choice(self.n_pn, inputs_per_kc, replace=False)] = rng.uniform(0.5, 1.0, inputs_per_kc)
        self.k_active = max(1, int(sparsity * n_kc))
        self.w = np.ones(n_kc)
        self.depression = depression
        self.recovery_s = recovery_s
        self.hold_s = hold_s
        # a new object after a long idle period peaks near 0.56 (the empty scene
        # habituates overlapping KCs); rotation/petting stay below ~0.32
        self.alert_threshold = alert_threshold
        self.state = Neuromodulators()
        self.expression = "curious"
        self._since_change = 0.0

    def _pn(self, p: Percept) -> np.ndarray:
        # coarse place code for position plus intensity channels (glomeruli)
        return np.array([
            p.motion, max(p.x, 0), max(-p.x, 0), max(p.y, 0), max(-p.y, 0),
            float(p.target), p.nearness, max(p.approach, 0), p.shake, 1.0 - p.nearness,
        ])

    def kenyon(self, p: Percept) -> np.ndarray:
        drive = self.j @ self._pn(p)
        kc = np.zeros_like(drive)
        top = np.argpartition(drive, -self.k_active)[-self.k_active:]
        kc[top] = drive[top] > 1e-6
        return kc

    def step(self, p: Percept, dt: float) -> tuple[Neuromodulators, str, bool]:
        """Advance the neuromodulator state; return (state, expression, changed)."""
        s = self.state
        kc = self.kenyon(p)
        active = kc.sum()
        s.novelty = float(self.w @ kc / active) if active else s.novelty

        intensity = min(1.0, p.motion + 0.5 * float(p.target))
        looming = max(p.approach, 0.0) * p.nearness
        oa_drive = min(1.0, 0.9 * s.novelty * intensity + 2.0 * looming + p.shake)
        petting = p.nearness * (1.0 - min(1.0, 3 * max(p.approach, 0))) if 0.3 < p.nearness < 0.9 else 0.0
        da_drive = min(1.0, 0.9 * petting + 0.6 * p.centered * float(p.target) * (1.0 - s.novelty))

        s.octopamine += dt / 0.5 * (oa_drive - s.octopamine)
        s.dopamine += dt / 2.0 * (da_drive - s.dopamine)
        boredom = (1.0 - intensity) * (1.0 - s.octopamine)
        s.sleep_pressure += dt / 20.0 * (boredom - s.sleep_pressure)
        if s.octopamine > 0.5:
            s.sleep_pressure *= 0.5  # startle wakes the robot

        # habituation of KC->MBON synapses; dopamine speeds up recovery
        recovery = dt / self.recovery_s * (1.0 + 4.0 * s.dopamine)
        self.w += -self.depression * dt * kc * self.w + recovery * (1.0 - self.w)
        np.clip(self.w, 0.0, 1.0, out=self.w)

        changed = self._choose(dt)
        return s, self.expression, changed

    def _choose(self, dt: float) -> bool:
        s = self.state
        if s.octopamine > self.alert_threshold:
            want = "alert"
        elif s.dopamine > 0.45:
            want = "happy"
        elif s.sleep_pressure > 0.6 and s.octopamine < 0.2:
            want = "sleepy"
        else:
            want = "curious"
        self._since_change += dt
        # alert may interrupt immediately; other changes respect the hold time
        if want != self.expression and (want == "alert" or self._since_change >= self.hold_s):
            self.expression = want
            self._since_change = 0.0
            return True
        return False
