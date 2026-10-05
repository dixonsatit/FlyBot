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

## Dashboard (หน้าเว็บ)

```bash
python -m flybot.mqtt_bridge --host 127.0.0.1 --gains data/gains.json --dashboard-port 8088 --dashboard-sim [--llm ...]
# เปิด http://localhost:8088   (บน K8s: http://<node IP>:31880 ผู้ใช้ stackchan + รหัส MQTT)
```

- **หุ่น**: หน้าตามอารมณ์, ทิศหัว (pan/tilt), บอลลูนข้อความ; **สมองสด**: HS/VS, LC10, dopamine, octopamine,
  novelty, ความง่วง, LPLC2/LC4/Giant Fiber
- **โลกจำลอง** (`--dashboard-sim` หรือสวิตช์มุมขวาบน): ใช้ `flybot.plant` แทนหุ่น — ลากวัตถุ, ให้วัตถุขยับเอง,
  หมุนตัวหุ่น, ลูบหัว, ของพุ่งเข้าหา; **ปิดเมื่อต่อหุ่นจริง** ไม่งั้นสองแหล่งจะป้อนสมองตัวเดียวกัน
- **สั่ง**: นิสัย, เกม, หันซ้าย/ขวา/เงย; **แชทกับ LLM** + ฟีดเหตุการณ์
- **🔊 พูด**: อ่านคำตอบ/คำบรรยาย/เตือนประชุมเป็นภาษาไทย ปากหุ่นขยับตาม — เลือกเสียงได้ 2 แหล่ง:
  - **Wayu-TTS** (ตัวเดียวกับ kiosk ซักประวัติ, 12 เสียง, รันด้วย CPU ในโรงพยาบาล): `--tts-url http://<wayu>:7860
    [--tts-voice m_young_clear]` bridge เรียก `POST /api/speak` ให้แล้วส่ง WAV ให้ browser ผ่าน `/api/tts`
    (โมเดล Wayu เป็น CC-BY-NC-4.0 ไม่ได้รวมใน image นี้ ต้องชี้ไปที่ Wayu ที่รันอยู่)
  - เสียงไทยของเครื่องที่เปิดหน้าเว็บ (Web Speech API)
