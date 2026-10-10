// StackChan (CoreS3 + SG90, or M5Stack's StackChan with SCS0009 servos) <-> FlyBot brain over MQTT.
//
// publishes  <base>/sensor/camera     {"x","y","vx","vy","box":[l,t,r,b],"width","height","polarity","lum":[l,r,t,b]}
//                                     | {"detected":false,"lum":[...]}
//            <base>/sensor/imu        {"gyro":[deg/s x3],"accel":[g x3]}
//            <base>/sensor/proximity  {"ps": LTR-553 raw 0..2047, "als": ambient light counts}
// subscribes <base>/command           {"servo":{"pan_angle","tilt_angle"},"face":{"expression"},"audio":{...}|null,
//                                      "text": speech balloon (attention game), optional}
//            <base>/snapshot/request  -> publishes one JPEG frame on <base>/snapshot (LLM vision)
//            <base>/selftest          boot self-test result (retained), see selftest.cpp
#include <M5CoreS3.h>
#include <Avatar.h>
#include <ArduinoJson.h>
#include <PubSubClient.h>
#include <WiFi.h>
#include <esp_sntp.h>
#include "esp_camera.h"
#include "img_converters.h"
#include "selftest.h"
#include "servo_drv.h"
#include "face.h"
#include "leds.h"
#include "motion.h"
#include "params.h"
#include "stream.h"
#include "voice.h"
#include "wsclient.h"
#include "root_ca.h"
#include "thai_font.h"

#if __has_include("flybot_config.h")
#include "flybot_config.h"
#else
#warning "src/flybot_config.h not found, building with flybot_config.example.h"
#include "flybot_config.example.h"
#endif
#ifndef SELF_TEST_FULL  // configs copied from an older example
#define SELF_TEST_FULL 0
#endif

using namespace m5avatar;

static constexpr int FRAME_W = 160;  // what the motion detector works on (QQVGA luma)
static constexpr int FRAME_H = 120;
#ifndef CAMERA_COLOR  // 1: the camera captures colour QVGA (snapshots for the vision model), the
#define CAMERA_COLOR 1  // detector subsamples it; 0: grayscale QQVGA as before
#endif

Avatar avatar;
WiFiClient wifiClient;
WsClient wssClient(KKH_ROOT_CA);  // MQTT host "wss://host/path": through the public HTTPS endpoint
PubSubClient mqtt(wifiClient);
static String mqttHost;  // host name only (setServer keeps the pointer)
String topicBase = MQTT_BASE_TOPIC;

// In PSRAM: internal RAM is kept for TLS (mbedTLS allocates only there in this core).
static uint8_t* prevFrame = nullptr;    // FRAME_W * FRAME_H, allocated in setup()
static uint8_t* changedMask = nullptr;  // for the camera monitor
static bool havePrev = false;
static bool cameraOk = false;
static bool proximityOk = false;
static uint32_t egoMotionUntil = 0;  // until then the camera sees its own head turning

// Latest values for the monitor page (built into /status JSON every 300 ms).
static struct {
  uint32_t n = 0, frames = 0, commands = 0;
  bool detected = false, ego = false;
  int box[4] = {0, 0, 0, 0};
  float gyro[3] = {}, accel[3] = {}, pan = 0, tilt = 0;
  uint16_t ps = 0, als = 0;
  String face = "", brain = "{}";
  bool faceFound = false;
  float faceX = 0, faceY = 0;
  uint16_t faceMs = 0;
  int battery = -1;  // %, -1 = unknown
  bool charging = false;
  String reaction = "";
} tele;
static bool faceDirty = false;  // a reaction changed the face: put the bridge's back

// -- tone queue: [Hz, ms] pairs played without blocking the main loop -------
struct Tone { uint16_t hz; uint16_t ms; };
static Tone tones[16];
static int toneCount = 0, toneIndex = 0;
static uint32_t toneUntil = 0;

static void serviceTones() {
  if (voiceOwnsAudio()) return;  // the mic or a reply holds the I2S bus
  if (toneIndex >= toneCount || millis() < toneUntil) return;
  const Tone& t = tones[toneIndex++];
  if (t.hz > 0) M5.Speaker.tone(t.hz, t.ms);  // freq 0 = rest
  toneUntil = millis() + t.ms;
}

