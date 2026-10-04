# CHANGELOG

<!-- version list -->

## v0.4.0 (2026-10-04)

### Features

- **deploy**: Run the bridge on Kubernetes
  ([`c40fbbf`](https://github.com/dixonsatit/FlyBot/commit/c40fbbf2b57fc08b802f13a2b11f86b82af882bc))


## v0.3.0 (2026-10-04)

### Continuous Integration

- Mark GitHub releases as pre-release until tested on the robot
  ([`e374039`](https://github.com/dixonsatit/FlyBot/commit/e374039a5faa87c77b4c0f8af1ae8b4a834fb4fd))

### Features

- **firmware**: Boot self-test and Thai speech-balloon font
  ([`c8dde09`](https://github.com/dixonsatit/FlyBot/commit/c8dde095a31626f9c671c631fa67942ee502cbae))


## v0.2.0 (2026-10-04)

### Features

- **calendar**: Remind upcoming meetings from ICS calendars
  ([`3e0b356`](https://github.com/dixonsatit/FlyBot/commit/3e0b3569ffd425b594654e59bb3b2e584d8f592c))


## v0.1.0 (2026-10-04)

First pre-release: FlyWire Codex (FAFB v783) fly circuits simulated with Nengo, driving an
M5Stack StackChan (CoreS3) over MQTT. Software tested with the real connectome, a real
broker and a real LLM; not yet run on the robot.

- Optic lobe (L1/L2 -> T4/T5 -> HS/VS, LC10) with connectome-derived gains; Nengo LIF and rate backends
- Central complex: E-PG heading, VOR, FC2 goal + PFL3 return
- Mushroom body: habituation, dopamine/octopamine -> expression
- Looming escape: LPLC2 / LC4 -> Giant Fiber (DNp01)
- Phototaxis, `--personality` presets, events (MQTT + webhook), attention game
- Camera latency compensation; simulated StackChan plant and `flybot.tune`
- LLM cortex (Claude or OpenAI-compatible): narration, chat with robot tools, vision
- CoreS3 + SG90 firmware (PlatformIO)