- **🎤 ฟัง**: กดแล้วพูด กดอีกครั้งเพื่อส่ง
  - `--stt-url http://<asr>:7871` (asr-typhoon ของ kiosk ซักประวัติ, Typhoon ASR บน CPU): หน้าเว็บอัดเสียง
    แปลงเป็น PCM 16 kHz ส่ง `/api/stt` → ถอดในโรงพยาบาล (~0.3 วินาที) แล้วส่งเข้าแชท — **เสียงไม่ออกนอกเครือข่าย**
  - ไม่ตั้ง: ใช้ตัวรู้จำเสียงของ Chrome แทน (**เสียงถูกส่งไป Google**)
  - ไมค์ใช้ได้เฉพาะ `https` หรือ `localhost` (บน K8s: `kubectl -n flybot port-forward svc/flybot-dashboard 8080`
    แล้วเปิด http://localhost:8080)
- **ใช้บริการเสียงร่วมกับ kiosk บน cluster เดียวกัน**: `FLYBOT_TTS_URL=http://wayu-tts.stackchan-interview.svc:7860`,
  `FLYBOT_STT_URL=http://asr-typhoon.stackchan-interview.svc:7871` — ไม่ต้องติดตั้งหรือดึง image private ซ้ำ

## สมองทั้งก้อน + Hospital Twin

`flybot/wholebrain.py` รัน connectome ทั้งหมด (v783: 138,584 เซลล์, 3.66 ล้านการเชื่อมต่อ, 50.7 ล้าน synapse)
เป็นเซลล์ LIF ตามพารามิเตอร์ของโมเดลสมองแมลงหวี่ทั้งก้อนที่ตีพิมพ์ (Shiu et al. 2024) — ACh กระตุ้น, GABA/Glu ยับยั้ง,
ไม่เรียนรู้ กระตุ้นด้วย spike Poisson ที่เซลล์รับสัมผัส อ่านผลจากเซลล์สั่งการ (DN) ได้ทั้งจำนวน spike และกระแสสะสม
ครั้งแรกสร้าง cache `data/codex/wholebrain.npz` (~14 MB, ~7 วินาที) · CPU ของ Mac: ช้ากว่าเวลาจริง ~4 เท่าที่ dt 0.1 ms

`python -m flybot.twin --data-dir data/codex --port 8090` → http://localhost:8090 — โรงพยาบาลจำลองที่แมลงหวี่บินตรวจ:
1. **ตัวแปลงข้อมูลเข้า (ของเรา)**: จุดวิกฤตด้านซ้าย/ขวา → กระตุ้นตาซ้าย/ขวา (R1-6)
2. **สมอง (connectome)**: รันทีละ 100 ms อ่านความต่างของ DNa01/DNa02 ขวา−ซ้าย เป็นคำสั่งเลี้ยว (ปรับเทียบตอนเริ่ม)
3. **ตัดสินใจ (กฎของเรา)**: ถึงแผนกที่วิกฤต → เปิดช่องบริการเพิ่ม 45 นาที
เทียบ 3 โลกที่ผู้ป่วยมาเหมือนกัน: แมลงหวี่ / autopilot (บินตรงไปจุดแย่สุด) / ไม่มีใครทำอะไร พร้อมกล่องสมมติฐาน/ข้อจำกัด

สิ่งที่วัดได้กับ connectome นี้ (ดูเพิ่มที่ docstring ของ `twin.py`):
- ไม่กระตุ้น = DN เงียบ; ตาซ้าย → DNa02/DNa01 ขวาทำงานมากกว่า, ตาขวา → กลับข้าง (มีความเอียงไปขวาเมื่อกระตุ้นสองตา)
- ต่ำกว่า ~100 Hz สัญญาณไม่ถึง DN; ตาขวาต้อง ~240 Hz จึงได้ทิศถูกแน่นอน → ตัวแปลงจึงส่งแค่ "ทิศ" ไม่ส่งความรุนแรง
- ถ้ากระตุ้นตาข้างเดียวตลอดแมลงจะบินวนรอบเป้า จึงมี "โซนตรงหน้า" ±20° ให้บินตรง
- **โหมดข้อมูลจริง** `--replay data/his/<วัน>.json`: เล่นซ้ำจำนวนผู้มารับบริการ**รายนาทีต่อจุดบริการ**จากระบบคิว Q4U
  (`app_queue.q4u_queue`, นับรวมเท่านั้น ไม่มี HN/VN/ชื่อ) ความเร็วให้บริการไม่มีในข้อมูล (หลายจุดไม่บันทึกว่าเสร็จ)
  จึงประมาณจากช่วงที่คนมามากของจุดนั้น — คิวในภาพเป็นค่าประมาณ; ไฟล์อยู่ใน `data/` (ไม่เข้า git)
  ตัวอย่าง query: `SELECT service_point_id, HOUR(time_serv)*60+MINUTE(time_serv) AS minute, COUNT(*) FROM
  app_queue.q4u_queue WHERE date_serv = ? AND (is_cancel IS NULL OR is_cancel != 'Y') GROUP BY 1, 2`
- ทุกตัว (แมลงหวี่/autopilot) ใช้กฎเดียวกัน: อยู่ที่จุดที่เพิ่งช่วย 10 นาที และเกาะนิ่งเมื่อไม่มีจุดวิกฤต (< 0.2)
  ระยะบินจึงนับเฉพาะตอนบินจริง และสมองไม่ต้องรันตอนพัก

บน K8s (`deploy/k8s/twin/`, NodePort 31890): ไฟล์ `wholebrain.npz` สร้างจากข้อมูล FlyWire จึงไม่ใส่ใน image สาธารณะ
คัดลอกขึ้น PVC `flybot-brain-data` ครั้งเดียว:

```bash
kubectl apply -k deploy/k8s/twin            # PVC + twin (pod รอไฟล์จนกว่าจะมี)
kubectl -n flybot run brain-loader --image=busybox --restart=Never --overrides='{"spec":{"containers":[{"name":"l","image":"busybox","command":["sleep","600"],"volumeMounts":[{"name":"d","mountPath":"/data"}]}],"volumes":[{"name":"d","persistentVolumeClaim":{"claimName":"flybot-brain-data"}}]}}'
kubectl -n flybot cp data/codex/wholebrain.npz brain-loader:/data/wholebrain.npz
kubectl -n flybot cp data/his/q4u-2026-10-02.json brain-loader:/data/q4u-2026-10-02.json   # ถ้าใช้ --replay
kubectl -n flybot delete pod brain-loader && kubectl -n flybot rollout restart deploy/flybot-twin
```

## รันบน Kubernetes

`deploy/k8s/` (kustomize) มี bridge + Mosquitto (มีรหัสผ่าน) ให้หุ่นต่อจาก LAN ผ่าน NodePort `31883`
image `ghcr.io/dixonsatit/flybot` สร้างอัตโนมัติทุก release (Dockerfile ที่ root: non-root, read-only root FS)

```bash
# 1. แปลง connectome เป็นไฟล์ gains เล็ก ๆ (ไม่ต้องเอาข้อมูล Codex 70 MB ขึ้น cluster)
python -m flybot.gains_io --data-dir data/codex --out data/gains.json
# 2. namespace, gains, secrets (ค่าที่เป็นความลับไม่อยู่ใน git)
kubectl create namespace flybot
kubectl -n flybot create configmap flybot-gains --from-file=gains.json=data/gains.json
kubectl -n flybot create secret generic flybot-secrets \
    --from-literal=mqtt-password='<รหัสผ่าน MQTT>' \
    --from-file=calendar-url=calendar.key          # ไม่บังคับ: webhook-url, llm-api-key
# 3. ตั้งค่าที่ไม่ลับใน deploy/k8s/config.yaml (FLYBOT_*: personality, LLM, timezone ...) แล้ว
kubectl apply -k deploy/k8s
kubectl -n flybot logs deploy/flybot-bridge -f
```

- ทุก option ของ bridge ตั้งผ่าน env `FLYBOT_<OPTION>` ได้ (เช่น `FLYBOT_LLM=openai`, `FLYBOT_SET="home_pan=0"`)
- หุ่น: `MQTT_HOST` = IP ของ node, `MQTT_PORT 31883`, `MQTT_USER "stackchan"` + รหัสผ่านเดียวกับ Secret
  (หรือเปลี่ยน Service เป็น `LoadBalancer` ถ้า cluster มี เช่น MetalLB)
- bridge มี replica เดียว (หุ่นหนึ่งตัวต่อสมองหนึ่งตัว), liveness probe ดูว่า loop 20 Hz ยังหมุน (heartbeat file),
  ใช้ ~16% CPU / ~65 MB RAM; ต่อ broker ใหม่เองถ้า broker รีสตาร์ต
- ถ้า package บน GHCR เป็น private ต้องตั้ง `imagePullSecrets` หรือเปลี่ยน package เป็น public
- ทดสอบแล้วบน minikube: Mosquitto ปฏิเสธการต่อแบบไม่มีรหัสผ่าน, หุ่นจำลองรับคำสั่ง/เหตุการณ์ผ่าน Service ได้

## เตือนประชุมจากปฏิทิน

อ่านปฏิทินแบบ iCalendar (ICS) ได้ทุกเจ้า: Google Calendar (Settings → ปฏิทิน → **Secret address in iCal format**),
Outlook / Microsoft 365 (Publish calendar → ICS), iCloud, `webcal://` หรือไฟล์ `.ics`
รองรับนัดซ้ำ (RRULE/EXDATE) และ timezone; ข้ามนัดทั้งวันและนัดที่ยกเลิก

```bash
pip install -e .[calendar]
python -m flybot.mqtt_bridge --host 192.168.1.10 --data-dir data/codex \
    --calendar "https://calendar.google.com/calendar/ical/.../basic.ics" \
    --remind-minutes 10,1 --tz Asia/Bangkok --webhook https://example.com/hook
```

ก่อนเริ่มประชุม 10 และ 1 นาที (ปรับด้วย `--remind-minutes`): หุ่นหันมาหาที่นั่งคุณ (`--set home_pan=0 --set home_tilt=10`),
หน้า alert 8 วินาที, เสียงเตือน 3 โน้ต, บอลลูน `Meeting in 10 min (14:00)` และส่งเหตุการณ์ `meeting`
(`title`, `start`, `minutes`, `location`, `message` ภาษาไทย) ไป MQTT/webhook — ประชุมต่างนัดไม่ติด cooldown กัน
ถ้าเปิด `--llm` จะได้คำเตือนภาษาไทยที่ `stackchan/chat/out` ด้วย; อ่านปฏิทินใหม่ทุก `--calendar-refresh` วินาที (300)

> ลิงก์ ICS แบบ secret ใครมีลิงก์ก็อ่านปฏิทินได้ เก็บไว้ในไฟล์ `*.key` (ถูก ignore) อย่าใส่ในโค้ดหรือ commit
> ชื่อ/สถานที่ประชุมจะถูกส่งไป webhook และ LLM (ถ้าเปิด) ด้วย

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
  ภาษาไทยออก `stackchan/chat/out` (`{"type":"narration",...}`), ข้อความสั้น (≤ 14 ตัวอักษร) ขึ้นบอลลูน; ปิดด้วย `--no-narrate`
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
- บอลลูนแสดงภาษาไทยด้วยฟอนต์ที่ฝังในเฟิร์มแวร์ (ดูหัวข้อเฟิร์มแวร์) ถ้าใช้เฟิร์มแวร์รุ่นเก่าที่ไม่มีฟอนต์ไทย
  ให้ `--set screen_lang=en` แล้วข้อความบนจอจะเป็นภาษาอังกฤษ
- โมเดลฝั่ง OpenAI-compatible ต้องรองรับ tool/function calling (และรองรับภาพถ้าเปิด `--llm-vision`)
- โมเดลที่ "คิดก่อนตอบ" (เช่น Qwen3 บน vLLM) อาจใช้ token หมดไปกับการคิดจนไม่มีคำตอบ (log จะเตือน)
  ปิดการคิดด้วย `--llm-extra-body '{"chat_template_kwargs": {"enable_thinking": false}}'` หรือเพิ่ม `--llm-max-tokens`
- ถ้า server ให้ภาพได้เฉพาะบางโมเดล ใช้ `--llm-vision-model` แยกโมเดลสำหรับดูภาพ (endpoint/key เดียวกัน)
- คำบรรยายแนบ "สาเหตุจริง" ของแต่ละเหตุการณ์ (`CAUSES` ใน `flybot/cortex.py`) และ `set_personality` คืนค่าที่เปลี่ยนจริง
  ให้ LLM อธิบายตามข้อเท็จจริงแทนการเดา

ตัวอย่าง (ทดสอบแล้วกับ server vLLM ภายในที่มี Qwen3.5-122B แบบตัวอักษร + Qwen3.8-27B ที่รับภาพได้):

```bash
export QWEN_API_KEY=...
python -m flybot.mqtt_bridge --host 127.0.0.1 --data-dir data/codex \
    --llm openai --llm-base-url https://<server>/v1 --llm-api-key-env QWEN_API_KEY \
    --llm-model qwen3.5-122b --llm-extra-body '{"chat_template_kwargs": {"enable_thinking": false}}' \
    --llm-vision --llm-vision-model qwen3.8-27b
```

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
- **ฟอนต์ไทยบนจอ** — ฟอนต์ Sarabun (SIL OFL, `fonts/OFL.txt`) แปลงเป็น VLW ฝังในเฟิร์มแวร์ (`src/thai_font.cpp`, ~17 KB)
  M5GFX วาดอักษรทีละตัวโดยไม่จัดตำแหน่งสระ/วรรณยุกต์ bridge จึงจัดให้ก่อนส่ง (`flybot/thai_text.py`: วรรณยุกต์
  เหนือสระบนยกสูง, สระ/วรรณยุกต์บน ป ฝ ฟ ฬ เลื่อนซ้าย, สระล่างใต้ ฎ ฏ เลื่อนลง โดยใช้ glyph ใน Private Use Area)
  สร้างใหม่ได้ด้วย `python tools/make_vlw_font.py --preview preview.png` (ดูภาพตัวอย่างก่อน build)
- **ตรวจเครื่องตอนเปิด** — ทุกครั้งที่เปิดเครื่องจะเช็ค PSRAM, กล้อง (+ จับภาพได้), IMU และ LTR-553 *หลัง* กล้องเริ่มทำงาน
  (ใช้บัส I2C ร่วมกัน), ลำโพง แล้วแสดงผล OK/FAIL บนจอ (ถ้ามี FAIL ค้างไว้ 10 วินาที) และส่ง `stackchan/selftest`
  (retained) ให้ bridge บันทึกใน log; **แตะจอระหว่างเปิดเครื่อง** (หรือ `SELF_TEST_FULL 1`) เพื่อทดสอบเต็ม:
  หมุนเซอร์โวพร้อมบอกบนจอว่าหัวควรหันไปทางไหน ถ้าหันผิดให้กลับ `SERVO_X_SIGN` / `SERVO_Y_SIGN`
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
flybot/meetings.py         อ่านปฏิทิน ICS → เตือนประชุม
flybot/wholebrain.py       สมองทั้งก้อน (LIF ทั้ง connectome)
flybot/twin.py             Hospital twin: แมลงหวี่นำทางด้วยสมองทั้งก้อน
flybot/controller.py       BrainController: sensors → command JSON
flybot/mqtt_bridge.py      MQTT loop
flybot/sim.py              ฉากจำลอง (กล้องอุดมคติ)
flybot/plant.py            หุ่นจำลองตามเฟิร์มแวร์ (เซอร์โว, frame differencing, latency)
flybot/tune.py             วัด error การมอง + grid search gain
tools/fake_stackchan.py    หุ่นจำลองฝั่ง MQTT
firmware/stackchan/        เฟิร์มแวร์ CoreS3 (PlatformIO)
tests/                     pytest
```