// -- camera ------------------------------------------------------------------
static bool initCamera() {
  camera_config_t cfg = {};  // CoreS3 GC0308 pin map (M5CoreS3 GC0308.cpp)
  cfg.pin_pwdn = -1;
  cfg.pin_reset = -1;
  cfg.pin_xclk = -1;
  cfg.pin_sccb_sda = 12;
  cfg.pin_sccb_scl = 11;
  cfg.pin_d7 = 47;
  cfg.pin_d6 = 48;
  cfg.pin_d5 = 16;
  cfg.pin_d4 = 15;
  cfg.pin_d3 = 42;
  cfg.pin_d2 = 41;
  cfg.pin_d1 = 40;
  cfg.pin_d0 = 39;
  cfg.pin_vsync = 46;
  cfg.pin_href = 38;
  cfg.pin_pclk = 45;
  cfg.xclk_freq_hz = 20000000;
  cfg.ledc_timer = LEDC_TIMER_0;
  cfg.ledc_channel = LEDC_CHANNEL_0;
  cfg.pixel_format = CAMERA_COLOR ? PIXFORMAT_RGB565 : PIXFORMAT_GRAYSCALE;
  cfg.frame_size = CAMERA_COLOR ? FRAMESIZE_QVGA : FRAMESIZE_QQVGA;
  cfg.fb_count = 2;
  cfg.fb_location = CAMERA_FB_IN_PSRAM;
  cfg.grab_mode = CAMERA_GRAB_LATEST;
  cfg.sccb_i2c_port = -1;

  // The camera's SCCB shares the internal I2C bus with the IMU, LTR-553 and touch.
  // Hand it to the camera driver for init, then take it back: SCCB is not used again.
  M5.In_I2C.release();
  bool ok = esp_camera_init(&cfg) == ESP_OK;
  if (!ok) {  // fall back to RGB565, converted to luma below
    cfg.pixel_format = PIXFORMAT_RGB565;
    ok = esp_camera_init(&cfg) == ESP_OK;
  }
  M5.In_I2C.begin();
  // cam_task's small stack overflows when it prints (FB-SIZE / FB-OVF) and the chip resets
  esp_log_level_set("cam_hal", ESP_LOG_NONE);
  return ok;
}

static inline uint8_t luma(const camera_fb_t* fb, int i) {
  if (fb->format == PIXFORMAT_GRAYSCALE) return fb->buf[i];
  uint16_t p = (fb->buf[2 * i] << 8) | fb->buf[2 * i + 1];  // RGB565 big endian
  uint8_t r = (p >> 11) << 3, g = ((p >> 5) & 0x3F) << 2, b = (p & 0x1F) << 3;
  return (r * 77 + g * 150 + b * 29) >> 8;
}

