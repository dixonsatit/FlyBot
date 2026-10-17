#include "ota.h"

#include "net.h"

#include <HTTPClient.h>
#include <Update.h>
#include <esp_ota_ops.h>

// Arduino marks every image good as it boots; this leaves a new one on trial until otaConfirm().
extern "C" bool verifyRollbackLater() { return true; }

void otaConfirm() {
  esp_ota_img_states_t st;
  if (esp_ota_get_state_partition(esp_ota_get_running_partition(), &st) == ESP_OK && st == ESP_OTA_IMG_PENDING_VERIFY) {
    esp_ota_mark_app_valid_cancel_rollback();
    log_w("ota: new firmware confirmed");
  }
}

String otaRun(const String& url, size_t size, const String& md5, void (*progress)(int)) {
  if (md5.length() != 32) return "no md5";
  HTTPClient http;
  httpBegin(http, url);
  http.setTimeout(15000);
  const int code = http.GET();
  if (code != 200) {
    http.end();
    return "http " + String(code);
  }
  const int len = http.getSize();
  if (len <= 0 || size_t(len) != size) {
    http.end();
    return "size " + String(len) + " not " + String(size);
  }
  if (!Update.begin(len, U_FLASH)) {
    http.end();
    return String("begin: ") + Update.errorString();
  }
  Update.setMD5(md5.c_str());
  static uint8_t buf[4096];  // not on the loop task's stack
  WiFiClient* s = http.getStreamPtr();
  size_t done = 0;
  int lastPct = -1;
  for (uint32_t lastData = millis(); done < size_t(len) && millis() - lastData < 15000;) {
    const size_t avail = s->available();
    if (!avail) {
      if (!http.connected()) break;
      delay(2);
      continue;
    }
    const int n = s->readBytes(buf, std::min(avail, sizeof buf));
    if (n <= 0) continue;
    if (Update.write(buf, n) != size_t(n)) break;
    done += n;
    lastData = millis();
    const int pct = done * 100 / len;
    if (pct != lastPct && progress) progress(lastPct = pct);
  }
  http.end();
  if (done != size_t(len)) {
    const String why = Update.hasError() ? String(Update.errorString()) : "stalled";
    Update.abort();
    return "got " + String(done) + " of " + String(len) + ": " + why;
  }
  if (!Update.end()) return String("check: ") + Update.errorString();  // MD5 or image header
  return "";
}
