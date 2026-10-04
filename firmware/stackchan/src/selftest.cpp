#include "selftest.h"

#include <ArduinoJson.h>
#include <M5CoreS3.h>
#include <stdarg.h>

#include "esp_camera.h"

bool SelfTestReport::allOk() const {
  for (int i = 0; i < count; ++i)
    if (!items[i].ok) return false;
  return true;
}

void SelfTestReport::add(const char* name, bool ok, const char* fmt, ...) {
  if (count >= int(sizeof items / sizeof *items)) return;
  SelfTestItem& it = items[count++];
  it.name = name;
  it.ok = ok;
  va_list args;
  va_start(args, fmt);
  vsnprintf(it.detail, sizeof it.detail, fmt, args);
  va_end(args);
}

String SelfTestReport::toJson() const {
  JsonDocument doc;
  doc["ok"] = allOk();
  doc["full"] = full;
  JsonArray arr = doc["items"].to<JsonArray>();
  for (int i = 0; i < count; ++i) {
    JsonObject o = arr.add<JsonObject>();
    o["name"] = items[i].name;
    o["ok"] = items[i].ok;
    o["detail"] = items[i].detail;
  }
  String out;
  serializeJson(doc, out);
  return out;
}

static void show(const SelfTestReport& r, const char* footer) {
  auto& d = M5.Display;
  d.startWrite();
  d.fillScreen(TFT_BLACK);
  d.setFont(&fonts::Font2);
  d.setTextDatum(top_left);
  d.setTextColor(TFT_WHITE, TFT_BLACK);
  d.drawString(r.full ? "FlyBot self-test (full)" : "FlyBot self-test", 6, 4);
  for (int i = 0; i < r.count; ++i) {
    const SelfTestItem& it = r.items[i];
    int y = 26 + i * 18;
    d.setTextColor(it.ok ? TFT_GREEN : TFT_RED, TFT_BLACK);
    d.drawString(it.ok ? "OK  " : "FAIL", 6, y);
    d.setTextColor(TFT_WHITE, TFT_BLACK);
    d.drawString(it.name, 52, y);
    d.drawString(it.detail, 150, y);
  }
  d.setTextColor(TFT_YELLOW, TFT_BLACK);
  d.drawString(footer, 6, d.height() - 18);
  d.endWrite();
}

static bool readImu(float& accelG, float& gyroDps) {
  float ax, ay, az, gx, gy, gz;
  for (int i = 0; i < 20 && !M5.Imu.update(); ++i) delay(5);
  if (!M5.Imu.getAccel(&ax, &ay, &az) || !M5.Imu.getGyro(&gx, &gy, &gz)) return false;
  accelG = sqrtf(ax * ax + ay * ay + az * az);
  gyroDps = sqrtf(gx * gx + gy * gy + gz * gz);
  return true;
}

// Servo sweep with the expected direction on screen: if the head moves the other way,
// flip SERVO_X_SIGN / SERVO_Y_SIGN in flybot_config.h.
static void sweep(SelfTestReport& r, void (*writeServos)(float, float)) {
  struct Step { float pan, tilt; const char* say; };
  const Step steps[] = {{0, 0, "centre"}, {30, 0, "pan +30: head turns RIGHT"}, {-30, 0, "pan -30: head turns LEFT"},
                        {0, 0, "centre"}, {0, 20, "tilt +20: head looks UP"}, {0, 0, "centre"}};
  for (const Step& s : steps) {
    writeServos(s.pan, s.tilt);
    show(r, s.say);
    delay(1200);
  }
  r.add("servo dir", true, "check it matched");
}

SelfTestReport runSelfTest(bool cameraOk, bool proximityOk, bool full, void (*writeServos)(float, float)) {
  SelfTestReport r;
  r.full = full;
  show(r, "running...");

  size_t psram = ESP.getPsramSize();
  r.add("psram", psram > 0, "%u KB", unsigned(psram / 1024));

  r.add("camera", cameraOk, cameraOk ? "init ok" : "esp_camera_init failed");
  if (cameraOk) {
    int frames = 0;
    uint32_t sum = 0, px = 0;
    for (int i = 0; i < 4; ++i) {  // the first frames after init are often dark
      camera_fb_t* fb = esp_camera_fb_get();
      if (!fb) continue;
      ++frames;
      if (i == 3 && fb->format == PIXFORMAT_GRAYSCALE) {
        for (size_t k = 0; k < fb->len; k += 7) sum += fb->buf[k], ++px;
      }
      esp_camera_fb_return(fb);
    }
    r.add("frames", frames == 4, "%d/4, mean %d", frames, px ? int(sum / px) : -1);
  }

  // after the camera has used the shared bus: does the IMU still answer?
  float accel = 0, gyro = 0;
  bool imu = readImu(accel, gyro);
  r.add("imu accel", imu && accel > 0.7f && accel < 1.3f, imu ? "|a| %.2f g" : "no data", accel);
  r.add("imu gyro", imu && gyro < 20.0f, imu ? "%.1f dps (still?)" : "no data", gyro);

  if (proximityOk) {
    uint16_t ps = CoreS3.Ltr553.getPsValue();
    uint16_t als = CoreS3.Ltr553.getAlsValue();
    r.add("ltr553", ps <= 2047, "ps %u als %u", ps, als);
  } else {
    r.add("ltr553", false, "begin failed");
  }

  M5.Speaker.tone(1319, 80);
  r.add("speaker", M5.Speaker.isEnabled(), "beep");

  if (full) sweep(r, writeServos);
  return r;
}
