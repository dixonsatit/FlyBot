# CHANGELOG

<!-- version list -->

## v0.13.1 (2026-10-09)

### Bug Fixes

- **firmware**: Keep the monitor page within the robot's 16 sockets
  ([`b529737`](https://github.com/dixonsatit/FlyBot/commit/b5297373afeed036da04f412d228b359aea143f4))


## v0.13.0 (2026-10-09)

### Features

- **firmware**: Monitor page, live tuning and a second WiFi via a VPN relay
  ([`c755371`](https://github.com/dixonsatit/FlyBot/commit/c755371f0a6021fc90315b5f180e440c7092800d))


## v0.12.1 (2026-10-09)

### Performance Improvements

- **voice**: Shorter spoken replies and 16 kHz audio back to the robot
  ([`1c8509a`](https://github.com/dixonsatit/FlyBot/commit/1c8509a78ddda216c0c77d5469fd1a88efdfc6c6))


## v0.12.0 (2026-10-09)

### Features

- **voice**: Hands-free น้องหวี่, head pats and a friendlier face
  ([`6b09256`](https://github.com/dixonsatit/FlyBot/commit/6b09256acd6b109a9655f859d21c41e27ee8d5be))


## v0.11.0 (2026-10-09)

### Features

- **firmware**: Camera monitor and calmer motion detection
  ([`4627ddc`](https://github.com/dixonsatit/FlyBot/commit/4627ddcb05f1bf494df51446b61e75314bb1cede))


## v0.10.0 (2026-10-09)

### Bug Fixes

- **firmware**: Reliable StackChan servos, camera and quieter face
  ([`099b967`](https://github.com/dixonsatit/FlyBot/commit/099b96744c0dfa766acd69f5d54d3fb6aac1f101))

### Features

- **firmware**: Support M5Stack's official StackChan body
  ([`f3fa3f2`](https://github.com/dixonsatit/FlyBot/commit/f3fa3f22855fea878411613537005f6b040a6336))

- **voice**: Talk to the robot in Thai by holding its screen
  ([`c960f60`](https://github.com/dixonsatit/FlyBot/commit/c960f6071e7437f3ffb663c735f96b56f095534e))


## v0.9.0 (2026-10-05)

### Features

- **twin**: Live Q4U queue counts following the wall clock
  ([`a592c4c`](https://github.com/dixonsatit/FlyBot/commit/a592c4c612df5eb3e291e102443a08b3ce85da68))


## v0.8.0 (2026-10-05)

### Features

- **twin**: Replay real per-minute queue arrivals from Q4U
  ([`0a92961`](https://github.com/dixonsatit/FlyBot/commit/0a929611e8301e62f1145241017295a38e2117f2))


## v0.7.0 (2026-10-05)

### Features

- **twin**: Hospital digital twin navigated by the whole fly brain
  ([`96aa4aa`](https://github.com/dixonsatit/FlyBot/commit/96aa4aa7d22d6fea71610659339ca81ef79f124b))


## v0.6.0 (2026-10-05)

### Features

- **voice**: Thai speech in and out through the hospital's own services
  ([`16cd641`](https://github.com/dixonsatit/FlyBot/commit/16cd641b706c8a6e77962aa2e6ae84b7aeae07e1))


## v0.5.0 (2026-10-04)

### Features

- **dashboard**: Web dashboard with simulated robot and Thai voice
  ([`6431c31`](https://github.com/dixonsatit/FlyBot/commit/6431c3198145112081af446d12f370a82103f3b3))


## v0.4.1 (2026-10-04)

### Bug Fixes

- **deploy**: Strip newlines from the MQTT password and log refused connections
  ([`fe2ffc9`](https://github.com/dixonsatit/FlyBot/commit/fe2ffc9e945b16182275423fa05f6e908151db4b))


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
