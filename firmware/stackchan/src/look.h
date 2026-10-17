// The robot's colour face: round eyes with a highlight, a rounded mouth and blush, on a background
// that follows the expression. Drawn at 16-bit into a canvas in PSRAM: the library would put
// 150 KB in internal RAM, which WiFi, TLS, the microphone and the camera already share.
#pragma once

#include <Avatar.h>

void lookBegin(m5avatar::Avatar& avatar);
// Expression and its colours together; use instead of avatar.setExpression().
void lookExpression(m5avatar::Avatar& avatar, m5avatar::Expression e);
// Take the screen from the avatar (a QR, the camera preview): its draw task parks at the start
// of its next frame, before it touches the display bus, until lookRelease(). Stopping the task
// instead and drawing after a fixed wait panicked when a 16-bit frame was still being pushed.
bool lookHold(uint32_t timeoutMs = 1500);
void lookRelease();
