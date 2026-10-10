// MQTT over WebSocket over TLS, as an Arduino Client for PubSubClient: the robot reaches the
// broker through https://stackchan.kkh.go.th/mqtt from any network (RFC 6455, binary frames,
// subprotocol "mqtt"). Only what MQTT needs: masked binary frames out, data frames in as one
// byte stream, ping answered, close ends the connection.
#pragma once

#include <Client.h>
#include <WiFiClientSecure.h>

class WsClient : public Client {
 public:
  explicit WsClient(const char* caCert) { tls_.setCACert(caCert); }
  void setPath(const String& path) { path_ = path; }

  int connect(IPAddress ip, uint16_t port) override { return 0; }  // needs the host name (SNI)
  int connect(const char* host, uint16_t port) override;
  size_t write(uint8_t b) override { return write(&b, 1); }
  size_t write(const uint8_t* buf, size_t size) override;
  int available() override;
  int read() override;
  int read(uint8_t* buf, size_t size) override;
  int peek() override { return -1; }
  void flush() override { tls_.flush(); }
  void stop() override;
  uint8_t connected() override { return open_ && tls_.connected(); }
  operator bool() override { return connected(); }

 private:
  bool readHeader();  // the next data frame's header; control frames handled here
  bool sendFrame(uint8_t opcode, const uint8_t* data, size_t len);

  WiFiClientSecure tls_;
  String path_ = "/mqtt";
  bool open_ = false;
  size_t remaining_ = 0;  // payload bytes left in the current data frame
};
