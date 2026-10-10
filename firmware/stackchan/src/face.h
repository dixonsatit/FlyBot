// On-device face detection (Espressif esp-dl HumanFaceDetectMSR01) on the colour camera frame,
// in its own task so the main loop keeps its rate. Images never leave the robot for this.
#pragma once

#include <Arduino.h>
#include "esp_camera.h"

struct FaceResult {
  bool found = false;
  float x = 0, y = 0, w = 0;  // nearest (largest) face centre and width, as fractions of the frame
  float score = 0;
  uint32_t at = 0;            // millis() of the frame
  uint16_t inferMs = 0;       // how long the detector took
  float pan = 0, tilt = 0;    // head pose when the frame was taken
};

void faceBegin();
// Offer a frame (RGB565). Copied only when the detector is idle and FACE_PERIOD_MS has passed.
void faceOffer(const camera_fb_t* fb, float pan, float tilt);
// A result not yet taken (once per detection), or false.
bool faceTake(FaceResult* out);
