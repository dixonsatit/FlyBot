// Someone handling the robot, from the head's IMU: lifted, tilted or shaken. The head moves
// with the servos, so a reading only counts while they have been still for a moment.
#pragma once

#include <Arduino.h>

// accel in g, gyro in deg/s, at the IMU rate. Returns "lift", "tilt", "shake" or nullptr.
const char* motionUpdate(float ax, float ay, float az, float gx, float gy, float gz);
String motionStatusJson();  // for the monitor page
