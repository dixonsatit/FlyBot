// Head servos: SG90 PWM (DIY StackChan) or SCS0009 bus servos (M5Stack StackChan, built
// with -DSTACKCHAN_OFFICIAL=1). pan_angle + = right, tilt_angle + = up, in degrees.
#pragma once

bool servoBegin();
void servoWrite(float pan, float tilt);
void servoUpdate();  // call from loop(): idle torque release
const char* servoInfo();  // one line for the serial log
