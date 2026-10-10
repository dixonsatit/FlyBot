// Tunables that the monitor page (stream.cpp) can change live. Defaults come from
// flybot_config.h; changes are kept in NVS ("flybot") across reboots.
#pragma once

#include <Arduino.h>

struct Params {
  int diffThreshold;        // per-pixel |change| counted as motion
  int minMotionPixels;      // fewer changed pixels -> "detected": false
  float maxMotionFraction;  // more -> the whole image moved, frame skipped
  float egoMotionDegS;      // frames are skipped while the head turns faster
  int speakerVolumePct;     // % of the bridge's tone volume (0 = mute)
  int voiceVolume;          // 0..255 for spoken replies
  float voiceGain;          // software gain on replies
  int vadMinRms;            // hands-free speech must be at least this loud ...
  float vadRatio;           // ... and this many times the room's noise
  float vadSpeechFrac;      // ... with at least this share of its energy at 100-1000 Hz (voices)
  int faceSwap;             // 1: swap the RGB565 bytes before face detection
  float faceScore;          // detector score threshold
  int faceTwoStage;         // 1: MSR01 candidates refined by MNP01 (slower, more accurate)
};

extern Params P;

void paramsBegin();
bool paramsSet(const char* name, const char* value);  // false: unknown name or out of range
bool paramsReset();                                   // back to the flybot_config.h defaults
String paramsJson();                                  // {"name": {"value", "min", "max"}, ...}
