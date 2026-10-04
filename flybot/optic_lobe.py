"""Connectome-constrained optic-lobe model (Nengo) for head tracking.

Pathway (one hemisphere)::

    camera ──► lamina L1 (ON) ─► Mi1/Tm3/Mi4/Mi9 ─► T4a..d ┐
           └─► lamina L2 (OFF) ─► Tm1/Tm2/Tm4/Tm9 ─► T5a..d ┤─► HS (horizontal), VS (vertical)
           └─► lobula (Tm) ─────────────────────────► LC10 (object position)

Weights of each stage are derived from the FlyWire adjacency matrix: the
fraction of a cell group's input coming from the upstream group, multiplied
along two-hop paths. T4/T5 subtypes a/b/c/d map to image motion
right/left/up/down; HS is excited by ``a`` and inhibited by ``b`` through
LPi, VS likewise by ``d`` and ``c``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

DIRECTIONS = ("a", "b", "c", "d")
ON_MEDULLA = ("Mi1", "Tm3", "Mi4", "Mi9")
OFF_MEDULLA = ("Tm1", "Tm2", "Tm4", "Tm9")


@dataclass
class CircuitGains:
    """Relative synaptic gains extracted from the connectome (mean ~1 per stage)."""

    on: dict[str, float]  # lamina L1 -> T4{d}
    off: dict[str, float]  # lamina L2 -> T5{d}
    hs_exc: float  # T4a/T5a -> HS
    hs_inh: float  # T4b/T5b -> LPi -> HS
    vs_exc: float  # T4d/T5d -> VS
    vs_inh: float  # T4c/T5c -> LPi -> VS
    position: float = 1.0  # lobula -> LC10
    raw: dict = field(default_factory=dict)

    @classmethod
    def uniform(cls) -> "CircuitGains":
        ones = {d: 1.0 for d in DIRECTIONS}
        return cls(dict(ones), dict(ones), 1.0, 1.0, 1.0, 1.0)


def input_fraction(adj: pd.DataFrame) -> pd.DataFrame:
    """Normalise each column by the post group's total (absolute) modelled input."""
    a = adj.abs()
    total = a.sum(axis=0).replace(0.0, np.nan)
    return (a / total).fillna(0.0)


def _normalise(values: dict[str, float], label: str) -> dict[str, float]:
    present = {k: v for k, v in values.items() if v > 0}
    if not present:
        log.warning("No synapses found for %s; using uniform gains", label)
        return {k: 1.0 for k in values}
    missing = sorted(set(values) - set(present))
    if missing:
        log.warning("%s: no synapses for %s; filling with the mean", label, missing)
    mean = float(np.mean(list(present.values())))
    return {k: values[k] / mean if k in present else 1.0 for k in values}


def derive_gains(adj: pd.DataFrame) -> CircuitGains:
    """Turn a ``pre_group x post_group`` synapse matrix into model gains."""
    frac = input_fraction(adj)

    def w(src: str, dst: str) -> float:
        if src in frac.index and dst in frac.columns:
            return float(frac.at[src, dst])
        return 0.0

    def path(src: str, mids: tuple[str, ...], dst: str) -> float:
        return sum(w(src, m) * w(m, dst) for m in mids)

    on = {d: path("L1", ON_MEDULLA, f"T4{d}") for d in DIRECTIONS}
    off = {d: path("L2", OFF_MEDULLA, f"T5{d}") for d in DIRECTIONS}
    feed = _normalise({f"on_{d}": v for d, v in on.items()} | {f"off_{d}": v for d, v in off.items()}, "T4/T5 input")

    # one-hop excitation and two-hop (via LPi) inhibition differ in scale, so each
    # kind is normalised on its own; HS vs VS differences within a kind are kept
    lptc = _normalise(
        {"hs_exc": w("T4a", "HS") + w("T5a", "HS"), "vs_exc": w("T4d", "VS") + w("T5d", "VS")},
        "lobula plate excitation",
    ) | _normalise(
        {
            "hs_inh": path("T4b", ("LPi",), "HS") + path("T5b", ("LPi",), "HS"),
            "vs_inh": path("T4c", ("LPi",), "VS") + path("T5c", ("LPi",), "VS"),
        },
        "lobula plate inhibition (LPi)",
    )
    has_lc10 = "LC10" in adj.columns and adj["LC10"].abs().sum() > 0
    if not has_lc10:
        log.warning("LC10 has no modelled input; object-position pathway uses unit gain")

    return CircuitGains(
        on={d: feed[f"on_{d}"] for d in DIRECTIONS},
        off={d: feed[f"off_{d}"] for d in DIRECTIONS},
        raw={"on": on, "off": off, "has_lc10": bool(has_lc10)},
        **lptc,
    )