// Frame differencing ~ the fly's motion detectors: the centroid of changed pixels is the
// target, the sign of the change (brighter/darker) is its ON/OFF polarity, the bounding
// box of the changed pixels feeds the looming detector (LPLC2 watches its four edges
// move outward) and the mean brightness of each image half drives phototaxis.
static void processCamera() {
  camera_fb_t* fb = esp_camera_fb_get();
  if (!fb) return;
  const int scale = fb->width / FRAME_W;  // 2 for colour QVGA: sample every other pixel
  if (scale < 1 || fb->width != FRAME_W * scale || fb->height != FRAME_H * scale) {
    esp_camera_fb_return(fb);
    return;
  }
  const uint32_t now = millis();
  uint32_t count = 0;
  int32_t sumX = 0, sumY = 0, sumSign = 0;
  int minX = FRAME_W, minY = FRAME_H, maxX = -1, maxY = -1;
  uint32_t lumL = 0, lumR = 0, lumT = 0, lumB = 0;
  for (int y = 0, i = 0; y < FRAME_H; ++y) {
    for (int x = 0; x < FRAME_W; ++x, ++i) {
      uint8_t v = luma(fb, (y * scale) * fb->width + x * scale);
      (x < FRAME_W / 2 ? lumL : lumR) += v;
      (y < FRAME_H / 2 ? lumT : lumB) += v;
      int d = int(v) - int(prevFrame[i]);
      prevFrame[i] = v;
      changedMask[i] = havePrev && abs(d) > P.diffThreshold;
      if (changedMask[i]) {
        ++count;
        sumX += x;
        sumY += y;
        sumSign += d > 0 ? 1 : -1;
        if (x < minX) minX = x;
        if (x > maxX) maxX = x;
        if (y < minY) minY = y;
        if (y > maxY) maxY = y;
      }
    }
  }
  faceOffer(fb, tele.pan, tele.tilt);  // copied only when the detector is free
  esp_camera_fb_return(fb);
  if (!havePrev) {
    havePrev = true;
    return;
  }

  static float lastX = 0, lastY = 0, vx = 0, vy = 0;
  static uint32_t lastSeen = 0;
  const bool ego = millis() < egoMotionUntil;  // the head is turning: the difference is our own motion
  const bool tooMuch = count > P.maxMotionFraction * FRAME_W * FRAME_H;
  const bool seen = count >= P.minMotionPixels;
  tele.n = count;
  tele.ego = ego || tooMuch;
  tele.detected = seen && !tele.ego;
  if (seen) tele.box[0] = minX, tele.box[1] = minY, tele.box[2] = maxX, tele.box[3] = maxY;
  ++tele.frames;
  streamFrame(prevFrame, changedMask, FRAME_W, FRAME_H, minX, minY, seen ? maxX : -1, maxY, ego || tooMuch);
#ifdef CAMERA_DEBUG  // publish every frame's changed-pixel count to tune the detector
  {
    char dbg[64];
    int n = snprintf(dbg, sizeof dbg, "{\"n\":%u,\"ego\":%d}", unsigned(count), ego);
    mqtt.publish((topicBase + "/debug/camera").c_str(), (const uint8_t*)dbg, n);
  }
#endif
  if (tooMuch || ego) return;  // whole image moved or the head is turning: keep the last target

  JsonDocument doc;
  if (count < P.minMotionPixels) {
    doc["detected"] = false;
    lastSeen = 0;
  } else {
    float cx = float(sumX) / count, cy = float(sumY) / count;
    if (lastSeen && now - lastSeen < 300) {
      float dt = (now - lastSeen) / 1000.0f;
      vx = 0.6f * vx + 0.4f * (cx - lastX) / dt;  // px/s, light smoothing
      vy = 0.6f * vy + 0.4f * (cy - lastY) / dt;
    } else {
      vx = vy = 0;
    }
    lastX = cx;
    lastY = cy;
    lastSeen = now;
    doc["x"] = cx;
    doc["y"] = cy;
    doc["vx"] = vx;
    doc["vy"] = vy;
    doc["width"] = FRAME_W;
    doc["height"] = FRAME_H;
    doc["polarity"] = float(sumSign) / count;
    JsonArray box = doc["box"].to<JsonArray>();
    box.add(minX);
    box.add(minY);
    box.add(maxX);
    box.add(maxY);
  }
  const float half = FRAME_W * FRAME_H / 2.0f;
  JsonArray lum = doc["lum"].to<JsonArray>();
  lum.add(int(lumL / half));
  lum.add(int(lumR / half));
  lum.add(int(lumT / half));
  lum.add(int(lumB / half));
  char buf[256];
  size_t n = serializeJson(doc, buf);
  mqtt.publish((topicBase + "/sensor/camera").c_str(), (const uint8_t*)buf, n);
}

// -- IMU / proximity -----------------------------------------------------------
static void publishBodyEvent(const char* type) {
  if (!mqtt.connected()) return;
  char buf[64];
  int n = snprintf(buf, sizeof buf, "{\"type\":\"%s\"}", type);
  mqtt.publish((topicBase + "/event/body").c_str(), (const uint8_t*)buf, n);
}

static void publishImu();

// Wall clock: NTP once online, kept in the CoreS3's RTC so it is right after a reboot offline.
static void serviceClock() {
  static bool ntpStarted = false, rtcWritten = false, rtcRead = false;
  if (!rtcRead) {
    rtcRead = true;
    if (M5.Rtc.isEnabled()) {
      const auto dt = M5.Rtc.getDateTime();  // UTC
      if (dt.date.year >= 2025) {
        struct tm t = {};
        t.tm_year = dt.date.year - 1900, t.tm_mon = dt.date.month - 1, t.tm_mday = dt.date.date;
        t.tm_hour = dt.time.hours, t.tm_min = dt.time.minutes, t.tm_sec = dt.time.seconds;
        setenv("TZ", "UTC0", 1);
        tzset();
        const timeval tv = {mktime(&t), 0};
        settimeofday(&tv, nullptr);
      }
    }
    setenv("TZ", "ICT-7", 1);
    tzset();
  }
  if (!ntpStarted && WiFi.status() == WL_CONNECTED) {
    ntpStarted = true;
    configTzTime("ICT-7", "pool.ntp.org", "time.google.com");
  }
  static uint32_t lastCheck = 0;
  if (rtcWritten || !ntpStarted || millis() - lastCheck < 10000) return;
  lastCheck = millis();
  if (sntp_get_sync_status() != SNTP_SYNC_STATUS_COMPLETED && time(nullptr) < 1735689600) return;  // not synced yet
  const time_t now = time(nullptr);
  if (now < 1735689600) return;
  if (M5.Rtc.isEnabled()) M5.Rtc.setDateTime(gmtime(&now));
  rtcWritten = true;
}

