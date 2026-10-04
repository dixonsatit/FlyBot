# FlyBot — สมองแมลงหวี่ (FlyWire Codex) ควบคุม M5Stack StackChan

ใช้วงจรประสาทจาก connectome ของแมลงหวี่ ([FlyWire Codex](https://codex.flywire.ai)) จำลองด้วย
[Nengo](https://www.nengo.ai) (spiking LIF) แปลงข้อมูลเซ็นเซอร์ของ StackChan (กล้อง CoreS3, IMU,
proximity) เป็นคำสั่งเซอร์โว Pan-Tilt, หน้าตา/อารมณ์ และเสียง แล้วส่งกลับผ่าน MQTT เป็น JSON

```
CoreS3 ──MQTT──► stackchan/sensor/{camera,imu,proximity}
                         │
                 ┌───────▼────────┐
                 │ Optic Lobe     │ L1/L2 → T4/T5(a-d) → HS/VS, LC10   (น้ำหนักจาก adjacency matrix)
                 │ Central Complex│ E-PG heading ring, FC2 goal, PFL3  (ชดเชยการหมุนตัว)
                 │ Mushroom Body  │ KC → MBON habituation, DA/OA       (อารมณ์)
                 └───────┬────────┘
CoreS3 ◄──MQTT── stackchan/command   {servo, face, audio}
```

## ติดตั้ง

```bash
pip install -r requirements.txt      # numpy, pandas, nengo, scipy, paho-mqtt
pip install pytest && pytest -q      # ทดสอบ
```

> Brian2 2.9 ยังใช้กับ numpy ≥ 2 ไม่ได้ จึงใช้ Nengo เป็นหลัก (มี backend `rate` แบบ NumPy ล้วนสำรองไว้)

## ข้อมูล FlyWire Codex

ล็อกอินที่ codex.flywire.ai → **Download Data** แล้ววางไฟล์ไว้ในโฟลเดอร์เดียวกัน เช่น `data/codex/`

| ไฟล์ | คอลัมน์ที่ใช้ |
|---|---|
| `connections*.csv.gz` | `pre_root_id, post_root_id, neuropil, syn_count, nt_type` |
| `visual_neuron_types*.csv.gz` (หรือ `consolidated_cell_types` / `classification`) | `root_id, type` |

ระบบเลือกเฉพาะ neuropil ของ Optic Lobe (`LA, ME, AME, LO, LOP`) ซีกที่เลือก (`--side R`)
แล้วรวม synapse เป็น matrix ระดับกลุ่มเซลล์ (`flybot/connectome.py: GROUP_PATTERNS`)
ถ้าไม่ระบุ `--data-dir` จะใช้ connectome จำลอง (ค่าโดยประมาณ) เพื่อให้รันได้ทันที

```python
from flybot import load_codex, derive_gains
adj = load_codex("data/codex", side="R").group_adjacency()   # pandas DataFrame pre x post
gains = derive_gains(adj)                                     # น้ำหนักที่ใช้ในเครือข่าย Nengo
```

## รัน

```bash
python -m flybot.sim                                   # จำลองฉากโดยไม่ต้องมีหุ่น/broker
python -m flybot.mqtt_bridge --host 192.168.1.10 --data-dir data/codex   # ใช้กับหุ่นจริง
```

ทดสอบ MQTT ในเครื่องด้วยหุ่นจำลอง (เปิดคนละ terminal ตามลำดับ):

```bash
mosquitto -v
python -m flybot.mqtt_bridge --host 127.0.0.1 --data-dir data/codex --set sensor_latency_s=0
python tools/fake_stackchan.py --host 127.0.0.1        # ส่งเซ็นเซอร์ 30 วินาที แล้วพิมพ์คำสั่งที่ได้รับ
mosquitto_sub -h 127.0.0.1 -t 'stackchan/#' -v         # (ไม่บังคับ) ดูทุกข้อความ
```

`fake_stackchan.py` สร้างภาพจากมุมหัวล่าสุดทันทีจึงไม่มี latency ต้องใส่ `--set sensor_latency_s=0`
ไม่อย่างนั้น controller จะชดเชยเกิน (หัวคลาดเป้า ~8°) ส่วนหุ่นจริงใช้ค่าเริ่มต้น 0.1 วินาที

ตัวเลือก: `--port --username --password --base-topic --side L|R --rate 20 --backend nengo|rate --no-telemetry`
ค่าปรับจูนอื่น ๆ (FOV, gain, ขีดจำกัดมุม, ทิศ IMU, `sensor_latency_s`) อยู่ใน `ControllerConfig` (`flybot/controller.py`)
ปรับได้จาก command line ด้วย `--set key=value` เช่น `--set k_position=60 --set tilt_limits=0,30`

### จูนก่อนมีหุ่น

`flybot/plant.py` จำลองหุ่นตามเฟิร์มแวร์: เซอร์โว SG90 (slew rate + lag, tilt 0–30°), กล้อง frame differencing
(เห็นวัตถุเฉพาะตอนขยับ, ข้ามเฟรมตอนหัวหมุนเร็ว), latency ของ WiFi/broker

```bash
python -m flybot.tune --data-dir data/codex            # คะแนน = error การมองเฉลี่ย (องศา)
python -m flybot.tune --search                         # grid search k_position / k_motion / k_heading
python -m flybot.tune --plant sensor_latency_s=0.2     # ลองเครือข่ายช้า
```

controller เก็บประวัติท่าหัวของตัวเอง แล้วเทียบภาพกับท่าหัว ณ เวลาที่ถ่าย (เวลาที่ได้รับ − `sensor_latency_s`)
ทั้ง efference copy และตำแหน่งเป้าหมาย; ถ้าไม่ชดเชยหัวจะเลยเป้าราว 40° ในหุ่นจำลอง
ตั้ง `sensor_latency_s` ให้ใกล้ latency จริง (ประเมินเกินดีกว่าขาด)

## MQTT payload

**อินพุตจาก StackChan** (ทุกฟิลด์เป็น JSON)

| topic | ตัวอย่าง | หมายเหตุ |
|---|---|---|
| `stackchan/sensor/camera` | `{"x":200,"y":110,"vx":35,"vy":-4,"width":320,"height":240,"box":[180,90,220,130],"lum":[90,120,110,100]}` | พิกัด/ความเร็วเป็นพิกเซล(/วินาที) ไม่ส่ง `vx,vy` ก็ได้ (คำนวณจากเฟรมต่อเนื่อง), `"detected":false` เมื่อไม่เจอวัตถุ, `polarity` (-1..1) ถ้ารู้ว่าเป็นขอบสว่าง/มืด, `box` = กรอบ [ซ้าย,บน,ขวา,ล่าง] ของพิกเซลที่เปลี่ยน (looming), `lum` = ความสว่างเฉลี่ยครึ่ง ซ้าย/ขวา/บน/ล่าง (phototaxis, ส่งมากับ `detected:false` ได้) |
| `stackchan/sensor/imu` | `{"gyro":[gx,gy,gz],"accel":[ax,ay,az]}` | gyro °/s, accel หน่วย g, ใส่ `"yaw"` (°) แทนได้ถ้ามี sensor fusion |
| `stackchan/sensor/proximity` | `{"distance_mm":120}` หรือ `{"ps":850,"als":45}` | `ps` = ค่าดิบ LTR-553 (0–2047), `als` = แสงรอบตัว (มืดกว่า `als_dark` → ง่วงเร็วขึ้น) |

**เอาต์พุต** `stackchan/command`

```json
{
  "servo": {"pan_angle": 12.4, "tilt_angle": -3.1},
  "face":  {"expression": "curious"},
  "audio": {"tones": [[600, 60], [900, 60], [1200, 90]], "volume": 120},
  "brain": {"HS": 0.41, "VS": 0.0, "LC10": [0.2, 0.0], "heading": 0.0,
            "dopamine": 0.1, "octopamine": 0.3, "novelty": 0.8, "sleep_pressure": 0.0,
            "LPLC2": 0.0, "LC4": 0.0, "GF": 0.0},
  "game": {"streak": 1.2, "best": 4.0}, "text": "1.2s / best 4.0s"
}
```

- `pan_angle` −90…90 (บวก = หันขวา), `tilt_angle` −45…45 (บวก = เงยขึ้น)
- `expression`: `curious | alert | sleepy | happy`
- `audio` ส่งเฉพาะตอนอารมณ์เปลี่ยนหรือตอนสะดุ้ง (นอกนั้นเป็น `null`): `tones` = `[ความถี่ Hz, มิลลิวินาที]` ใช้กับ `M5.Speaker.tone()`
- `brain` เป็น telemetry สำหรับ debug ปิดได้ด้วย `--no-telemetry`
- `game` / `text` มีเฉพาะเมื่อรันด้วย `--game` (เฟิร์มแวร์แสดง `text` ในบอลลูนคำพูด)

**เหตุการณ์** `stackchan/event` (และ POST ไป `--webhook URL` ถ้าตั้งไว้; ชนิดเดียวกันส่งห่างกันอย่างน้อย `--event-cooldown` วินาที)

```json
{"type": "escape", "from": "right", "robot": "stackchan", "at": "2026-10-04T06:50:58+00:00",
 "message": "StackChan: สะดุ้งหลบสิ่งที่พุ่งเข้ามา", "brain": {...}}
```

`type`: `presence` (มีอะไรใหม่เข้ามา → alert), `escape` (Giant Fiber ยิง), `record` (สถิติใหม่ในเกม, มี `seconds`)

## พฤติกรรมเพิ่มเติม

```bash
python -m flybot.mqtt_bridge --host 192.168.1.10 --data-dir data/codex \
    --personality skittish --game --webhook https://example.com/hook --event-cooldown 60
```

- **หลบสิ่งที่พุ่งเข้าหา (looming escape)** — LPLC2 (มี 4 แขน ชอบการเคลื่อนที่ออกจากศูนย์กลาง) ตอบสนองเมื่อขอบทั้ง 4
  ของ `box` ขยายออกพร้อมกัน (วัตถุเลื่อนข้างไม่นับ) เทียบกับขนาด = 1/เวลาถึงตัว; LC4 ตอบความเร็วการขยาย;
  Giant Fiber (DNp01) รวมสองทางด้วยน้ำหนักจากสัดส่วน synapse จริง (v783 ซีกขวา: LC4 1634, LPLC2 658 synapse)
  แล้วยิงเมื่อเกิน `gf_threshold` → หันหนีด้านตรงข้าม + เงยขึ้น `escape_s` วินาที, octopamine พุ่ง (หน้า alert),
  เสียงสะดุ้ง แล้ว PFL3 พาหันกลับมาดูจุดเดิม; ไม่ตอบวัตถุเล็กกว่า ~10° หรือเข้ามาช้า ๆ
- **Phototaxis** — `phototaxis` +1 หันเข้าหาแสง / −1 หนีแสง (ใช้เมื่อไม่มีวัตถุ) จาก `lum` ซ้าย/ขวา/บน/ล่าง
- **นิสัย** `--personality`: `curious` (ค่าเริ่มต้น), `skittish` (ตกใจง่าย, หนีแสง, ลืมเร็ว), `bold` (ใจกล้า, เข้าหาแสง),
  `sleepy` (ง่วงเร็ว, ชอบที่มืด) — เป็นชุดค่า `ControllerConfig` (`PERSONALITIES` ใน `flybot/controller.py`) แล้วค่อยทับด้วย `--set`
- **เกมดึงความสนใจ** `--game` — นับเวลาที่หุ่นมองวัตถุตรงกลางได้ต่อเนื่อง (หลุดสั้นกว่า `game_grace_s` ไม่นับว่าขาด)
  frame differencing เห็นเฉพาะของที่ขยับ ผู้เล่นต้องขยับวัตถุช้า ๆ ให้หุ่นตาม

## LLM (ชั้นคิดช้า เหนือสมองแมลง)

สมองแมลงยังคุม reflex 20 Hz เหมือนเดิม LLM ทำงานบน thread แยก ไม่อยู่ใน loop ควบคุม
สั่งหุ่นได้ผ่านคำสั่งแบบคิว (`look_at` → เป้าหมาย FC2 ให้ PFL3 หันไป, `configure`, `say`) เท่านั้น
LLM ช้าหรือล่มก็ไม่ทำให้เซอร์โวสะดุด

```bash
pip install -e .[llm]
# Claude (ค่าเริ่มต้น claude-opus-5-5, effort low, prompt caching, server-side refusal fallback)
export ANTHROPIC_API_KEY=...
python -m flybot.mqtt_bridge --host 192.168.1.10 --data-dir data/codex --llm anthropic
# OpenAI-compatible: OpenAI / Ollama / vLLM / LM Studio / Typhoon / OpenRouter
python -m flybot.mqtt_bridge ... --llm openai --llm-model <โมเดล OpenAI>      # ใช้ OPENAI_API_KEY
python -m flybot.mqtt_bridge ... --llm openai --llm-base-url http://localhost:11434/v1 --llm-model <โมเดลใน Ollama>
python -m flybot.mqtt_bridge ... --llm openai --llm-base-url <base URL ของผู้ให้บริการ> \
    --llm-model <ชื่อโมเดล> --llm-api-key-env <ชื่อ env ที่เก็บ key>
```

- **ผู้บรรยาย** — ทุกเหตุการณ์ (เว้นระยะ `--narrate-cooldown`) LLM อธิบายว่าวงจรไหนทำงานเพราะอะไร:
  ภาษาไทยออก `stackchan/chat/out` (`{"type":"narration",...}`), ประโยคอังกฤษสั้นขึ้นบอลลูน; ปิดด้วย `--no-narrate`
- **แชท + สั่งหุ่น** — ส่งข้อความ (หรือ `{"text": ...}`) ไป `stackchan/chat/in`, คำตอบออก `stackchan/chat/out`
  tools: `look_at`, `set_personality`, `set_game`, `brain_state`, `say`, `look_and_describe` (ถ้าเปิด vision)
  ```bash
  mosquitto_pub -h 192.168.1.10 -t stackchan/chat/in -m 'หันไปทางขวาหน่อย แล้วเปลี่ยนเป็นขี้ตกใจ'
  mosquitto_sub -h 192.168.1.10 -t stackchan/chat/out
  ```
- **ดูแล้วบอกว่าเห็นอะไร** `--llm-vision` — ตอน `presence` bridge ขอภาพ (`stackchan/snapshot/request`
  → เฟิร์มแวร์ส่ง JPEG 160×120 grayscale ที่ `stackchan/snapshot`) ให้ LLM บรรยาย แล้วส่งเหตุการณ์ `seen`
  (มี `description`) ไป MQTT/webhook; prompt สั่งไม่ให้ระบุตัวบุคคล **ภาพออกจากหุ่นไปยังผู้ให้บริการ LLM**
  ถ้าใช้ในโรงพยาบาลให้ใช้โมเดลที่รันในเครือข่ายภายใน (เช่น Ollama ที่รองรับภาพ) หรือผ่านการพิจารณา PDPA ก่อน
- ฟอนต์บอลลูนของ M5GFX ไม่มีอักษรไทย จึงให้ LLM เขียนข้อความบนจอเป็นภาษาอังกฤษสั้น ๆ ส่วนภาษาไทยออกทาง MQTT
- โมเดลฝั่ง OpenAI-compatible ต้องรองรับ tool/function calling (และรองรับภาพถ้าเปิด `--llm-vision`)

## หลักการของแต่ละวงจร

- **Optic Lobe** — `lamina` แยกการเคลื่อนไหวเป็นช่อง ON (L1) / OFF (L2) สี่ทิศ, ensemble T4a–d / T5a–d
  (encoder บวก = rectify) รับน้ำหนักจาก path `L1→{Mi1,Tm3,Mi4,Mi9}→T4` และ `L2→{Tm1,Tm2,Tm4,Tm9}→T5`
  (สัดส่วน synapse ขาเข้า), HS = T4a/T5a − (T4b/T5b ผ่าน LPi), VS = T4d/T5d − (T4c/T5c ผ่าน LPi),
  LC10 แทนตำแหน่งวัตถุ → `pan_rate = k_position·LC10x + k_motion·HS` และมี efference copy
  หักการเคลื่อนไหวของภาพที่เกิดจากการหันหัวเอง
- **Central Complex** — gyro z หมุน bump บนวงแหวน E-PG 16 wedge; การเปลี่ยน heading ทำให้ pan
  หมุนกลับทันที (VOR) และเมื่อวัตถุหายไป PFL3 เทียบ goal (FC2) กับทิศที่มองอยู่ แล้วหันกลับไปจุดเดิม
- **Mushroom Body** — feature → KC แบบ sparse (top 5%) → MBON familiarity ที่ synapse ลดลงเมื่อเจอซ้ำ
  (novelty ลด); Octopamine ↑ จาก novelty×ความแรง / วัตถุพุ่งเข้าใกล้ / การเขย่า, Dopamine ↑ จากการลูบ
  (มืออยู่ใกล้นิ่ง ๆ) และการติดตามได้ตรงกลาง, sleep pressure ↑ เมื่อไม่มีอะไรเกิดขึ้น
  → alert (OA สูง) > happy (DA สูง) > sleepy > curious

## ฝั่ง CoreS3 (เฟิร์มแวร์)

โปรเจกต์ PlatformIO อยู่ที่ `firmware/stackchan/` (CoreS3 + เซอร์โว SG90, M5Unified, M5CoreS3,
m5stack-avatar, PubSubClient, ArduinoJson, ESP32Servo)

```bash
cd firmware/stackchan
cp src/flybot_config.example.h src/flybot_config.h   # WiFi, broker, ขาเซอร์โว, ทิศ/ขอบเขตมุม
pio run -t upload && pio device monitor
```

- กล้อง GC0308 แบบ grayscale 160×120: หาวัตถุด้วย frame differencing (จุดศูนย์กลางของพิกเซลที่เปลี่ยน)
  แล้วคำนวณ `vx, vy` บนบอร์ด `polarity` = ทิศของการเปลี่ยนความสว่าง (ON/OFF)
  ถ้าภาพเปลี่ยนเกิน `MAX_MOTION_FRACTION` (หัวกำลังหมุน/แสงเปลี่ยน) จะข้ามเฟรมนั้น
- ส่ง `box` (กรอบพิกเซลที่เปลี่ยน) และ `lum` (ความสว่างเฉลี่ยแต่ละครึ่งภาพ) ไปกับทุกเฟรม
- IMU (BMI270) ~30 Hz, LTR-553 `ps` + `als` 10 Hz
- `text` จากคำสั่งแสดงในบอลลูนคำพูดของ avatar (อัปเดตไม่เกิน 4 ครั้ง/วินาที)
- เซอร์โว: `องศา = CENTER + SIGN × มุมจากคำสั่ง` แล้วจำกัดใน MIN..MAX (SG90 แกน Y ขยับได้ ~60–90°)
  ค่าเริ่มต้น Port.C X=17, Y=18 ถ้าหัวหันผิดทางให้กลับ `SERVO_X_SIGN` / `SERVO_Y_SIGN`
- เสียงเล่นจากคิวแบบไม่บล็อก loop; หน้า: alert→Angry, happy→Happy, sleepy→Sleepy, curious→Doubt

## โครงสร้าง

```
flybot/connectome.py       โหลด Codex CSV → adjacency matrix (+ connectome จำลอง)
flybot/optic_lobe.py       derive_gains() + เครือข่าย Nengo / rate model
flybot/central_complex.py  E-PG / FC2 / PFL3
flybot/mushroom_body.py    KC, MBON, dopamine/octopamine, อารมณ์
flybot/looming.py          LPLC2 / LC4 → Giant Fiber (หลบ)
flybot/events.py           เหตุการณ์ → MQTT + webhook (cooldown)
flybot/llm.py              ผู้ให้บริการ LLM: Claude (anthropic SDK) / OpenAI-compatible (openai SDK)
flybot/cortex.py           ชั้น LLM: บรรยาย, แชท + tools, ดูภาพ (thread แยก)
flybot/controller.py       BrainController: sensors → command JSON
flybot/mqtt_bridge.py      MQTT loop
flybot/sim.py              ฉากจำลอง (กล้องอุดมคติ)
flybot/plant.py            หุ่นจำลองตามเฟิร์มแวร์ (เซอร์โว, frame differencing, latency)
flybot/tune.py             วัด error การมอง + grid search gain
tools/fake_stackchan.py    หุ่นจำลองฝั่ง MQTT
firmware/stackchan/        เฟิร์มแวร์ CoreS3 (PlatformIO)
tests/                     pytest
```
