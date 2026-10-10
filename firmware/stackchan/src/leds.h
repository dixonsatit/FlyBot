// The base's RGB ring shows what the robot is doing: dim blue breathing while it listens for
// its name, green while it hears you, a purple dot going round while it thinks or looks
// something up, cyan while it speaks, orange flashes when startled, a red flash on low battery.
#pragma once

#include <Arduino.h>

void ledsUpdate();  // call every loop()
// The bridge's own signal for `seconds`: mode "solid", "blink" or "spin".
void ledsSet(uint8_t r, uint8_t g, uint8_t b, const char* mode, float seconds);
void ledsBattery(bool low);