// Being handled (IMU), head-strip swipes, battery and the LED ring: these work offline too.
static void serviceBody() {
  static uint32_t lastImu = 0, lastPower = 0;
  const uint32_t now = millis();
  if (now - lastImu >= IMU_PERIOD_MS && M5.Imu.update()) {
    lastImu = now;
    float gx, gy, gz, ax, ay, az;
    M5.Imu.getGyro(&gx, &gy, &gz);
    M5.Imu.getAccel(&ax, &ay, &az);
    tele.gyro[0] = gx, tele.gyro[1] = gy, tele.gyro[2] = gz, tele.accel[0] = ax, tele.accel[1] = ay, tele.accel[2] = az;
    const char* ev = motionUpdate(ax, ay, az, gx, gy, gz);
    // a hand stroking or swiping the head jolts the IMU (0.3-0.46 g): that is not being lifted
    if (ev && voiceLastTouchMs() && now - voiceLastTouchMs() < 1500) {
      M5_LOGW("body: %s ignored (head touched)", ev);
      ev = nullptr;
    }
    if (ev) {
      M5_LOGW("body: %s", ev);
      tele.reaction = ev;
      voiceReact(ev);
      faceDirty = true;
      publishBodyEvent(ev);
    }
    if (mqtt.connected()) publishImu();
  }
  String gesture;
  if (voiceTakeGesture(&gesture)) {
    M5_LOGW("body: %s", gesture.c_str());
    publishBodyEvent(gesture.c_str());
  }
  if (!lastPower || now - lastPower >= 30000) {
    lastPower = now;
    tele.battery = M5.Power.getBatteryLevel();
    tele.charging = M5.Power.isCharging() == m5::Power_Class::is_charging;
    ledsBattery(tele.battery >= 0 && tele.battery <= 15 && !tele.charging);
    if (mqtt.connected()) {
      char buf[64];
      int n = snprintf(buf, sizeof buf, "{\"battery\":%d,\"charging\":%d}", tele.battery, tele.charging);
      mqtt.publish((topicBase + "/sensor/power").c_str(), (const uint8_t*)buf, n);
    }
  }
  ledsUpdate();
}

static void publishImu() {
  const float *g = tele.gyro, *a = tele.accel;
  const float gx = g[0], gy = g[1], gz = g[2], ax = a[0], ay = a[1], az = a[2];
  char buf[160];
  int n = snprintf(buf, sizeof buf, "{\"gyro\":[%.2f,%.2f,%.2f],\"accel\":[%.3f,%.3f,%.3f]}",
                   gx, gy, gz, ax, ay, az);
  mqtt.publish((topicBase + "/sensor/imu").c_str(), (const uint8_t*)buf, n);
}

static void publishProximity() {
  char buf[48];
  tele.ps = CoreS3.Ltr553.getPsValue();
  tele.als = CoreS3.Ltr553.getAlsValue();
  int n = snprintf(buf, sizeof buf, "{\"ps\":%u,\"als\":%u}", tele.ps, tele.als);
  mqtt.publish((topicBase + "/sensor/proximity").c_str(), (const uint8_t*)buf, n);
}

// -- commands ---------------------------------------------------------------
static void writeServos(float pan, float tilt) {
  static float lastPan = 0, lastTilt = 0;
  static uint32_t lastMs = 0;
  const uint32_t now = millis();
  const float dt = max(now - lastMs, 20u) / 1000.0f;
  const float speed = hypotf(pan - lastPan, tilt - lastTilt) / dt;
  if (speed > P.egoMotionDegS) egoMotionUntil = now + 150;  // one frame plus the servo settling
  lastPan = pan;
  lastTilt = tilt;
  lastMs = now;
  servoWrite(pan, tilt);
}

static Expression toExpression(const char* e) {
  if (!strcmp(e, "happy")) return Expression::Happy;
  if (!strcmp(e, "sleepy")) return Expression::Sleepy;
  if (!strcmp(e, "alert")) return Expression::Happy;  // someone came close: greet, not glare
  return Expression::Neutral;  // curious: the resting face of an assistant
}

