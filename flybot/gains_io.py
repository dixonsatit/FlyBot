"""Save / load the connectome-derived gains, so a deployment doesn't need the Codex CSVs.

    python -m flybot.gains_io --data-dir data/codex --side R --out gains.json

The bridge then starts with ``--gains gains.json`` (a few hundred bytes, e.g. a
Kubernetes ConfigMap) instead of loading ~70 MB of connections at every start.
The numbers are derived from FlyWire Codex data: keep its attribution and licence
terms in mind before publishing the file.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .looming import LoomingGains, derive_looming_gains
from .optic_lobe import CircuitGains, derive_gains

FORMAT = 1


def save_gains(path: str | Path, optic: CircuitGains, looming: LoomingGains, source: str, side: str) -> None:
    Path(path).write_text(json.dumps({"format": FORMAT, "source": source, "side": side,
                                      "optic": asdict(optic), "looming": asdict(looming)}, indent=2))


def load_gains(path: str | Path) -> tuple[CircuitGains, LoomingGains, dict]:
    data = json.loads(Path(path).read_text())
    if data.get("format") != FORMAT:
        raise ValueError(f"{path}: unsupported gains format {data.get('format')!r}")
    meta = {k: data.get(k) for k in ("source", "side")}
    return CircuitGains(**data["optic"]), LoomingGains(**data["looming"]), meta


def main(argv: list[str] | None = None) -> None:
    from .controller import load_connectome

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", help="FlyWire Codex CSV folder (synthetic connectome if omitted)")
    ap.add_argument("--side", default="R", choices=["L", "R"])
    ap.add_argument("--out", default="gains.json")
    args = ap.parse_args(argv)
    connectome = load_connectome(args.data_dir, side=args.side)
    optic = derive_gains(connectome.group_adjacency())
    looming = derive_looming_gains(connectome.group_adjacency(neuropils=None))
    save_gains(args.out, optic, looming, connectome.source, args.side)
    print(f"wrote {args.out} ({connectome.source}, side {args.side})")


if __name__ == "__main__":
    main()
