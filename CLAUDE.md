# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

FlyBot drives an M5Stack StackChan (CoreS3) from fruit-fly brain circuits: FlyWire Codex connectome data → Nengo (spiking LIF) network → servo/face/audio commands over MQTT JSON. README.md (Thai) documents MQTT payloads, Codex data setup and the CoreS3 firmware side.

## Commands

Local env is `.venv/` (Python 3.14; there is no `python` on PATH, so use `.venv/bin/python` / `.venv/bin/pytest`).

```bash
pip install -r requirements.txt            # or: pip install -e .[dev,fast]
pytest -q                                  # all tests
pytest -q tests/test_flybot.py::test_central_complex_ring   # single test
pytest -q -k "rate"                        # parametrized backend subset

python -m flybot.sim                       # offline scene simulation (no robot/broker)
python -m flybot.mqtt_bridge --host <broker> --data-dir data/codex [--backend nengo|rate] [--side L|R]
python tools/fake_stackchan.py --host <broker>   # fake robot publishing sensors
```

To test end to end locally, start `mosquitto` (Homebrew, `/opt/homebrew/sbin/mosquitto`) on `127.0.0.1:1883`, then the bridge, then `fake_stackchan.py` (it runs the `sim.scene` for 30 s and prints the received commands). Use `mosquitto_sub -t 'stackchan/#' -v` to capture traffic.

Run `pytest -q` before every commit. `pyproject.toml` sets `addopts = "-p no:nengo"` — Nengo's pytest plugin is disabled on purpose. There is no linter configured. Brian2 is not used (incompatible with numpy ≥ 2).

## Architecture

Pipeline per control tick (`BrainController.step` in `flybot/controller.py`, called at `--rate` Hz by `mqtt_bridge.StackChanBridge.run`):

1. **Connectome → gains** (startup only). `connectome.load_codex` streams Codex `connections*.csv(.gz)` in chunks, keeps optic-lobe neuropils (`LA, ME, AME, LO, LOP`) of one hemisphere, and maps root ids to functional groups via `GROUP_PATTERNS` (ordered regexes, first match wins). `group_adjacency()` collapses synapses into a `pre_group × post_group` DataFrame. `optic_lobe.derive_gains` converts input fractions along specific paths (L1→ON medulla→T4, L2→OFF medulla→T5, T4/T5→HS/VS, T4/T5→LPi→HS/VS) into a `CircuitGains` dataclass, normalising each kind separately and filling missing pathways with the mean/unit gain (logged as a warning). Without `--data-dir`, `synthetic_codex()` provides a hand-tuned stand-in, so everything runs with no downloaded data.
2. **Optic lobe** (`optic_lobe.py`). Stimulus vector is `[x, y, vx, vy, polarity]` (normalised); `lamina()` splits it into 8 ON/OFF directional channels + position. Two interchangeable backends with the same `step(stim, duration) -> [HS, VS, LC10x, LC10y]` / `close()` interface: `OpticLobeNetwork` (persistent `nengo.Simulator`, stepped incrementally via `run_steps`, I/O through closure-bound `_stim`/`_out` arrays) and `RateOpticLobe` (pure NumPy). `build_optic_lobe` falls back to the rate model if Nengo is missing. Changes to circuit structure must be mirrored in both backends — tests parametrize over both.
3. **Central complex** (`central_complex.py`). E-PG ring bump integrates IMU yaw; heading change produces VOR counter-rotation of pan; FC2 goal is stored while a target is visible and PFL3 steering returns the gaze when it is lost.
4. **Mushroom body** (`mushroom_body.py`). `Percept` → sparse KC code → habituating MBON familiarity; dopamine/octopamine/sleep pressure select the expression (`alert > happy > sleepy > curious`). Audio is emitted only when the expression changes.

Controller details that span modules: an efference copy of the previous pan/tilt rate is added back to image velocity before the optic lobe (to cancel self-motion); image +y is down so tilt is negated; IMU yaw sign is flipped via `ControllerConfig.imu_yaw_sign`. All tunables (FOV, gains, limits, deadband, backend) live in `ControllerConfig`. `SensorState` holds the latest async MQTT messages; the controller estimates `vx, vy` from successive frames when the camera payload omits them.

## Firmware (`firmware/stackchan/`)

PlatformIO project for CoreS3 + SG90 (`pio run` to build; `src/flybot_config.h` is gitignored and falls back to `flybot_config.example.h` with a `#warning`). The config header is deliberately not named `config.h`, because a dependency ships its own `config.h` that would shadow it. PlatformIO comes from Homebrew (`pio`).

- The camera is initialised with `esp_camera_init` directly (the released M5CoreS3 1.0.1 `GC0308::begin()` takes no config). Camera SCCB shares the internal I2C bus with IMU/LTR-553/touch: `M5.In_I2C.release()` before init and `M5.In_I2C.begin()` after it. Never call `sensor_t` setters later.
- Motion detection is frame differencing on 160×120 grayscale. The payload carries `width/height`, so the controller normalises it independently of `ControllerConfig.frame_width`.
- The command JSON with telemetry exceeds PubSubClient's 256 B default buffer, hence `setBufferSize(1024)`.

## Data and naming conventions

- Codex files go in a gitignored `data/` dir (e.g. `data/codex/`). The loader accepts column aliases (`_CONN_ALIASES`) and several cell-type tables (`visual_neuron_types`, `consolidated_cell_types`, `cell_types`, `classification`).
- Confirmed Codex v783 (FAFB) headers: `visual_neuron_types` = `root_id,type,family,subsystem,category,side`; `consolidated_cell_types` = `root_id,primary_type,additional_type(s)`. `connections_princeton.csv.gz` (~68 MB, 5.3M rows) = `pre_root_id,post_root_id,neuropil,syn_count,nt_type`; neuropils are side-suffixed (`ME_R`, `LOP_L`, `AME_R`, ...); nt_type ∈ `ACH/GABA/GLUT/DA/SER/OCT` (the last three get +1 in signed mode). The cell-type table is whole-brain, not per side; the side filter applies only to connections. With real data every group in `GROUP_PATTERNS` has synapses on both sides and `derive_gains` logs no warnings. Loading takes a few seconds.
- v783 type names: HS = `HSE/HSN/HSS`, VS = `VS1–8/VSm`, LC10 = `LC10a–f` (all matched by prefix regexes). LPi cells are `LPi01..LPi15` with no layer info, so they form a single `LPi` group; the opponent paths onto HS/VS are selected by synapse counts. Keep `_SYNTHETIC_TYPE_NAME` in sync if group naming changes.
- `NT_SIGN` treats glutamate as inhibitory (GluCl) for `signed=True` adjacency.
- `write_codex_csv` exports a `Connectome` in Codex CSV format (used for round-trip tests).
