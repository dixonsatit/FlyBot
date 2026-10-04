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
python -m flybot.mqtt_bridge --host 192.168.1.10 --data-dir data/codex
python tools/fake_stackchan.py --host 192.168.1.10     # หุ่นจำลองสำหรับทดสอบ broker
```

ตัวเลือก: `--port --username --password --base-topic --side L|R --rate 20 --backend nengo|rate --no-telemetry`
ค่าปรับจูนอื่น ๆ (FOV, gain, ขีดจำกัดมุม, ทิศ IMU) อยู่ใน `ControllerConfig` (`flybot/controller.py`)

## MQTT payload

**อินพุตจาก StackChan** (ทุกฟิลด์เป็น JSON)

| topic | ตัวอย่าง | หมายเหตุ |
|---|---|---|
| `stackchan/sensor/camera` | `{"x":200,"y":110,"vx":35,"vy":-4,"width":320,"height":240}` | พิกัด/ความเร็วเป็นพิกเซล(/วินาที) ไม่ส่ง `vx,vy` ก็ได้ (คำนวณจากเฟรมต่อเนื่อง), `"detected":false` เมื่อไม่เจอวัตถุ, `polarity` (-1..1) ถ้ารู้ว่าเป็นขอบสว่าง/มืด |
| `stackchan/sensor/imu` | `{"gyro":[gx,gy,gz],"accel":[ax,ay,az]}` | gyro °/s, accel หน่วย g, ใส่ `"yaw"` (°) แทนได้ถ้ามี sensor fusion |
| `stackchan/sensor/proximity` | `{"distance_mm":120}` หรือ `{"ps":850}` | `ps` = ค่าดิบ LTR-553 (0–2047) |

**เอาต์พุต** `stackchan/command`

```json
{
  "servo": {"pan_angle": 12.4, "tilt_angle": -3.1},
  "face":  {"expression": "curious"},
  "audio": {"tones": [[600, 60], [900, 60], [1200, 90]], "volume": 120},
  "brain": {"HS": 0.41, "VS": 0.0, "LC10": [0.2, 0.0], "heading": 0.0,
            "dopamine": 0.1, "octopamine": 0.3, "novelty": 0.8, "sleep_pressure": 0.0}
}
```

- `pan_angle` −90…90 (บวก = หันขวา), `tilt_angle` −45…45 (บวก = เงยขึ้น)
- `expression`: `curious | alert | sleepy | happy`
- `audio` ส่งเฉพาะตอนอารมณ์เปลี่ยน (นอกนั้นเป็น `null`): `tones` = `[ความถี่ Hz, มิลลิวินาที]` ใช้กับ `M5.Speaker.tone()`
- `brain` เป็น telemetry สำหรับ debug ปิดได้ด้วย `--no-telemetry`

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

## ฝั่ง CoreS3 (ตัวอย่างแนวทาง)

ฝั่งหุ่นต้องทำ 3 อย่าง: (1) หาตำแหน่งวัตถุในภาพแล้ว publish `camera` (2) publish IMU/proximity
(3) subscribe `stackchan/command` แล้วสั่งเซอร์โว/หน้า/ลำโพง ตัวอย่างโครง Arduino
(PubSubClient + ArduinoJson + m5stack-avatar; ปรับชื่อฟังก์ชันเซอร์โวให้ตรงกับเฟิร์มแวร์ของคุณ):

```cpp
void onCommand(char* topic, byte* payload, unsigned int len) {
  JsonDocument doc;
  if (deserializeJson(doc, payload, len)) return;
  float pan = doc["servo"]["pan_angle"], tilt = doc["servo"]["tilt_angle"];
  servoX.moveTo(90 + pan);  servoY.moveTo(90 + tilt);   // แล้วแต่การติดตั้งเซอร์โว
  const char* e = doc["face"]["expression"];
  avatar.setExpression(!strcmp(e, "happy") ? Expression::Happy
                     : !strcmp(e, "sleepy") ? Expression::Sleepy
                     : !strcmp(e, "alert")  ? Expression::Angry : Expression::Doubt);
  for (JsonArray t : doc["audio"]["tones"].as<JsonArray>()) {
    if (t[0].as<int>() > 0) M5.Speaker.tone(t[0], t[1]);
    delay(t[1].as<int>());
  }
}
```

## โครงสร้าง

```
flybot/connectome.py       โหลด Codex CSV → adjacency matrix (+ connectome จำลอง)
flybot/optic_lobe.py       derive_gains() + เครือข่าย Nengo / rate model
flybot/central_complex.py  E-PG / FC2 / PFL3
flybot/mushroom_body.py    KC, MBON, dopamine/octopamine, อารมณ์
flybot/controller.py       BrainController: sensors → command JSON
flybot/mqtt_bridge.py      MQTT loop
flybot/sim.py              ฉากจำลอง
tools/fake_stackchan.py    หุ่นจำลองฝั่ง MQTT
tests/                     pytest
```
