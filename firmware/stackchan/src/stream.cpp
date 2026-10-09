#include "stream.h"

#include <Arduino.h>

#if __has_include("flybot_config.h")
#include "flybot_config.h"
#else
#include "flybot_config.example.h"
#endif

#ifdef STREAM_PORT
#include <esp_http_server.h>
#include <img_converters.h>

static constexpr int W = 160, H = 120;
static constexpr uint32_t FRAME_MS = 200;  // 5 fps is enough to watch and keeps the CPU for the brain
static uint8_t view[W * H];
static SemaphoreHandle_t lock;
static volatile uint32_t viewSeq = 0;
static volatile int clients = 0;

void streamFrame(const uint8_t* luma, const uint8_t* changed, int w, int h, int minX, int minY, int maxX,
                 int maxY, bool skipped) {
  static uint32_t last = 0;
  if (!clients || w != W || h != H || millis() - last < FRAME_MS) return;
  last = millis();
  if (xSemaphoreTake(lock, 0) != pdTRUE) return;
  for (int i = 0; i < W * H; ++i) view[i] = changed[i] ? 255 : luma[i] / 2;  // motion bright, scene dim
  if (maxX >= 0) {
    for (int x = minX; x <= maxX; ++x) view[minY * W + x] = view[maxY * W + x] = 200;
    for (int y = minY; y <= maxY; ++y) view[y * W + minX] = view[y * W + maxX] = 200;
  }
  if (skipped) {  // a white frame: the head was turning, the detector ignored this one
    for (int x = 0; x < W; ++x) view[x] = view[(H - 1) * W + x] = 255;
    for (int y = 0; y < H; ++y) view[y * W] = view[y * W + W - 1] = 255;
  }
  ++viewSeq;
  xSemaphoreGive(lock);
}

static esp_err_t streamHandler(httpd_req_t* req) {
  static const char* BOUNDARY = "\r\n--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n";
  httpd_resp_set_type(req, "multipart/x-mixed-replace;boundary=frame");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  ++clients;
  uint32_t seen = 0;
  esp_err_t res = ESP_OK;
  while (res == ESP_OK) {
    if (viewSeq == seen) {
      delay(20);
      continue;
    }
    uint8_t* jpg = nullptr;
    size_t len = 0;
    xSemaphoreTake(lock, portMAX_DELAY);
    seen = viewSeq;
    bool ok = fmt2jpg(view, sizeof view, W, H, PIXFORMAT_GRAYSCALE, 70, &jpg, &len);
    xSemaphoreGive(lock);
    if (!ok) break;
    char head[96];
    int n = snprintf(head, sizeof head, BOUNDARY, unsigned(len));
    res = httpd_resp_send_chunk(req, head, n);
    if (res == ESP_OK) res = httpd_resp_send_chunk(req, (const char*)jpg, len);
    free(jpg);
  }
  --clients;
  return res;
}

static esp_err_t indexHandler(httpd_req_t* req) {
  static const char page[] =
      "<!doctype html><meta name=viewport content='width=device-width'><title>StackChan camera</title>"
      "<body style='margin:0;background:#111;color:#ccc;font:14px sans-serif;text-align:center'>"
      "<p>bright = changed pixels, box = detected motion, white border = skipped (head turning)</p>"
      "<img src='/stream' style='width:min(100vw,640px);image-rendering:pixelated'>";
  httpd_resp_set_type(req, "text/html");
  return httpd_resp_send(req, page, sizeof page - 1);
}

void streamBegin() {
  lock = xSemaphoreCreateMutex();
  httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
  cfg.server_port = STREAM_PORT;
  cfg.ctrl_port = STREAM_PORT + 32768;
  cfg.max_open_sockets = 3;
  httpd_handle_t server = nullptr;
  if (httpd_start(&server, &cfg) != ESP_OK) {
    log_e("stream: httpd_start failed");
    return;
  }
  httpd_uri_t index = {"/", HTTP_GET, indexHandler, nullptr};
  httpd_uri_t stream = {"/stream", HTTP_GET, streamHandler, nullptr};
  httpd_register_uri_handler(server, &index);
  httpd_register_uri_handler(server, &stream);
}

#else
void streamBegin() {}
void streamFrame(const uint8_t*, const uint8_t*, int, int, int, int, int, int, bool) {}
#endif
