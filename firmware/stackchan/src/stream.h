// Monitor page (STREAM_PORT in flybot_config.h): http://<robot>/ shows live values (/status),
// tuning (/set, see params.h) and the camera, streamed as MJPEG from port STREAM_PORT + 1.
#pragma once

#include <Arduino.h>

#include <stdint.h>

void streamBegin();
// One analysed frame: luma, changed-pixel mask, the motion box (or none) and whether the
// frame was skipped because the head was turning.
void streamFrame(const uint8_t* luma, const uint8_t* changed, int w, int h, int minX, int minY, int maxX,
                 int maxY, bool skipped);
void streamSetStatus(const String& json);  // the /status body, rebuilt by the main loop
