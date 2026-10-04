"""Load FlyWire Codex exports and collapse them into cell-type adjacency matrices.

FlyWire Codex (https://codex.flywire.ai, "Download Data") provides, among others:

* ``connections*.csv.gz``  - columns ``pre_root_id, post_root_id, neuropil, syn_count, nt_type``
* ``visual_neuron_types*.csv.gz`` - optic-lobe cell types (``root_id, type, family, ...``)
* ``consolidated_cell_types*.csv.gz`` / ``classification*.csv.gz`` - whole-brain types

Only the optic-lobe neuropils (LA, ME, AME, LO, LOP) of one hemisphere are kept,
then every neuron is mapped to one of the functional groups in ``GROUP_PATTERNS``
and synapse counts are summed into a ``pre_group x post_group`` matrix.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

OPTIC_LOBE_NEUROPILS = ("LA", "ME", "AME", "LO", "LOP")

# Ordered (group, regex on cell type); first match wins.
GROUP_PATTERNS: list[tuple[str, str]] = [
    ("L1", r"^L1$"),
    ("L2", r"^L2$"),
    ("L3", r"^L3$"),
    # ON pathway medulla interneurons -> T4
    ("Mi1", r"^Mi1$"),
    ("Tm3", r"^Tm3$"),
    ("Mi4", r"^Mi4$"),
    ("Mi9", r"^Mi9$"),
    # OFF pathway medulla interneurons -> T5
    ("Tm1", r"^Tm1$"),
    ("Tm2", r"^Tm2$"),
    ("Tm4", r"^Tm4$"),
    ("Tm9", r"^Tm9$"),
    # Direction-selective cells, one subtype per lobula-plate layer
    *[(f"T4{d}", rf"^T4{d}$") for d in "abcd"],
    *[(f"T5{d}", rf"^T5{d}$") for d in "abcd"],
    # Lobula-plate intrinsic (opponent inhibition) and tangential cells
    ("LPi_h", r"^LPi\s*2"),
    ("LPi_v", r"^LPi\s*[34]"),
    ("HS", r"^HS"),
    ("VS", r"^VS"),
    # Lobula columnar object-tracking pathway
    ("LC10", r"^LC10"),
]

# Sign of a synapse by predicted neurotransmitter. Fly glutamate acts mostly
# through the inhibitory GluCl channel, so it is treated as inhibitory.
NT_SIGN = {"ACH": 1.0, "GABA": -1.0, "GLUT": -1.0}

_CONN_ALIASES = {
    "pre_root_id": "pre", "pre_pt_root_id": "pre", "pre": "pre",
    "post_root_id": "post", "post_pt_root_id": "post", "post": "post",
    "neuropil": "neuropil",
    "syn_count": "syn_count", "count": "syn_count",
    "nt_type": "nt_type", "nt": "nt_type",
}
_TYPE_COLUMNS = ("type", "primary_type", "cell_type", "hemibrain_type")


@dataclass
class Connectome:
    """Optic-lobe synapses plus a ``root_id -> cell type`` mapping."""

    connections: pd.DataFrame  # columns: pre, post, neuropil, syn_count, nt_type
    cell_types: pd.Series  # index: root_id, values: cell type name
    source: str

    def groups(self, patterns: list[tuple[str, str]] = GROUP_PATTERNS) -> pd.Series:
        """Map every typed root id to its functional group (unmatched ids dropped)."""
        types = self.cell_types.astype(str)
        group = pd.Series(pd.NA, index=types.index, dtype="object")
        for name, pattern in patterns:
            hit = group.isna() & types.str.match(pattern)
            group[hit] = name
        return group.dropna()

    def group_adjacency(
        self, patterns: list[tuple[str, str]] = GROUP_PATTERNS, signed: bool = False
    ) -> pd.DataFrame:
        """Sum synapses into a square ``pre_group x post_group`` matrix.

        With ``signed=True`` each synapse is weighted by ``NT_SIGN`` of its
        neurotransmitter prediction.
        """
        group = self.groups(patterns)
        df = self.connections.assign(
            pre_g=self.connections["pre"].map(group),
            post_g=self.connections["post"].map(group),
        ).dropna(subset=["pre_g", "post_g"])
        weight = df["syn_count"].astype(float)
        if signed:
            weight = weight * df["nt_type"].map(NT_SIGN).fillna(1.0)
        adj = (
            df.assign(w=weight)
            .pivot_table(index="pre_g", columns="post_g", values="w", aggfunc="sum", fill_value=0.0)
        )
        names = [n for n, _ in patterns]
        return adj.reindex(index=names, columns=names, fill_value=0.0)


def _find(data_dir: Path, prefixes: tuple[str, ...]) -> Path | None:
    for prefix in prefixes:
        hits = sorted(data_dir.glob(f"{prefix}*.csv*"))
        if hits:
            return hits[0]
    return None


def _neuropil_regex(neuropils: tuple[str, ...], side: str | None) -> re.Pattern:
    suffix = f"_{side}" if side else r"(_[LR])?"
    return re.compile(rf"^({'|'.join(neuropils)}){suffix}$")


def load_codex(
    data_dir: str | Path,
    side: str | None = "R",
    neuropils: tuple[str, ...] = OPTIC_LOBE_NEUROPILS,
    min_syn: int = 1,
    chunksize: int = 2_000_000,
) -> Connectome:
    """Read Codex CSV exports from ``data_dir`` and keep optic-lobe synapses of ``side``."""
    data_dir = Path(data_dir)
    conn_path = _find(data_dir, ("connections",))
    type_path = _find(data_dir, ("visual_neuron_types", "consolidated_cell_types", "classification"))
    if conn_path is None or type_path is None:
        raise FileNotFoundError(
            f"Expected connections*.csv(.gz) and a cell-type table in {data_dir}; "
            "download them from https://codex.flywire.ai (Download Data)."
        )

    pil = _neuropil_regex(neuropils, side)
    chunks = []
    for chunk in pd.read_csv(conn_path, chunksize=chunksize):
        chunk = chunk.rename(columns={c: _CONN_ALIASES[c] for c in chunk.columns if c in _CONN_ALIASES})
        if "nt_type" not in chunk:
            chunk["nt_type"] = "ACH"
        keep = chunk["neuropil"].astype(str).str.match(pil) & (chunk["syn_count"] >= min_syn)
        chunks.append(chunk.loc[keep, ["pre", "post", "neuropil", "syn_count", "nt_type"]])
    connections = pd.concat(chunks, ignore_index=True)

    types = pd.read_csv(type_path)
    type_col = next((c for c in _TYPE_COLUMNS if c in types.columns), None)
    if type_col is None:
        raise ValueError(f"No cell-type column ({', '.join(_TYPE_COLUMNS)}) in {type_path}")
    cell_types = types.dropna(subset=[type_col]).drop_duplicates("root_id").set_index("root_id")[type_col]

    log.info("Loaded %d optic-lobe connections from %s", len(connections), conn_path.name)
    return Connectome(connections, cell_types, source=str(data_dir))


# --- synthetic stand-in -----------------------------------------------------

# (pre group, post group, mean synapses per post cell, neuropil). Rough
# magnitudes after the medulla/lobula-plate EM literature; only meant to let the
# pipeline run before the real Codex files are downloaded.
_SYNTHETIC_EDGES: list[tuple[str, str, float, str]] = [
    ("L1", "Mi1", 90, "ME"), ("L1", "Tm3", 60, "ME"), ("L3", "Mi9", 40, "ME"),
    ("L2", "Tm1", 60, "ME"), ("L2", "Tm2", 70, "ME"), ("L2", "Tm4", 30, "ME"),
    ("L3", "Tm9", 45, "ME"), ("L1", "Mi4", 25, "ME"),
    *[(m, f"T4{d}", n * s, "ME") for d, s in zip("abcd", (1.0, 0.95, 0.9, 1.05))
      for m, n in (("Mi1", 35), ("Tm3", 15), ("Mi4", 15), ("Mi9", 12))],
    *[(m, f"T5{d}", n * s, "LO") for d, s in zip("abcd", (1.0, 0.9, 1.0, 1.1))
      for m, n in (("Tm1", 20), ("Tm2", 25), ("Tm4", 10), ("Tm9", 25))],
    ("T4a", "HS", 40, "LOP"), ("T5a", "HS", 35, "LOP"),
    ("T4d", "VS", 40, "LOP"), ("T5d", "VS", 38, "LOP"),
    ("T4b", "LPi_h", 30, "LOP"), ("T5b", "LPi_h", 28, "LOP"), ("LPi_h", "HS", 25, "LOP"),
    ("T4c", "LPi_v", 30, "LOP"), ("T5c", "LPi_v", 30, "LOP"), ("LPi_v", "VS", 25, "LOP"),
    ("Tm2", "LC10", 20, "LO"), ("Tm3", "LC10", 10, "LO"),
]
_SYNTHETIC_NT = {"L1": "GLUT", "Mi4": "GABA", "Mi9": "GLUT", "LPi_h": "GLUT", "LPi_v": "GLUT"}
_SYNTHETIC_TYPE_NAME = {"LPi_h": "LPi2-1", "LPi_v": "LPi4-3"}


def synthetic_codex(n_per_type: int = 6, side: str = "R", seed: int = 0) -> Connectome:
    """Build a small Codex-shaped connectome for offline testing and demos."""
    rng = np.random.default_rng(seed)
    names = [n for n, _ in GROUP_PATTERNS]
    ids = {g: 720575940600000000 + 1000 * i + np.arange(n_per_type) for i, g in enumerate(names)}
    rows = []
    for pre_g, post_g, mean, pil in _SYNTHETIC_EDGES:
        for post in ids[post_g]:
            counts = rng.poisson(mean / n_per_type, size=n_per_type)
            for pre, c in zip(ids[pre_g], counts):
                if c > 0:
                    rows.append((pre, post, f"{pil}_{side}", int(c), _SYNTHETIC_NT.get(pre_g, "ACH")))
    connections = pd.DataFrame(rows, columns=["pre", "post", "neuropil", "syn_count", "nt_type"])
    cell_types = pd.Series(
        {rid: _SYNTHETIC_TYPE_NAME.get(g, g) for g, arr in ids.items() for rid in arr}, name="type"
    )
    cell_types.index.name = "root_id"
    return Connectome(connections, cell_types, source="synthetic")


def write_codex_csv(connectome: Connectome, out_dir: str | Path) -> None:
    """Write a connectome in the Codex download layout (used for tests/demos)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    connectome.connections.rename(
        columns={"pre": "pre_root_id", "post": "post_root_id"}
    ).to_csv(out_dir / "connections.csv.gz", index=False)
    connectome.cell_types.rename("type").reset_index().to_csv(
        out_dir / "visual_neuron_types.csv.gz", index=False
    )
