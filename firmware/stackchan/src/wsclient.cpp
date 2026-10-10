#include "wsclient.h"

#include <esp_random.h>
#include <mbedtls/base64.h>

static bool readExactly(WiFiClientSecure& c, uint8_t* buf, size_t n, uint32_t timeoutMs = 5000) {
  for (uint32_t t0 = millis(); n;) {
    const int got = c.read(buf, n);
    if (got > 0) {
      buf += got, n -= got;
      continue;
    }
    if (!c.connected() || millis() - t0 > timeoutMs) return false;
    delay(1);
  }
  return true;
}

int WsClient::connect(const char* host, uint16_t port) {
  stop();
  if (!tls_.connect(host, port)) return 0;
  uint8_t nonce[16], key[32];
  for (auto& b : nonce) b = esp_random();
  size_t keyLen = 0;
  mbedtls_base64_encode(key, sizeof key, &keyLen, nonce, sizeof nonce);
  tls_.printf("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
              "Sec-WebSocket-Key: %.*s\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: mqtt\r\n\r\n",
              path_.c_str(), host, int(keyLen), key);
  // "HTTP/1.1 101 Switching Protocols", headers, blank line
  String status = tls_.readStringUntil('\n');
  if (status.indexOf(" 101") < 0) {
    log_w("ws: upgrade refused: %s", status.c_str());
    tls_.stop();
    return 0;
  }
  for (uint32_t t0 = millis(); millis() - t0 < 5000;) {
    String line = tls_.readStringUntil('\n');
    if (line == "\r" || line.isEmpty()) break;
  }
  open_ = true;
  remaining_ = 0;
  return 1;
}

bool WsClient::sendFrame(uint8_t opcode, const uint8_t* data, size_t len) {
  uint8_t head[14];
  size_t h = 0;
  head[h++] = 0x80 | opcode;  // FIN
  if (len < 126) {
    head[h++] = 0x80 | len;  // client frames are masked
  } else if (len < 65536) {
    head[h++] = 0x80 | 126, head[h++] = len >> 8, head[h++] = len;
  } else {
    head[h++] = 0x80 | 127;
    for (int i = 7; i >= 0; --i) head[h++] = uint64_t(len) >> (8 * i);
  }
  const uint32_t m = esp_random();
  const uint8_t mask[4] = {uint8_t(m), uint8_t(m >> 8), uint8_t(m >> 16), uint8_t(m >> 24)};
  memcpy(head + h, mask, 4), h += 4;
  if (tls_.write(head, h) != h) return false;
  uint8_t chunk[256];
  for (size_t done = 0; done < len;) {
    const size_t n = min(sizeof chunk, len - done);
    for (size_t i = 0; i < n; ++i) chunk[i] = data[done + i] ^ mask[(done + i) & 3];
    if (tls_.write(chunk, n) != n) return false;
    done += n;
  }
  return true;
}

size_t WsClient::write(const uint8_t* buf, size_t size) {
  if (!open_ || !size) return 0;
  return sendFrame(0x2, buf, size) ? size : 0;
}

bool WsClient::readHeader() {
  while (open_ && !remaining_ && tls_.available() >= 2) {
    uint8_t h[2];
    if (!readExactly(tls_, h, 2)) return stop(), false;
    const uint8_t opcode = h[0] & 0x0F;
    uint64_t len = h[1] & 0x7F;
    if (len == 126 || len == 127) {
      uint8_t ext[8];
      const size_t n = len == 126 ? 2 : 8;
      if (!readExactly(tls_, ext, n)) return stop(), false;
      len = 0;
      for (size_t i = 0; i < n; ++i) len = (len << 8) | ext[i];
    }
    if (h[1] & 0x80) {  // servers must not mask, but skip a key if one came
      uint8_t key[4];
      if (!readExactly(tls_, key, 4)) return stop(), false;
    }
    if (opcode == 0x0 || opcode == 0x1 || opcode == 0x2) {  // continuation / text / binary: MQTT bytes
      remaining_ = len;
      continue;
    }
    uint8_t payload[125];  // control frames carry at most 125 bytes
    const size_t n = min<uint64_t>(len, sizeof payload);
    if (!readExactly(tls_, payload, n)) return stop(), false;
    if (opcode == 0x9) sendFrame(0xA, payload, n);  // ping -> pong
    else if (opcode == 0x8) return stop(), false;   // close
  }
  return remaining_ > 0;
}

int WsClient::available() {
  if (!readHeader()) return 0;
  return min<size_t>(remaining_, tls_.available());
}

int WsClient::read() {
  uint8_t b;
  return read(&b, 1) == 1 ? b : -1;
}

int WsClient::read(uint8_t* buf, size_t size) {
  if (!readHeader()) return -1;
  const int got = tls_.read(buf, min(size, remaining_));
  if (got > 0) remaining_ -= got;
  return got;
}

void WsClient::stop() {
  if (open_ && tls_.connected()) sendFrame(0x8, nullptr, 0);
  open_ = false;
  remaining_ = 0;
  tls_.stop();
}
