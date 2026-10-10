# CHANGELOG

<!-- version list -->

## v0.32.2 (2026-10-10)

### Bug Fixes

- **faces**: The assistant knows it recognised the face itself
  ([`64c2f46`](https://github.com/dixonsatit/FlyBot/commit/64c2f46fda208093b1ca956e11b3336579bc734c))


## v0.32.1 (2026-10-10)

### Bug Fixes

- **firmware**: Allocate the PSRAM buffers at start-up, not at static initialisation
  ([`1eb248f`](https://github.com/dixonsatit/FlyBot/commit/1eb248fab64cf1bfd6703d72fd10f76996d995c8))


## v0.32.0 (2026-10-10)

### Features

- **faces**: Follow the face with the DGX detector, fine head moves, streamed camera frames
  ([`5c6b149`](https://github.com/dixonsatit/FlyBot/commit/5c6b149e35d7d50720b89be747ce2bf3c4ef437b))


## v0.31.0 (2026-10-10)

### Chores

- **k8s**: Keep the bridge off tyranus
  ([`5c69932`](https://github.com/dixonsatit/FlyBot/commit/5c699327d4ec44b62fb1cf3e237d9291a63bf061))

### Features

- **faces**: Enroll and test with the browser's webcam, or the robot camera
  ([`ee5ee47`](https://github.com/dixonsatit/FlyBot/commit/ee5ee4773d898f2ed4dbdfb9efb511cb8f9d8882))


## v0.30.0 (2026-10-10)

### Features

- **faces**: Recognise enrolled people and greet them by name
  ([`246f001`](https://github.com/dixonsatit/FlyBot/commit/246f0016cb7f75c9bd88c2e50bd9e7d2853c5fca))


## v0.29.0 (2026-10-10)

### Features

- **remote**: Stackchan.kkh.go.th for the robot away from the LAN, live camera on the dashboard
  ([`b70b2c1`](https://github.com/dixonsatit/FlyBot/commit/b70b2c1ace331e24e40ffa9bbd07db1a9e03d5d5))


## v0.28.0 (2026-10-10)

### Features

- **voice**: Wake-word recording session for training "น้องหวี่"
  ([`d11e562`](https://github.com/dixonsatit/FlyBot/commit/d11e5620fbd9d8e6ab32fa8b0564c1427c44df62))


## v0.27.1 (2026-10-10)

### Bug Fixes

- **robot**: Name handling after a 0.6 s look, swipes on release, no startle from a hand on the head
  ([`772af7e`](https://github.com/dixonsatit/FlyBot/commit/772af7e29bbe2ec6c3b77a9031ce8553dfa8bcba))


## v0.27.0 (2026-10-10)

### Features

- **robot**: React to being lifted, tilted or shaken; RGB ring, head swipes, battery and RTC
  ([`238712a`](https://github.com/dixonsatit/FlyBot/commit/238712af4b9bfd3135b61da73c6ee9e5ddd58206))


## v0.26.1 (2026-10-10)

### Bug Fixes

- **assistant**: น้องหวี่ is a boy and ends with ครับ
  ([`df42b54`](https://github.com/dixonsatit/FlyBot/commit/df42b540b41bb6f3add9927549473124b9f56643))

### Testing

- **presence**: Expect the welcome-back question with ครับ
  ([`b618b11`](https://github.com/dixonsatit/FlyBot/commit/b618b11b4b028d31d0f0c8ed9e7e3e321f7ed988))


## v0.26.0 (2026-10-10)

### Features

- **voice**: Say "searching" while the assistant looks something up, and "still working" on long
  waits
  ([`0a6f31b`](https://github.com/dixonsatit/FlyBot/commit/0a6f31bd48011e8a9247dfd1f0c0ddfcdcf34728))


## v0.25.0 (2026-10-10)

### Features

- **assistant**: On-device face tracking, speech-only listening and Google Maps places
  ([`66367d2`](https://github.com/dixonsatit/FlyBot/commit/66367d2fca922ad0e1f1c788683efc485e0e9fbf))


## v0.24.6 (2026-10-10)

### Bug Fixes

- **assistant**: The LLM knows what the camera sees and can look on request
  ([`3fdac0b`](https://github.com/dixonsatit/FlyBot/commit/3fdac0b1e64856859851e6e6d3f17083b4075040))


## v0.24.5 (2026-10-10)

### Bug Fixes

- **presence**: Turn to the person from the pose the frame was taken at
  ([`da6390c`](https://github.com/dixonsatit/FlyBot/commit/da6390c547635d29fc570ba3d0d4a380bafbf476))


## v0.24.4 (2026-10-10)

### Bug Fixes

- **presence**: Wait up to 10 s for a camera frame
  ([`8bdce9d`](https://github.com/dixonsatit/FlyBot/commit/8bdce9df4fde94da112a09dfe1940bec4d1d40f9))


## v0.24.3 (2026-10-10)

### Bug Fixes

- **presence**: Look back at the desk when nobody is in view, scan now and then
  ([`28d1b43`](https://github.com/dixonsatit/FlyBot/commit/28d1b43c28c5e3fb9cec9c28126cd40221a965f8))


## v0.24.2 (2026-10-10)

### Bug Fixes

- **presence**: Log every check; any speech means someone is here
  ([`3fa7979`](https://github.com/dixonsatit/FlyBot/commit/3fa79796989d80bde7fb82b160d9db60ffef1f4b))


## v0.24.1 (2026-10-10)

### Bug Fixes

- **presence**: Colour QVGA frames and a person seen from any body part
  ([`3a4b2fd`](https://github.com/dixonsatit/FlyBot/commit/3a4b2fd065058cac992e149f8022cf17a87cce6a))


## v0.24.0 (2026-10-10)

### Features

- **assistant**: Watch people, not motion; weather and PM2.5 where it is
  ([`4740b66`](https://github.com/dixonsatit/FlyBot/commit/4740b66f0c66bdae4a4f27acd6affdb404467a3b))


## v0.23.0 (2026-10-10)

### Features

- **voice**: --tts-speed for a calmer speaking rate
  ([`a46321d`](https://github.com/dixonsatit/FlyBot/commit/a46321d304d76f01010f7c97e13fd86b0604909a))


## v0.22.1 (2026-10-10)

### Bug Fixes

- **voice**: Greetings call the robot; longer follow-up at the desk; keep clips
  ([`338cafe`](https://github.com/dixonsatit/FlyBot/commit/338cafe038ab7e98732e8bfac9f7119e1872cb00))


## v0.22.0 (2026-10-10)

### Features

- **assistant**: Know when the user is at the desk
  ([`37e4ca0`](https://github.com/dixonsatit/FlyBot/commit/37e4ca0994af3b27727ee09df83066672c17a6f5))


## v0.21.0 (2026-10-10)

### Features

- **assistant**: Shorter spoken summaries
  ([`96b4354`](https://github.com/dixonsatit/FlyBot/commit/96b43542f836f9eaa6e8ba6be220c23dcebfef5e))


## v0.20.1 (2026-10-10)

### Bug Fixes

- **meetings**: Retry stalled feed downloads and reload soon after a failure
  ([`bc576aa`](https://github.com/dixonsatit/FlyBot/commit/bc576aaf82615d471e7cd0603536b3667d468cbe))


## v0.20.0 (2026-10-10)

### Features

- **assistant**: Calendar answers cover the next 7 days
  ([`f87ac82`](https://github.com/dixonsatit/FlyBot/commit/f87ac82fd506713960f4ee1cb1b2eb82aee82833))


## v0.19.3 (2026-10-10)

### Bug Fixes

- **assistant**: Keep tool actions in the chat history
  ([`0f59abc`](https://github.com/dixonsatit/FlyBot/commit/0f59abcabad1c0e021729a96d028e7410ffb0c46))


## v0.19.2 (2026-10-09)

### Bug Fixes

- **assistant**: A sentence starting with จำ/จด is saved even if the LLM only said so
  ([`0fce2f0`](https://github.com/dixonsatit/FlyBot/commit/0fce2f09af348c2a4bc8c07957fcdfc80c0dd342))


## v0.19.1 (2026-10-09)

### Bug Fixes

- **assistant**: Save facts said with จำ/จด and never claim a save that didn't happen
  ([`dc89eeb`](https://github.com/dixonsatit/FlyBot/commit/dc89eeb2f0b0f8e449d5a4f11db818fbff496969))


## v0.19.0 (2026-10-09)

### Features

- **assistant**: Notes and spoken reminders that survive restarts
  ([`bbddd98`](https://github.com/dixonsatit/FlyBot/commit/bbddd988c35308d8b908f40a92f4716747e2b250))


## v0.18.0 (2026-10-09)

### Features

- **voice**: More head-pat lines in the same spirit
  ([`e6b40b5`](https://github.com/dixonsatit/FlyBot/commit/e6b40b5b9cea725998d4e804ea0b10a83b39774d))


## v0.17.0 (2026-10-09)

### Features

- **voice**: Head pats say "โอยยยย ฟินจังเอาอีกๆ"
  ([`0668d83`](https://github.com/dixonsatit/FlyBot/commit/0668d832bc4f20b9117447f29e3ea32e7b1b6194))


## v0.16.3 (2026-10-09)

### Bug Fixes

- **voice**: Answer the name said alone, then listen for the question
  ([`ada4228`](https://github.com/dixonsatit/FlyBot/commit/ada42282fb1f39b8b2b574082af6a2195a259c93))


## v0.16.2 (2026-10-09)

### Bug Fixes

- **voice**: Close each request's socket; log every voice turn on both sides
  ([`cbc85c8`](https://github.com/dixonsatit/FlyBot/commit/cbc85c87481d1fe1b8a53ffead12e821187c0f9c))


## v0.16.1 (2026-10-09)

### Bug Fixes

- **voice**: Accept "วี" at the start of a sentence as the wake name
  ([`930590b`](https://github.com/dixonsatit/FlyBot/commit/930590b88c93a78afa64c5d8f5707ed120e81544))


## v0.16.0 (2026-10-09)

### Features

- **assistant**: Calendar and GitHub watching, and the robot speaks up on its own
  ([`09f1e4d`](https://github.com/dixonsatit/FlyBot/commit/09f1e4dc0a97ea2ae53285fe5bffb88a1bf47f1f))


## v0.15.0 (2026-10-09)

### Features

- **assistant**: An IT/dev sidekick persona, clever and a little cheeky
  ([`ee52ea3`](https://github.com/dixonsatit/FlyBot/commit/ee52ea33b99323de839ba62681facdd768bc75dd))


## v0.14.0 (2026-10-09)

### Features

- **assistant**: Answer anything, know the time, move the head on command
  ([`6723f2b`](https://github.com/dixonsatit/FlyBot/commit/6723f2ba448ea52d189797305c94c72675e6803e))


## v0.13.3 (2026-10-09)

### Bug Fixes

- **firmware**: Never block on USB serial logs; per-network wake name
  ([`3057d9b`](https://github.com/dixonsatit/FlyBot/commit/3057d9bd8c969f956916ea39dd9f21b63d3afad0))


## v0.13.2 (2026-10-09)

### Bug Fixes

- **voice**: Follow-up turns without the name and fewer missed wake words
  ([`eb5ae75`](https://github.com/dixonsatit/FlyBot/commit/eb5ae75d5223a07fec1ab621b2512483c0214318))


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
