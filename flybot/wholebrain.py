"""Whole-brain spiking model of the FlyWire connectome (all ~138k neurons).

Every neuron is a leaky integrate-and-fire unit; every connection is the summed
synapse count between two neurons, signed by the predicted transmitter (acetylcholine
excites, GABA and glutamate inhibit; dopamine, serotonin and octopamine act slowly
and are left out of this fast model). The parameters follow the published
whole-brain LIF model of Drosophila (Shiu et al., Nature 2024) and are tunable:
there is no learning, so the connectome alone shapes the response.

Inputs are spikes injected into chosen neurons (e.g. photoreceptors of one eye at a
Poisson rate); outputs are spike counts, e.g. of descending neurons that steer.

    brain = WholeBrain.load("data/codex")            # cached to data/codex/wholebrain.npz
    eye_l = brain.select(type_prefix="R1-6", side="L")
    counts = brain.run(500, inputs={eye_l: 100.0})   # 500 ms, 100 Hz Poisson drive
    brain.rates(counts, brain.select(type="DNa02", side="R"), 500)
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

SIGN = {"ACH": 1.0, "GABA": -1.0, "GLUT": -1.0}  # others (DA, SER, OCT): neuromodulators, 0 here
CACHE = "wholebrain.npz"


@dataclass
class LIFParams:
    v_rest: float = -52.0  # mV
    v_reset: float = -52.0
    v_threshold: float = -45.0
    tau_m: float = 20.0  # ms
    tau_syn: float = 5.0
    refractory: float = 2.2
    delay: float = 1.8  # synaptic delay
    w_syn: float = 0.275  # mV per synapse
    dt: float = 0.1  # ms; 0.5 trades accuracy for ~5x speed


class WholeBrain:
    def __init__(self, ids: np.ndarray, indptr: np.ndarray, indices: np.ndarray, weights: np.ndarray,
                 types: np.ndarray, sides: np.ndarray, params: LIFParams | None = None, seed: int = 0):
        # CSC-like adjacency by presynaptic neuron: post targets of neuron j are
        # indices[indptr[j]:indptr[j+1]] with signed synapse counts weights[...]
        self.ids, self.indptr, self.indices, self.weights = ids, indptr, indices, weights
        self.types, self.sides = types, sides
        self.p = params or LIFParams()
        self.rng = np.random.default_rng(seed)
        self._index = {int(i): k for k, i in enumerate(ids)}
        self.reset()

    @property
    def n(self) -> int:
        return len(self.ids)

    # -- construction ------------------------------------------------------------
    @classmethod
    def load(cls, data_dir: str | Path, params: LIFParams | None = None, rebuild: bool = False) -> "WholeBrain":
        data_dir = Path(data_dir)
        cache = data_dir / CACHE
        if cache.exists() and not rebuild:
            z = np.load(cache, allow_pickle=False)
            return cls(z["ids"], z["indptr"], z["indices"], z["weights"], z["types"], z["sides"], params)
        brain = cls.build(data_dir, params)
        np.savez_compressed(cache, ids=brain.ids, indptr=brain.indptr, indices=brain.indices,
                            weights=brain.weights, types=brain.types, sides=brain.sides)
        log.info("cached whole brain to %s", cache)
        return brain

    @classmethod
    def build(cls, data_dir: str | Path, params: LIFParams | None = None) -> "WholeBrain":
        import pandas as pd
        from .connectome import _find, _TYPE_COLUMNS, _TYPE_TABLES

        data_dir = Path(data_dir)
        t0 = time.monotonic()
        c = pd.read_csv(_find(data_dir, ("connections",)),
                        usecols=["pre_root_id", "post_root_id", "neuropil", "syn_count", "nt_type"])
        ids = pd.unique(np.concatenate([c.pre_root_id.to_numpy(), c.post_root_id.to_numpy()]))
        ids.sort()
        pre = np.searchsorted(ids, c.pre_root_id.to_numpy())
        post = np.searchsorted(ids, c.post_root_id.to_numpy())
        w = c.syn_count.to_numpy(dtype=np.float32) * c.nt_type.map(SIGN).fillna(0.0).to_numpy(dtype=np.float32)

        # sum parallel rows (one pair can appear in several neuropils), sort by presynaptic neuron
        key = pre.astype(np.int64) * len(ids) + post
        order = np.argsort(key, kind="stable")
        key, w = key[order], w[order]
        uniq, start = np.unique(key, return_index=True)
        w = np.add.reduceat(w, start)
        keep = w != 0
        uniq, w = uniq[keep], w[keep]
        pre_u, post_u = uniq // len(ids), uniq % len(ids)
        indptr = np.zeros(len(ids) + 1, dtype=np.int64)
        np.add.at(indptr, pre_u + 1, 1)
        indptr = np.cumsum(indptr)

        # side: where most of the neuron's synapses (in or out) are, by neuropil suffix _L / _R
        side_of = c.neuropil.astype(str).str.extract(r"_([LR])$")[0].map({"L": -1, "R": 1}).fillna(0).to_numpy()
        score = np.zeros(len(ids))
        np.add.at(score, pre, side_of * c.syn_count.to_numpy())
        np.add.at(score, post, side_of * c.syn_count.to_numpy())
        sides = np.where(score > 0, "R", np.where(score < 0, "L", "C"))

        types = np.full(len(ids), "", dtype=object)
        for prefix in reversed(_TYPE_TABLES):  # earlier tables win, as in load_codex
            path = _find(data_dir, (prefix,))
            if path is None:
                continue
            table = pd.read_csv(path)
            col = next((k for k in _TYPE_COLUMNS if k in table.columns), None)
            if col is None:
                continue
            table = table.dropna(subset=[col])
            pos = np.searchsorted(ids, table.root_id.to_numpy())
            ok = (pos < len(ids)) & (ids[np.minimum(pos, len(ids) - 1)] == table.root_id.to_numpy())
            types[pos[ok]] = table[col].astype(str).to_numpy()[ok]
        log.info("built whole brain: %d neurons, %d connections in %.1fs", len(ids), len(w), time.monotonic() - t0)
        return cls(ids, indptr, post_u.astype(np.int32), w.astype(np.float32),
                   types.astype(str), sides.astype(str), params)

    # -- selection ----------------------------------------------------------------
    def select(self, type: str | None = None, type_prefix: str | None = None, pattern: str | None = None,
               side: str | None = None) -> tuple:
        """Indices of neurons by exact type, prefix or regex, optionally one side ('L'/'R')."""
        mask = np.ones(self.n, dtype=bool)
        if type is not None:
            mask &= self.types == type
        if type_prefix is not None:
            mask &= np.char.startswith(self.types.astype(str), type_prefix)
        if pattern is not None:
            rx = re.compile(pattern)
            mask &= np.array([bool(rx.search(t)) for t in self.types])
        if side is not None:
            mask &= self.sides == side
        return tuple(np.flatnonzero(mask).tolist())

    # -- simulation -----------------------------------------------------------------
    def reset(self) -> None:
        p = self.p
        self.v = np.full(self.n, p.v_rest, dtype=np.float32)
        self.g = np.zeros(self.n, dtype=np.float32)
        self.refr = np.zeros(self.n, dtype=np.float32)
        self._delay_steps = max(1, int(round(p.delay / p.dt)))
        self._queue: list[np.ndarray] = [np.empty(0, dtype=np.int64)] * self._delay_steps

    def _deliver(self, spikes: np.ndarray) -> None:
        if not len(spikes):
            return
        starts, ends = self.indptr[spikes], self.indptr[spikes + 1]
        lengths = ends - starts
        total = int(lengths.sum())
        if not total:
            return
        # gather all outgoing edges of the spiking neurons without a Python loop
        offsets = np.repeat(starts - np.concatenate([[0], np.cumsum(lengths)[:-1]]), lengths)
        edges = np.arange(total) + offsets
        self.g += np.bincount(self.indices[edges], self.weights[edges], minlength=self.n).astype(np.float32) * self.p.w_syn

    def run(self, ms: float, inputs: dict[tuple, float] | None = None, probe: tuple | None = None) -> np.ndarray:
        """Simulate ``ms`` milliseconds; ``inputs`` maps neuron index tuples to Poisson rates (Hz)
        injected as spikes. Returns spike counts per neuron. With ``probe``, ``self.drive`` holds
        those neurons' mean synaptic drive (mV) over the run: a continuous readout that is far
        less noisy than counting the few spikes of a single descending neuron."""
        p = self.p
        steps = int(round(ms / p.dt))
        counts = np.zeros(self.n, dtype=np.int32)
        drive = [(np.asarray(idx, dtype=np.int64), rate * p.dt / 1000.0) for idx, rate in (inputs or {}).items() if idx]
        decay_m, decay_s = np.float32(p.dt / p.tau_m), np.float32(np.exp(-p.dt / p.tau_syn))
        probe_idx = np.asarray(probe or (), dtype=np.int64)
        drive_sum = np.zeros(len(probe_idx))
        for step in range(steps):
            self._deliver(self._queue[step % self._delay_steps])  # spikes sent one delay ago arrive
            self.g *= decay_s
            self.v += decay_m * (self.g - (self.v - p.v_rest))
            active = self.refr <= 0
            self.v[~active] = p.v_reset
            self.refr -= p.dt
            fired = np.flatnonzero(active & (self.v >= p.v_threshold))
            for idx, prob in drive:  # sensory drive: Poisson spikes in the stimulated neurons
                fired = np.union1d(fired, idx[self.rng.random(len(idx)) < prob])
            self.v[fired] = p.v_reset
            self.refr[fired] = p.refractory
            counts[fired] += 1
            self._queue[step % self._delay_steps] = fired
            if len(probe_idx):
                drive_sum += self.g[probe_idx]
        self.drive = drive_sum / max(steps, 1)
        return counts

    @staticmethod
    def rates(counts: np.ndarray, idx: tuple, ms: float) -> float:
        """Mean firing rate (Hz) of ``idx`` over a run of ``ms``."""
        return float(counts[list(idx)].mean() * 1000.0 / ms) if idx else 0.0
