// Firmware update over WiFi. The bridge publishes {"path","size","md5"} on <base>/ota and serves
// the image at that path next to /api/voice. The MD5 comes over MQTT, not with the download, so
// an image changed on the way (plain http at home) is refused before it can boot.
#pragma once

#include <Arduino.h>

// Download and write the image to the other app slot; "" when it is ready to boot, else why not.
// progress(0..100) is called as it goes.
String otaRun(const String& url, size_t size, const String& md5, void (*progress)(int pct));
// A new image boots on trial: call once it has shown it works (MQTT up), or the next reset goes
// back to the previous one.
void otaConfirm();