static volatile bool snapshotRequested = false;
// <base>/camera/stream {"fps", "seconds"}: someone watches the dashboard camera, so send frames
// on our own instead of one per request (a round trip through the VPN took 0.3-3.7 s).
static uint32_t streamUntil = 0, streamEveryMs = 250, lastStreamMs = 0;
static String selfTestJson;  // published (retained) once MQTT connects
// Thai speech-balloon font; marks arrive pre-positioned by flybot.thai_text.shape()
static lgfx::PointerWrapper thaiFontData;
static lgfx::VLWfont thaiFont;

// One JPEG of the current frame for the bridge's LLM; streamed so it does not have
// to fit PubSubClient's buffer.
static void publishSnapshot() {
  camera_fb_t* fb = esp_camera_fb_get();
  if (!fb) return;
  uint8_t* jpg = nullptr;
  size_t len = 0;
  bool ok = frame2jpg(fb, 80, &jpg, &len);
  esp_camera_fb_return(fb);
  if (!ok) return;
  String topic = topicBase + "/snapshot";
  if (mqtt.beginPublish(topic.c_str(), len, false)) {
    mqtt.write(jpg, len);
    mqtt.endPublish();
  }
  free(jpg);
}

static void onCommand(char* topic, byte* payload, unsigned int len) {
  if (topicBase + "/snapshot/request" == topic) {
    snapshotRequested = true;
    return;
  }
  if (topicBase + "/camera/stream" == topic) {
    JsonDocument a;
    if (!deserializeJson(a, payload, len)) {
      streamEveryMs = 1000 / constrain(int(a["fps"] | 4), 1, 10);
      streamUntil = millis() + uint32_t(constrain(float(a["seconds"] | 20.0f), 1.0f, 60.0f) * 1000);
    }
    return;
  }
  if (topicBase + "/leds" == topic) {  // {"rgb": [r, g, b], "mode": "solid|blink|spin", "seconds": 5}
    JsonDocument a;
    if (!deserializeJson(a, payload, len))
      ledsSet(a["rgb"][0] | 0, a["rgb"][1] | 0, a["rgb"][2] | 0, a["mode"] | "solid", a["seconds"] | 5.0f);
    return;
  }
  if (topicBase + "/voice/progress" == topic) {  // still making the reply: {"say": "search" | "wait"}
    JsonDocument a;
    if (!deserializeJson(a, payload, len) && a["say"].is<const char*>()) voiceProgress(a["say"]);
    return;
  }
  if (topicBase + "/announce" == topic) {  // the bridge wants to say something: {"id": "..."}
    JsonDocument a;
    if (!deserializeJson(a, payload, len) && a["id"].is<const char*>()) voiceAnnounce(a["id"]);
    return;
  }
  JsonDocument doc;
  if (deserializeJson(doc, payload, len)) return;

  JsonObject servo = doc["servo"];
  if (!servo.isNull()) {
    tele.pan = servo["pan_angle"] | 0.0f;
    tele.tilt = servo["tilt_angle"] | 0.0f;
    writeServos(tele.pan, tele.tilt);
  }
  tele.brain = "";
  serializeJson(doc["brain"], tele.brain);
  ++tele.commands;

  static String lastFace;
  const char* face = doc["face"]["expression"];
  if (face && (lastFace != face || faceDirty) && !voiceBusy()) {  // keep the face steady while talking
    lastFace = face;
    faceDirty = false;
    tele.face = face;
    avatar.setExpression(toExpression(face));
  }

  static String lastText;
  const char* text = doc["text"];
  static uint32_t lastTextMs = 0;
  if (text && lastText != text && millis() - lastTextMs > 250) {  // balloon redraw is slow
    lastText = text;
    lastTextMs = millis();
    avatar.setSpeechText(text);
  }

  JsonObject audio = doc["audio"];
  if (!audio.isNull() && !voiceOwnsAudio()) {
    M5.Speaker.setVolume((audio["volume"] | 120) * P.speakerVolumePct / 100);
    toneCount = toneIndex = 0;
    for (JsonArray t : audio["tones"].as<JsonArray>()) {
      if (toneCount >= int(sizeof tones / sizeof *tones)) break;
      tones[toneCount++] = {t[0].as<uint16_t>(), t[1].as<uint16_t>()};
    }
    toneUntil = 0;
  }
}

