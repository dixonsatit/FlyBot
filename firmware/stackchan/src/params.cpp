#include "params.h"

#include <Preferences.h>

#if __has_include("flybot_config.h")
#include "flybot_config.h"
#else
#include "flybot_config.example.h"
#endif

#ifndef EGO_MOTION_DEG_S
#define EGO_MOTION_DEG_S 10
#endif
#ifndef SPEAKER_VOLUME_PCT
#define SPEAKER_VOLUME_PCT 100
#endif
#ifndef VOICE_VOLUME
#define VOICE_VOLUME 140
#endif
#ifndef VOICE_GAIN
#define VOICE_GAIN 1.0f
#endif
#ifndef VAD_MIN_RMS
#define VAD_MIN_RMS 400
#endif
#ifndef VAD_RATIO
#define VAD_RATIO 3.0f
#endif
#ifndef LED_BRIGHTNESS
#define LED_BRIGHTNESS 40
#endif
#ifndef VAD_SPEECH_FRAC
#define VAD_SPEECH_FRAC 0.4f
#endif

static const Params DEFAULTS = {DIFF_THRESHOLD, MIN_MOTION_PIXELS, MAX_MOTION_FRACTION, EGO_MOTION_DEG_S,
                                SPEAKER_VOLUME_PCT, VOICE_VOLUME, VOICE_GAIN, VAD_MIN_RMS, VAD_RATIO, VAD_SPEECH_FRAC, 0.25f, 25.0f, 0.8f, LED_BRIGHTNESS, 0, 0.3f, 0};
Params P = DEFAULTS;

struct Field {
  const char* name;  // also the NVS key (max 15 chars)
  bool isFloat;
  void* ptr;
  float min, max;
};
static const Field FIELDS[] = {
    {"diff_threshold", false, &P.diffThreshold, 1, 255},
    {"min_motion_px", false, &P.minMotionPixels, 1, 19200},
    {"max_motion_frac", true, &P.maxMotionFraction, 0.01f, 1},
    {"ego_deg_s", true, &P.egoMotionDegS, 1, 500},
    {"tone_volume_pct", false, &P.speakerVolumePct, 0, 100},
    {"voice_volume", false, &P.voiceVolume, 0, 255},
    {"voice_gain", true, &P.voiceGain, 0.1f, 4},
    {"vad_min_rms", false, &P.vadMinRms, 0, 20000},
    {"vad_ratio", true, &P.vadRatio, 1, 20},
    {"vad_speech_frac", true, &P.vadSpeechFrac, 0, 1},
    {"motion_lift_g", true, &P.motionLiftG, 0.05f, 2},
    {"motion_tilt_deg", true, &P.motionTiltDeg, 5, 90},
    {"motion_shake_g", true, &P.motionShakeG, 0.2f, 4},
    {"led_brightness", false, &P.ledBrightness, 0, 255},
    {"face_swap", false, &P.faceSwap, 0, 1},
    {"face_score", true, &P.faceScore, 0.05f, 0.95f},
    {"face_two_stage", false, &P.faceTwoStage, 0, 1},
};

static float get(const Field& f) { return f.isFloat ? *(float*)f.ptr : float(*(int*)f.ptr); }
static void put(const Field& f, float v) {
  if (f.isFloat) *(float*)f.ptr = v;
  else *(int*)f.ptr = int(lroundf(v));
}

void paramsBegin() {
  Preferences prefs;
  if (!prefs.begin("flybot", true)) return;  // nothing saved yet
  for (const Field& f : FIELDS) {
    if (prefs.isKey(f.name)) put(f, constrain(prefs.getFloat(f.name, get(f)), f.min, f.max));
  }
  prefs.end();
}

bool paramsSet(const char* name, const char* value) {
  char* end = nullptr;
  float v = strtof(value, &end);
  if (!end || end == value) return false;
  for (const Field& f : FIELDS) {
    if (strcmp(f.name, name)) continue;
    if (v < f.min || v > f.max) return false;
    put(f, v);
    Preferences prefs;
    if (prefs.begin("flybot", false)) {
      prefs.putFloat(f.name, get(f));
      prefs.end();
    }
    return true;
  }
  return false;
}

bool paramsReset() {
  P = DEFAULTS;
  Preferences prefs;
  if (!prefs.begin("flybot", false)) return false;
  prefs.clear();
  prefs.end();
  return true;
}

String paramsJson() {
  String s = "{";
  for (const Field& f : FIELDS) {
    if (s.length() > 1) s += ",";
    char buf[96];
    snprintf(buf, sizeof buf, "\"%s\":{\"value\":%g,\"min\":%g,\"max\":%g,\"float\":%d}", f.name, get(f), f.min,
             f.max, f.isFloat);
    s += buf;
  }
  return s + "}";
}
