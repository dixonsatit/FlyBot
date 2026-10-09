#include "voice.h"

#include <Avatar.h>
#include <HTTPClient.h>
#include <M5Unified.h>
#include <WiFi.h>

#if __has_include("flybot_config.h")
#include "flybot_config.h"
#else
#include "flybot_config.example.h"
#endif

#ifdef VOICE_URL
#ifndef VOICE_USER
#define VOICE_USER MQTT_USER
#endif
#ifndef VOICE_PASSWORD
#define VOICE_PASSWORD MQTT_PASSWORD
#endif
#ifndef SPEAKER_VOLUME_PCT
#define SPEAKER_VOLUME_PCT 100
#endif
#ifndef VOICE_VOLUME  // 0..255, separate from SPEAKER_VOLUME_PCT so speech stays audible
#define VOICE_VOLUME 140
#endif

extern m5avatar::Avatar avatar;

static constexpr uint32_t RATE = 16000;     // asr-typhoon: PCM 16 kHz mono s16le
static constexpr size_t BLOCK = 1600;       // 0.1 s per mic request
static constexpr size_t MAX_SAMPLES = RATE * 10;
static constexpr size_t MIN_SAMPLES = RATE * 4 / 10;  // shorter is a tap, not speech
static constexpr size_t MAX_WAV = 4 * 1024 * 1024;

enum class State { Idle, Listening, Waiting, Speaking };
static volatile State state = State::Idle;
static int16_t* rec = nullptr;
static size_t recLen = 0;
static uint8_t* wav = nullptr;
static volatile size_t wavLen = 0;
static volatile int httpCode = 0;

bool voiceBusy() { return state != State::Idle; }

void voiceBegin() {
  rec = (int16_t*)ps_malloc(MAX_SAMPLES * sizeof(int16_t));
  if (!rec) M5_LOGE("voice: no PSRAM for the recording buffer");
}

static void sendTask(void*) {
  HTTPClient http;
  http.begin(VOICE_URL);
  http.setAuthorization(VOICE_USER, VOICE_PASSWORD);
  http.addHeader("Content-Type", "application/octet-stream");
  http.setConnectTimeout(5000);
  http.setTimeout(60000);  // ASR + LLM + TTS
  int code = http.POST((uint8_t*)rec, recLen * sizeof(int16_t));
  if (code == 200) {
    int len = http.getSize();
    if (len > 0 && size_t(len) <= MAX_WAV && (wav = (uint8_t*)ps_malloc(len))) {
      WiFiClient* s = http.getStreamPtr();
      size_t got = 0;
      for (uint32_t t0 = millis(); got < size_t(len) && millis() - t0 < 20000;) {
        int n = s->read(wav + got, len - got);
        if (n > 0) got += n;
        else delay(2);
      }
      if (got == size_t(len)) wavLen = got;
      else code = -100;  // cut short
    }
  }
  http.end();
  httpCode = code;
  state = wavLen ? State::Speaking : State::Idle;
  vTaskDelete(nullptr);
}

static void stopListening() {
  while (M5.Mic.isRecording()) delay(1);
  M5.Mic.end();
  M5.Speaker.begin();
}

void voiceUpdate() {
  static uint32_t idleSince = 0;
  const bool touched = M5.Touch.getCount() > 0 && M5.Touch.getDetail(0).isPressed();
  switch (state) {
    case State::Idle:
      if (httpCode) {  // a turn just ended in sendTask
        if (httpCode != 200 && httpCode != 204) M5_LOGW("voice: bridge answered %d", httpCode);
        avatar.setSpeechText(httpCode == 200 || httpCode == 204 ? "" : "voice error");
        httpCode = 0;
        idleSince = millis();
      }
      if (!rec || !touched || WiFi.status() != WL_CONNECTED || millis() - idleSince < 500) return;
      M5.Speaker.end();  // mic and speaker share the I2S bus
      M5.Mic.begin();
      recLen = 0;
      avatar.setSpeechText("listening...");
      state = State::Listening;
      return;

    case State::Listening:
      if (touched && recLen + BLOCK <= MAX_SAMPLES) {
        if (M5.Mic.record(rec + recLen, BLOCK, RATE)) recLen += BLOCK;
        return;
      }
      stopListening();
      if (recLen < MIN_SAMPLES) {
        avatar.setSpeechText("");
        state = State::Idle;
        idleSince = millis();
        return;
      }
      avatar.setSpeechText("thinking...");
      wavLen = 0;
      httpCode = 0;
      state = State::Waiting;
      if (xTaskCreate(sendTask, "voice", 8192, nullptr, 1, nullptr) != pdPASS) state = State::Idle;
      return;

    case State::Waiting:
      return;  // sendTask moves on to Speaking or Idle

    case State::Speaking: {
      static bool started = false;
      if (!started) {
        avatar.setSpeechText("");
        M5.Speaker.setVolume(VOICE_VOLUME);
        started = M5.Speaker.playWav(wav, wavLen);
        if (!started) M5_LOGW("voice: playWav failed (%u bytes)", unsigned(wavLen));
      }
      if (started && M5.Speaker.isPlaying()) {
        avatar.setMouthOpenRatio(0.2f + 0.6f * ((millis() / 90) % 3) / 2.0f);
        return;
      }
      avatar.setMouthOpenRatio(0);
      M5.Speaker.setVolume(64 * SPEAKER_VOLUME_PCT / 100);
      free(wav);
      wav = nullptr;
      wavLen = 0;
      started = false;
      state = State::Idle;
      idleSince = millis();
      return;
    }
  }
}

#else
void voiceBegin() {}
void voiceUpdate() {}
bool voiceBusy() { return false; }
#endif