// -- monitor page status (stream.cpp serves it) -------------------------------------
static void publishStatus() {
  static uint32_t last = 0, lastFrames = 0, lastCommands = 0;
  const uint32_t now = millis();
  if (now - last < 300) return;
  const float dt = (now - last) / 1000.0f;
  last = now;
  char head[640];
  snprintf(head, sizeof head,
           "{\"uptime\":%lu,\"wifi\":{\"ssid\":\"%s\",\"rssi\":%d,\"ip\":\"%s\"},\"mqtt\":%d,"
           "\"heap\":%u,\"psram\":%u,\"fps\":%.1f,\"cmd_hz\":%.1f,"
           "\"servo\":{\"pan\":%.1f,\"tilt\":%.1f},\"face\":\"%s\","
           "\"cam\":{\"n\":%lu,\"detected\":%d,\"skipped\":%d,\"box\":[%d,%d,%d,%d]},"
           "\"imu\":{\"gyro\":[%.1f,%.1f,%.1f],\"accel\":[%.2f,%.2f,%.2f]},\"ps\":%u,\"als\":%u,",
           (unsigned long)(now / 1000), WiFi.SSID().c_str(), WiFi.RSSI(), WiFi.localIP().toString().c_str(),
           mqtt.connected(), ESP.getFreeHeap(), ESP.getFreePsram(), (tele.frames - lastFrames) / dt,
           (tele.commands - lastCommands) / dt, tele.pan, tele.tilt, tele.face.c_str(), (unsigned long)tele.n,
           tele.detected, tele.ego, tele.box[0], tele.box[1], tele.box[2], tele.box[3], tele.gyro[0], tele.gyro[1],
           tele.gyro[2], tele.accel[0], tele.accel[1], tele.accel[2], tele.ps, tele.als);
  lastFrames = tele.frames;
  lastCommands = tele.commands;
  String s = head;
  s += "\"brain\":" + (tele.brain.length() ? tele.brain : String("{}"));
  char fbuf[96];
  snprintf(fbuf, sizeof fbuf, ",\"faceDet\":{\"found\":%d,\"x\":%.2f,\"y\":%.2f,\"ms\":%u}", tele.faceFound, tele.faceX,
           tele.faceY, tele.faceMs);
  s += fbuf;
  char bbuf[160];
  char clock[24] = "";
  const time_t wall = time(nullptr);
  if (wall > 1735689600) strftime(clock, sizeof clock, "%Y-%m-%d %H:%M:%S", localtime(&wall));
  snprintf(bbuf, sizeof bbuf, ",\"power\":{\"battery\":%d,\"charging\":%d},\"reaction\":\"%s\",\"time\":\"%s\"",
           tele.battery, tele.charging, tele.reaction.c_str(), clock);
  s += bbuf;
  s += ",\"motion\":" + motionStatusJson();
  s += ",\"voice\":" + voiceStatusJson();
  s += ",\"params\":" + paramsJson() + "}";
  streamSetStatus(s);
}

// -- connectivity -----------------------------------------------------------------
// Up to two networks, each with its own broker and voice URL (e.g. the hospital WiFi
// straight to the cluster, and home WiFi through a laptop relaying over its VPN).
struct Network { const char *ssid, *password, *mqttHost; uint16_t mqttPort; const char* voiceUrl; bool wakeName; };
static const Network NETWORKS[] = {
#ifdef VOICE_URL
    {WIFI_SSID, WIFI_PASSWORD, MQTT_HOST, MQTT_PORT, VOICE_URL, true},
#else
    {WIFI_SSID, WIFI_PASSWORD, MQTT_HOST, MQTT_PORT, "", true},
#endif
#ifdef WIFI2_SSID
#ifndef WIFI2_WAKE_NAME  // 0: answer every sentence on this network (e.g. at home, alone)
#define WIFI2_WAKE_NAME 1
#endif
    {WIFI2_SSID, WIFI2_PASSWORD, WIFI2_MQTT_HOST, WIFI2_MQTT_PORT, WIFI2_VOICE_URL, WIFI2_WAKE_NAME},
#endif
};
static constexpr int N_NETWORKS = sizeof NETWORKS / sizeof *NETWORKS;
static int network = 0;
static uint32_t lastJoin = 0;  // give each join 10 s before starting over

