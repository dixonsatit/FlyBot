#include "leds.h"

#include "params.h"
#include "servo_drv.h"
#include "voice.h"

static uint8_t overrideRgb[3];
static char overrideMode[8] = "";
static uint32_t overrideUntil = 0;
static bool batteryLow = false;

void ledsSet(uint8_t r, uint8_t g, uint8_t b, const char* mode, float seconds) {
  overrideRgb[0] = r, overrideRgb[1] = g, overrideRgb[2] = b;
  strlcpy(overrideMode, mode, sizeof overrideMode);
  overrideUntil = millis() + uint32_t(seconds * 1000);
}

void ledsBattery(bool low) { batteryLow = low; }

static void fill(uint8_t f[BASE_LEDS][3], uint8_t r, uint8_t g, uint8_t b, float k = 1) {
  for (int i = 0; i < BASE_LEDS; ++i) f[i][0] = r * k, f[i][1] = g * k, f[i][2] = b * k;
}

// one bright LED going round, with a fading tail
static void spin(uint8_t f[BASE_LEDS][3], uint8_t r, uint8_t g, uint8_t b, uint32_t now, uint32_t stepMs) {
  const int head = (now / stepMs) % BASE_LEDS;
  for (int i = 0; i < BASE_LEDS; ++i) {
    const int behind = (head - i + BASE_LEDS) % BASE_LEDS;
    const float k = behind == 0 ? 1.0f : behind == 1 ? 0.35f : behind == 2 ? 0.1f : 0.0f;
    f[i][0] = r * k, f[i][1] = g * k, f[i][2] = b * k;
  }
}

void ledsUpdate() {
  static uint32_t last = 0;
  static uint8_t shown[BASE_LEDS][3];
  static bool shownOnce = false;
  const uint32_t now = millis();
  if (!baseLedsOk() || now - last < 80) return;
  last = now;

  uint8_t f[BASE_LEDS][3];
  const float breath = 0.55f + 0.45f * sinf(now / 600.0f);
  const char* v = voiceStateName();
  if (overrideUntil && int32_t(overrideUntil - now) > 0) {  // the bridge asked (CI broke, reminder...)
    const uint8_t r = overrideRgb[0], g = overrideRgb[1], b = overrideRgb[2];
    if (!strcmp(overrideMode, "spin")) spin(f, r, g, b, now, 70);
    else if (!strcmp(overrideMode, "blink")) fill(f, r, g, b, (now / 300) % 2);
    else fill(f, r, g, b);
  } else if (!strcmp(v, "reacting")) {
    fill(f, 255, 90, 0, (now / 120) % 2);  // startled: orange flashes
  } else if (!strcmp(v, "capturing")) {
    fill(f, 0, 255, 60);  // hearing you
  } else if (!strcmp(v, "waiting")) {
    spin(f, 150, 0, 255, now, 60);  // thinking / looking it up
  } else if (!strcmp(v, "speaking")) {
    fill(f, 0, 200, 255, breath);
  } else if (batteryLow && (now / 1000) % 3 == 0) {
    fill(f, 255, 0, 0);  // battery low: a red flash every 3 s
  } else if (!strcmp(v, "listening")) {
    fill(f, 0, 40, 255, 0.25f + 0.15f * sinf(now / 1500.0f));  // waiting for the name: dim breathing blue
  } else {
    fill(f, 0, 0, 0);
  }
  for (int i = 0; i < BASE_LEDS; ++i)
    for (int c = 0; c < 3; ++c) f[i][c] = f[i][c] * P.ledBrightness / 255;
  if (shownOnce && !memcmp(f, shown, sizeof f)) return;  // the bus is shared: write only changes
  memcpy(shown, f, sizeof f);
  shownOnce = true;
  baseLedsShow(f);
}
