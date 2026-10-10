// Head servos: SG90 PWM (DIY StackChan) or SCS0009 bus servos (M5Stack StackChan, built
// with -DSTACKCHAN_OFFICIAL=1). pan_angle + = right, tilt_angle + = up, in degrees.
#pragma once

#include <stdint.h>

bool servoBegin();
void servoWrite(float pan, float tilt);
void servoUpdate();  // call from loop(): idle torque release
const char* servoInfo();  // one line for the serial log
uint32_t servoLastMoveMs();  // millis() of the last move command that turned a servo

// The base's ring of 12 RGB LEDs (M5Stack StackChan; driven by the same PY32 as the servo
// power). No-ops on other bodies.
static constexpr int BASE_LEDS = 12;
bool baseLedsOk();
void baseLedsShow(const uint8_t rgb[BASE_LEDS][3]);