// The strongest known network in a scan (logged, so a weak or missing AP shows on serial).
static int pickNetwork() {
  int best = network, bestRssi = -1000;
  int found = WiFi.scanNetworks();
  for (int i = 0; i < found; ++i) {
    for (int n = 0; n < N_NETWORKS; ++n) {
      if (WiFi.SSID(i) == NETWORKS[n].ssid) {
        M5_LOGW("scan: %s rssi %d ch %d", NETWORKS[n].ssid, WiFi.RSSI(i), WiFi.channel(i));
        if (WiFi.RSSI(i) > bestRssi) bestRssi = WiFi.RSSI(i), best = n;
      }
    }
  }
  M5_LOGW("scan: %d networks, my mac %s", found, WiFi.macAddress().c_str());
  WiFi.scanDelete();
  return best;
}

static void joinNetwork() {
  network = pickNetwork();
  lastJoin = millis();
  const Network& n = NETWORKS[network];
  WiFi.begin(n.ssid, n.password);
  mqtt.disconnect();
  String host = n.mqttHost;
  if (host.startsWith("wss://")) {  // e.g. wss://stackchan.kkh.go.th/mqtt, port 443
    host = host.substring(6);
    const int slash = host.indexOf('/');
    wssClient.setPath(slash < 0 ? "/mqtt" : host.substring(slash));
    if (slash >= 0) host = host.substring(0, slash);
    mqtt.setClient(wssClient);
  } else {
    mqtt.setClient(wifiClient);
  }
  mqttHost = host;
  mqtt.setServer(mqttHost.c_str(), n.mqttPort);
  voiceSetUrl(n.voiceUrl, n.wakeName);
}

static void ensureConnected() {
  static bool loggedIp = false;
  if (WiFi.status() != WL_CONNECTED) {
    loggedIp = false;
    // auto-reconnect gives up after a run of auth failures, so start over every 10 s
    if (millis() - lastJoin > 10000) {
      M5_LOGW("WiFi not connected (status %d), rejoining", WiFi.status());
      WiFi.disconnect();
      joinNetwork();
    }
    return;
  }
  if (!loggedIp) {
    loggedIp = true;
    M5_LOGW("WiFi %s ip %s gw %s rssi %d", WiFi.SSID().c_str(), WiFi.localIP().toString().c_str(),
            WiFi.gatewayIP().toString().c_str(), WiFi.RSSI());
  }
  if (mqtt.connected()) return;
  static uint32_t lastTry = 0;
  if (millis() - lastTry < 2000) return;
  lastTry = millis();
  String id = "stackchan-" + String((uint32_t)ESP.getEfuseMac(), HEX);
  bool ok = strlen(MQTT_USER) ? mqtt.connect(id.c_str(), MQTT_USER, MQTT_PASSWORD) : mqtt.connect(id.c_str());
  if (ok) {
    mqtt.subscribe((topicBase + "/command").c_str());
    mqtt.subscribe((topicBase + "/snapshot/request").c_str());
    mqtt.subscribe((topicBase + "/announce").c_str(), 1);
    mqtt.subscribe((topicBase + "/voice/progress").c_str(), 0);
    mqtt.subscribe((topicBase + "/leds").c_str(), 0);
    mqtt.subscribe((topicBase + "/camera/stream").c_str(), 0);
    if (selfTestJson.length()) {
      mqtt.publish((topicBase + "/selftest").c_str(), (const uint8_t*)selfTestJson.c_str(),
                   selfTestJson.length(), true);
      selfTestJson = "";
    }
    M5_LOGI("MQTT connected as %s", id.c_str());
  } else {
    M5_LOGW("MQTT connect failed, state %d", mqtt.state());
  }
}

