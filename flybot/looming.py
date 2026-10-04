"""Looming detection and the Giant Fiber escape (LPLC2 / LC4 -> DNp01).

* LPLC2 dendrites have four arms in the lobula plate, each preferring motion outward
  from the cell's centre, so they respond to radial expansion but not to translation.
  Here the arms are the four edges of the moving blob's bounding box and the expansion
  is the outward speed shared by all four (``min``), which is zero when the blob just
  slides and rarely positive from edge noise. LPLC2 drive is that expansion relative to
  the blob's size, d(theta)/dt / theta (the inverse of time-to-contact), gated on by
  size between ``MIN_SIZE_DEG`` and twice that, as the Giant Fiber fires once a looming
  object passes an angular size threshold.
* LC4 cells respond to fast edge motion; their drive is the absolute expansion speed
  d(theta)/dt.
* The Giant Fiber sums both, weighted by their share of its synaptic input in the
  connectome, through a leaky membrane; crossing threshold triggers one escape and a
  refractory period. A fast approach on the proximity sensor adds an extra
  (non-visual) cue, capped below threshold so it cannot trigger an escape alone
  (reaching out to pet the robot is also a fast approach).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import pandas as pd

from .optic_lobe import input_fraction

log = logging.getLogger(__name__)

TTC_SCALE = 2.0  # 1/s of relative expansion that drives LPLC2 to 1 (0.5 s to contact)
SPEED_SCALE = 60.0  # deg/s of expansion that drives LC4 to 1
MIN_SIZE_DEG = 10.0


@dataclass
class LoomingGains:
    lplc2: float = 1.0
    lc4: float = 1.0
    raw: dict = field(default_factory=dict)


def derive_looming_gains(adj: pd.DataFrame) -> LoomingGains:
    """Share of the Giant Fiber's input from LPLC2 vs LC4, normalised to mean 1."""
    frac = input_fraction(adj)
    raw = {k: float(frac.at[k, "GF"]) if k in frac.index and "GF" in frac.columns else 0.0
           for k in ("LPLC2", "LC4")}
    total = sum(raw.values())
    if total <= 0:
        log.warning("No LPLC2/LC4 -> Giant Fiber synapses; looming uses unit gains")
        return LoomingGains(raw=raw)
    return LoomingGains(lplc2=2 * raw["LPLC2"] / total, lc4=2 * raw["LC4"] / total, raw=raw)


class GiantFiber:
    def __init__(self, gains: LoomingGains, threshold: float = 1.0, tau: float = 0.05,
                 refractory_s: float = 2.0, approach_gain: float = 1.0):
        self.gains = gains
        self.threshold = threshold
        self.tau = tau
        self.refractory_s = refractory_s
        self.approach_gain = approach_gain
        self.v = 0.0
        self.lplc2 = self.lc4 = 0.0
        self._box: tuple[tuple[float, float, float, float], float] | None = None  # (edges deg, stamp)
        self._theta = 0.0
        self._rate = 0.0
        self._last_spike = -math.inf

    def observe(self, box_deg: tuple[float, float, float, float] | None, stamp: float | None) -> None:
        """Feed the moving blob's bounding box (left, top, right, bottom; deg) from a new frame."""
        if box_deg is None or stamp is None:
            self._box = None
            self._rate = 0.0
            return
        if self._box and stamp > self._box[1]:
            (l0, t0, r0, b0), dt = self._box[0], stamp - self._box[1]
            l1, t1, r1, b1 = box_deg
            # outward motion of both opposite edges; image y grows downwards
            arms = min(r1 - r0, l0 - l1, b1 - b0, t0 - t1) / dt
            self._rate = 0.5 * self._rate + 0.5 * 2.0 * max(arms, 0.0)  # d(theta)/dt
        if not self._box or stamp != self._box[1]:
            self._box = (tuple(box_deg), stamp)
            self._theta = max(box_deg[2] - box_deg[0], box_deg[3] - box_deg[1])

    def step(self, dt: float, now: float, approach: float = 0.0) -> bool:
        """Advance the membrane; return True on the step the Giant Fiber fires."""
        theta = self._theta if self._box else 0.0
        expanding = self._rate
        size_gate = min(1.0, max(0.0, theta / MIN_SIZE_DEG - 1.0))
        self.lplc2 = size_gate * min(1.5, expanding / max(theta, 1.0) / TTC_SCALE)
        self.lc4 = size_gate * min(1.5, expanding / SPEED_SCALE)
        ir = min(0.6 * self.threshold, self.approach_gain * max(approach, 0.0))
        drive = self.gains.lplc2 * self.lplc2 + self.gains.lc4 * self.lc4 + ir
        self.v += dt / self.tau * (drive - self.v)
        if self.v > self.threshold and now - self._last_spike >= self.refractory_s:
            self._last_spike = now
            return True
        return False
