#include "face.h"

#if __has_include("flybot_config.h")
#include "flybot_config.h"
#else
#include "flybot_config.example.h"
#endif

#ifndef FACE_DETECT  // needs CAMERA_COLOR (RGB565 frames)
#define FACE_DETECT 1
#endif
#ifndef FACE_PERIOD_MS
#define FACE_PERIOD_MS 200
#endif

#if FACE_DETECT
#include "human_face_detect_mnp01.hpp"
#include "human_face_detect_msr01.hpp"
#include "params.h"

static uint16_t* frame = nullptr;  // PSRAM copy the detector works on
static int frameW = 0, frameH = 0;
static volatile bool busy = false;
static volatile bool fresh = false;
static FaceResult pending, result;
static TaskHandle_t task = nullptr;
static SemaphoreHandle_t lock;

static void faceTask(void*) {
  // Thresholds from Arduino's CameraWebServer example; the score and the stages are live params.
  float score = -1;
  HumanFaceDetectMSR01* single = nullptr;
  static HumanFaceDetectMSR01 first(0.1F, 0.5F, 10, 0.2F);
  static HumanFaceDetectMNP01 second(0.5F, 0.3F, 5);
  for (;;) {
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    if (P.faceSwap) {  // camera RGB565 is big-endian; try the other order
      for (int i = 0; i < frameW * frameH; ++i) frame[i] = __builtin_bswap16(frame[i]);
    }
    if (score != P.faceScore) {
      delete single;
      score = P.faceScore;
      single = new HumanFaceDetectMSR01(score, 0.5F, 10, 0.2F);
    }
    const uint32_t t0 = millis();
    std::list<dl::detect::result_t>& faces =
        P.faceTwoStage ? second.infer(frame, {frameH, frameW, 3}, first.infer(frame, {frameH, frameW, 3}))
                       : single->infer(frame, {frameH, frameW, 3});
    FaceResult r = pending;
    r.inferMs = millis() - t0;
    int best = 0;
    for (auto& f : faces) {
      const int w = f.box[2] - f.box[0], h = f.box[3] - f.box[1];
      if (w * h <= best) continue;
      best = w * h;
      r.found = true;
      r.x = (f.box[0] + w / 2.0f) / frameW;
      r.y = (f.box[1] + h / 2.0f) / frameH;
      r.w = float(w) / frameW;
      r.score = f.score;
    }
    xSemaphoreTake(lock, portMAX_DELAY);
    result = r;
    fresh = true;
    xSemaphoreGive(lock);
    busy = false;
  }
}

void faceBegin() {
  lock = xSemaphoreCreateMutex();
  xTaskCreatePinnedToCore(faceTask, "face", 8192, nullptr, 1, &task, 0);
}

void faceOffer(const camera_fb_t* fb, float pan, float tilt) {
  static uint32_t last = 0;
  if (busy || !task || fb->format != PIXFORMAT_RGB565 || millis() - last < FACE_PERIOD_MS) return;
  if (!frame || frameW != fb->width || frameH != fb->height) {
    free(frame);
    frame = (uint16_t*)ps_malloc(fb->len);
    if (!frame) return;
    frameW = fb->width;
    frameH = fb->height;
  }
  last = millis();
  memcpy(frame, fb->buf, fb->len);
  pending = FaceResult();
  pending.at = last;
  pending.pan = pan;
  pending.tilt = tilt;
  busy = true;
  xTaskNotifyGive(task);
}

bool faceTake(FaceResult* out) {
  if (!fresh || !lock) return false;
  xSemaphoreTake(lock, portMAX_DELAY);
  *out = result;
  fresh = false;
  xSemaphoreGive(lock);
  return true;
}

#else
void faceBegin() {}
void faceOffer(const camera_fb_t*, float, float) {}
bool faceTake(FaceResult*) { return false; }
#endif