void setup() {
  auto cfg = M5.config();
  CoreS3.begin(cfg);
  // Logs go to the USB serial port; with nobody reading it a full buffer made every log call
  // wait (the mic task and web server stalled). Drop output instead of waiting.
  Serial.setTxTimeoutMs(0);
  paramsBegin();  // tunables saved from the monitor page

  Ltr5xx_Init_Basic_Para ltr = LTR5XX_BASE_PARA_CONFIG_DEFAULT;
  ltr.ps_led_pulse_freq = LTR5XX_LED_PULSE_FREQ_40KHZ;
  ltr.ps_measurement_rate = LTR5XX_PS_MEASUREMENT_RATE_50MS;
  proximityOk = CoreS3.Ltr553.begin(&ltr);
  if (proximityOk) {
    CoreS3.Ltr553.setPsMode(LTR5XX_PS_ACTIVE_MODE);
    CoreS3.Ltr553.setAlsMode(LTR5XX_ALS_ACTIVE_MODE);
  }

  // PSRAM is set up after static initialisation (Arduino 2.x): allocate here, not at the declaration
  prevFrame = (uint8_t*)ps_calloc(FRAME_W * FRAME_H, 1);
  changedMask = (uint8_t*)ps_calloc(FRAME_W * FRAME_H, 1);
  cameraOk = prevFrame && changedMask && initCamera();
  if (!cameraOk) M5_LOGE("camera init failed");
  if (cameraOk) faceBegin();

  bool servoOk = servoBegin();  // after the camera has handed the shared I2C bus back
  M5_LOGW("servo: %s", servoInfo());

  writeServos(0, 0);
  M5.Speaker.begin();
  M5.Speaker.setVolume(64 * P.speakerVolumePct / 100);  // boot beep; 64 = M5Unified default

  // touch the screen during the first 1.5 s for the full test (servo sweep)
  bool full = SELF_TEST_FULL;
  M5.Display.setFont(&fonts::Font2);
  M5.Display.drawString("Touch screen for full self-test", 6, 6);
  for (uint32_t t0 = millis(); !full && millis() - t0 < 1500; delay(20)) {
    M5.update();
    full = M5.Touch.getCount() > 0;
  }
  SelfTestReport report = runSelfTest(cameraOk, proximityOk, servoOk, servoInfo(), full, writeServos);
  selfTestJson = report.toJson();
  M5_LOGW("selftest %s", selfTestJson.c_str());
  delay(report.allOk() ? 2500 : 10000);  // leave failures on screen long enough to read

  avatar.init();
  thaiFontData.set(thai_font_vlw, thai_font_vlw_len);
  if (thaiFont.loadFont(&thaiFontData)) {
    avatar.setSpeechFont(&thaiFont);
  } else {
    M5_LOGE("Thai font failed to load");
  }
  avatar.setExpression(Expression::Neutral);

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  mqtt.setBufferSize(1024);  // command JSON with brain telemetry exceeds the 256 B default
  mqtt.setCallback(onCommand);
  voiceBegin();
  joinNetwork();
#ifdef WIFI_TX_POWER  // some APs drop the auth of a loud ESP32 (AUTH_EXPIRE)
  WiFi.setTxPower(WIFI_TX_POWER);
#endif
  streamBegin();
}

void loop() {
  M5.update();
  voiceUpdate();
  publishStatus();
  ensureConnected();
  mqtt.loop();
  serviceTones();
  servoUpdate();
  serviceBody();
  serviceClock();
  if (!mqtt.connected()) {
    if (cameraOk) {  // keep the frame buffers moving so the driver never runs out
      camera_fb_t* fb = esp_camera_fb_get();
      if (fb) esp_camera_fb_return(fb);
    }
    delay(10);
    return;
  }

  const bool streaming = int32_t(streamUntil - millis()) > 0 && millis() - lastStreamMs >= streamEveryMs;
  if (cameraOk && (snapshotRequested || streaming)) {
    snapshotRequested = false;
    lastStreamMs = millis();
    publishSnapshot();
  }
  if (cameraOk) processCamera();
  FaceResult face;
  if (faceTake(&face)) {
    tele.faceFound = face.found, tele.faceX = face.x, tele.faceY = face.y, tele.faceMs = face.inferMs;
    char buf[160];
    int n = snprintf(buf, sizeof buf,
                     "{\"found\":%d,\"x\":%.3f,\"y\":%.3f,\"w\":%.3f,\"score\":%.2f,\"pan\":%.1f,\"tilt\":%.1f,\"ms\":%u}",
                     face.found, face.x, face.y, face.w, face.score, face.pan, face.tilt, face.inferMs);
    mqtt.publish((topicBase + "/sensor/face").c_str(), (const uint8_t*)buf, n);
  }

  SoundDirection dir;
  if (voiceTakeDirection(&dir)) {
    char buf[120];
    int n = snprintf(buf, sizeof buf, "{\"lag\":%.2f,\"ild_db\":%.1f,\"corr\":%.2f,\"pan\":%.1f,\"tilt\":%.1f}",
                     dir.lag, dir.ildDb, dir.corr, tele.pan, tele.tilt);
    mqtt.publish((topicBase + "/sensor/sound").c_str(), (const uint8_t*)buf, n);
    M5_LOGW("sound: %s", buf);
  }

  static uint32_t lastPs = 0;
  const uint32_t now = millis();
  if (proximityOk && now - lastPs >= PROXIMITY_PERIOD_MS) {
    lastPs = now;
    publishProximity();
  }
}