def lamina(u: np.ndarray) -> np.ndarray:
    """Photoreceptor/lamina stage: split motion into ON/OFF directional channels.

    ``u = [x, y, vx, vy, polarity]`` (all in [-1, 1]); polarity +1 means the
    moving edge is brightening (ON), -1 darkening (OFF), 0 unknown.
    Returns ``[L1 a..d, L2 a..d, x, y]``.
    """
    x, y, vx, vy, pol = u
    on = 0.5 * (1.0 + pol)
    motion = np.array([vx, -vx, -vy, vy])  # a: right, b: left, c: up, d: down (image coords)
    return np.concatenate([on * motion, (1.0 - on) * motion, [x, y]])


def _relu(x):
    return np.maximum(x, 0.0)


class OpticLobeNetwork:
    """Spiking (LIF) Nengo implementation of the pathway above."""

    def __init__(self, gains: CircuitGains, n_neurons: int = 60, dt: float = 0.001, seed: int = 1):
        import nengo

        self.gains = gains
        self.dt = dt
        self._stim = np.zeros(5)
        self._out = np.zeros(4)
        g = gains
        rect = dict(
            encoders=nengo.dists.Choice([[1.0]]),
            intercepts=nengo.dists.Uniform(0.0, 0.5),
        )
        with nengo.Network(seed=seed, label="FlyWire optic lobe") as net:
            stim = nengo.Node(lambda t: self._stim, label="photoreceptors")
            lam = nengo.Node(lambda t, u: lamina(u), size_in=5, size_out=10, label="lamina")
            nengo.Connection(stim, lam, synapse=None)

            hs = nengo.Ensemble(2 * n_neurons, 1, label="HS")
            vs = nengo.Ensemble(2 * n_neurons, 1, label="VS")
            # output weights of each T4/T5 subtype onto the tangential cells
            lp = {"a": (hs, g.hs_exc), "b": (hs, -g.hs_inh), "d": (vs, g.vs_exc), "c": (vs, -g.vs_inh)}
            for i, d in enumerate(DIRECTIONS):
                for name, offset, gain in (("T4", 0, g.on[d]), ("T5", 4, g.off[d])):
                    # positive encoders + thresholds -> half-wave rectifying cells
                    ens = nengo.Ensemble(n_neurons, 1, label=f"{name}{d}", **rect)
                    nengo.Connection(lam[offset + i], ens, transform=gain, synapse=0.005)
                    post, w = lp[d]
                    nengo.Connection(ens, post, function=_relu, transform=w, synapse=0.01)

            lc10 = nengo.Ensemble(4 * n_neurons, 2, radius=1.4, label="LC10")
            nengo.Connection(lam[8:10], lc10, transform=g.position, synapse=0.01)

            out = nengo.Node(self._store, size_in=4, label="readout")
            nengo.Connection(hs, out[0], synapse=0.02)
            nengo.Connection(vs, out[1], synapse=0.02)
            nengo.Connection(lc10, out[2:4], synapse=0.02)
        self.network = net
        self.sim = nengo.Simulator(net, dt=dt, progress_bar=False)

    def _store(self, t, x):
        self._out[:] = x

    def step(self, stim: np.ndarray, duration: float) -> np.ndarray:
        """Run for ``duration`` seconds with ``stim``; return ``[HS, VS, pos_x, pos_y]``."""
        self._stim[:] = stim
        self.sim.run_steps(max(1, int(round(duration / self.dt))))
        return self._out.copy()

    def close(self) -> None:
        self.sim.close()


class RateOpticLobe:
    """Non-spiking NumPy equivalent of :class:`OpticLobeNetwork` (no Nengo needed)."""

    def __init__(self, gains: CircuitGains, tau: float = 0.02, **_):
        self.gains = gains
        self.tau = tau
        self._out = np.zeros(4)

    def step(self, stim: np.ndarray, duration: float) -> np.ndarray:
        g = self.gains
        lam = lamina(np.asarray(stim, dtype=float))
        t4 = _relu(np.array([g.on[d] for d in DIRECTIONS]) * lam[0:4])
        t5 = _relu(np.array([g.off[d] for d in DIRECTIONS]) * lam[4:8])
        a, b, c, d = t4 + t5
        target = np.array([
            g.hs_exc * a - g.hs_inh * b,
            g.vs_exc * d - g.vs_inh * c,
            *(g.position * lam[8:10]),
        ])
        target[:2] = np.clip(target[:2], -1.0, 1.0)
        alpha = 1.0 - np.exp(-duration / self.tau)
        self._out += alpha * (target - self._out)
        return self._out.copy()

    def close(self) -> None:
        pass


def build_optic_lobe(gains: CircuitGains, backend: str = "nengo", **kwargs):
    if backend == "nengo":
        try:
            return OpticLobeNetwork(gains, **kwargs)
        except ImportError:
            log.warning("nengo not installed; falling back to the rate model")
    return RateOpticLobe(gains, **kwargs)
