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
#include "esp_camera.h"
#include "img_converters.h"
#include "selftest.h"
#include "servo_drv.h"
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
#ifndef SPEAKER_VOLUME_PCT  // scales the bridge's audio volume; 0 = mute
#define SPEAKER_VOLUME_PCT 100
#endif

using namespace m5avatar;

static constexpr int FRAME_W = 160;  // FRAMESIZE_QQVGA
static constexpr int FRAME_H = 120;

Avatar avatar;
WiFiClient wifiClient;
PubSubClient mqtt(wifiClient);
String topicBase = MQTT_BASE_TOPIC;

static uint8_t prevFrame[FRAME_W * FRAME_H];
static bool havePrev = false;
static bool cameraOk = false;
static bool proximityOk = false;

// -- tone queue: [Hz, ms] pairs played without blocking the main loop -------
struct Tone { uint16_t hz; uint16_t ms; };
static Tone tones[16];
static int toneCount = 0, toneIndex = 0;
static uint32_t toneUntil = 0;

static void serviceTones() {
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
  cfg.pixel_format = PIXFORMAT_GRAYSCALE;
  cfg.frame_size = FRAMESIZE_QQVGA;
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
  if (fb->width != FRAME_W || fb->height != FRAME_H) {
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
      uint8_t v = luma(fb, i);
      (x < FRAME_W / 2 ? lumL : lumR) += v;
      (y < FRAME_H / 2 ? lumT : lumB) += v;
      int d = int(v) - int(prevFrame[i]);
      prevFrame[i] = v;
      if (havePrev && abs(d) > DIFF_THRESHOLD) {
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
  esp_camera_fb_return(fb);
  if (!havePrev) {
    havePrev = true;
    return;
  }

  static float lastX = 0, lastY = 0, vx = 0, vy = 0;
  static uint32_t lastSeen = 0;
  if (count > MAX_MOTION_FRACTION * FRAME_W * FRAME_H) return;  // ego-motion: keep the last target

  JsonDocument doc;
  if (count < MIN_MOTION_PIXELS) {
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
static void publishImu() {
  float gx, gy, gz, ax, ay, az;
  M5.Imu.getGyro(&gx, &gy, &gz);
  M5.Imu.getAccel(&ax, &ay, &az);
  char buf[160];
  int n = snprintf(buf, sizeof buf, "{\"gyro\":[%.2f,%.2f,%.2f],\"accel\":[%.3f,%.3f,%.3f]}",
                   gx, gy, gz, ax, ay, az);
  mqtt.publish((topicBase + "/sensor/imu").c_str(), (const uint8_t*)buf, n);
}

static void publishProximity() {
  char buf[48];
  int n = snprintf(buf, sizeof buf, "{\"ps\":%u,\"als\":%u}", CoreS3.Ltr553.getPsValue(),
                   CoreS3.Ltr553.getAlsValue());
  mqtt.publish((topicBase + "/sensor/proximity").c_str(), (const uint8_t*)buf, n);
}

// -- commands ---------------------------------------------------------------
static void writeServos(float pan, float tilt) { servoWrite(pan, tilt); }

static Expression toExpression(const char* e) {
  if (!strcmp(e, "happy")) return Expression::Happy;
  if (!strcmp(e, "sleepy")) return Expression::Sleepy;
  if (!strcmp(e, "alert")) return Expression::Angry;
  return Expression::Neutral;  // curious: the resting face of an assistant
}

static volatile bool snapshotRequested = false;
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
  JsonDocument doc;
  if (deserializeJson(doc, payload, len)) return;

  JsonObject servo = doc["servo"];
  if (!servo.isNull()) writeServos(servo["pan_angle"] | 0.0f, servo["tilt_angle"] | 0.0f);

  static String lastFace;
  const char* face = doc["face"]["expression"];
  if (face && lastFace != face) {
    lastFace = face;
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
  if (!audio.isNull()) {
    M5.Speaker.setVolume((audio["volume"] | 120) * SPEAKER_VOLUME_PCT / 100);
    toneCount = toneIndex = 0;
    for (JsonArray t : audio["tones"].as<JsonArray>()) {
      if (toneCount >= int(sizeof tones / sizeof *tones)) break;
      tones[toneCount++] = {t[0].as<uint16_t>(), t[1].as<uint16_t>()};
    }
    toneUntil = 0;
  }
}

// -- connectivity -----------------------------------------------------------------
static void ensureConnected() {
  static bool loggedIp = false;
  static uint32_t lastJoin = 0;
  if (WiFi.status() != WL_CONNECTED) {
    loggedIp = false;
    // auto-reconnect gives up after a run of auth failures, so start over every 10 s
    if (millis() - lastJoin > 10000) {
      lastJoin = millis();
      M5_LOGW("WiFi not connected (status %d), joining %s", WiFi.status(), WIFI_SSID);
      WiFi.disconnect();
      WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
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

  Ltr5xx_Init_Basic_Para ltr = LTR5XX_BASE_PARA_CONFIG_DEFAULT;
  ltr.ps_led_pulse_freq = LTR5XX_LED_PULSE_FREQ_40KHZ;
  ltr.ps_measurement_rate = LTR5XX_PS_MEASUREMENT_RATE_50MS;
  proximityOk = CoreS3.Ltr553.begin(&ltr);
  if (proximityOk) {
    CoreS3.Ltr553.setPsMode(LTR5XX_PS_ACTIVE_MODE);
    CoreS3.Ltr553.setAlsMode(LTR5XX_ALS_ACTIVE_MODE);
  }

  cameraOk = initCamera();
  if (!cameraOk) M5_LOGE("camera init failed");

  bool servoOk = servoBegin();  // after the camera has handed the shared I2C bus back
  M5_LOGW("servo: %s", servoInfo());

  writeServos(0, 0);
  M5.Speaker.begin();
  M5.Speaker.setVolume(64 * SPEAKER_VOLUME_PCT / 100);  // boot beep; 64 = M5Unified default

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
  int found = WiFi.scanNetworks();  // logged so a weak or missing AP is visible on the serial port
  for (int i = 0; i < found; ++i) {
    if (WiFi.SSID(i) == WIFI_SSID) {
      M5_LOGW("scan: %s rssi %d ch %d bssid %s auth %d", WIFI_SSID, WiFi.RSSI(i), WiFi.channel(i),
              WiFi.BSSIDstr(i).c_str(), WiFi.encryptionType(i));
    }
  }
  M5_LOGW("scan: %d networks, my mac %s", found, WiFi.macAddress().c_str());
  WiFi.scanDelete();
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
#ifdef WIFI_TX_POWER  // some APs drop the auth of a loud ESP32 (AUTH_EXPIRE)
  WiFi.setTxPower(WIFI_TX_POWER);
#endif
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setBufferSize(1024);  // command JSON with brain telemetry exceeds the 256 B default
  mqtt.setCallback(onCommand);
}

void loop() {
  M5.update();
  ensureConnected();
  mqtt.loop();
  serviceTones();
  servoUpdate();
  if (!mqtt.connected()) {
    if (cameraOk) {  // keep the frame buffers moving so the driver never runs out
      camera_fb_t* fb = esp_camera_fb_get();
      if (fb) esp_camera_fb_return(fb);
    }
    delay(10);
    return;
  }

  if (cameraOk && snapshotRequested) {
    snapshotRequested = false;
    publishSnapshot();
  }
  if (cameraOk) processCamera();

  static uint32_t lastImu = 0, lastPs = 0;
  const uint32_t now = millis();
  if (now - lastImu >= IMU_PERIOD_MS && M5.Imu.update()) {
    lastImu = now;
    publishImu();
  }
  if (proximityOk && now - lastPs >= PROXIMITY_PERIOD_MS) {
    lastPs = now;
    publishProximity();
  }
}
