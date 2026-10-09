// Camera monitor (STREAM_PORT in flybot_config.h): http://<robot>/stream is an MJPEG view of
// what the motion detector sees, served from its own task.
#pragma once

#include <stdint.h>

void streamBegin();
// One analysed frame: luma, changed-pixel mask, the motion box (or none) and whether the
// frame was skipped because the head was turning.
void streamFrame(const uint8_t* luma, const uint8_t* changed, int w, int h, int minX, int minY, int maxX,
                 int maxY, bool skipped);
