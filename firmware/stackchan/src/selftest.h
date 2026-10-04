// Boot self-test: checks the hardware the FlyBot bridge depends on and shows the result
// on screen before the avatar starts. The IMU and LTR-553 are read after camera frames
// have been grabbed, because the camera's SCCB shares their I2C bus.
#pragma once

#include <Arduino.h>

struct SelfTestItem {
  const char* name;
  bool ok;
  char detail[40];
};

struct SelfTestReport {
  SelfTestItem items[12];
  int count = 0;
  bool full = false;

  bool allOk() const;
  void add(const char* name, bool ok, const char* fmt, ...);
  String toJson() const;
};

// full = also sweep the servos (watch that the head turns the way the screen says)
SelfTestReport runSelfTest(bool cameraOk, bool proximityOk, bool full, void (*writeServos)(float pan, float tilt));
