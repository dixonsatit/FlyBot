#include "voice.h"

#include "params.h"

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

#if STACKCHAN_OFFICIAL
#include <Si12T.h>
static Si12T headTouch(SI12T_Type_Low, SI12T_Sensitivity_Level_3);  // as M5Stack's StackChan-BSP
#endif

extern m5avatar::Avatar avatar;

static constexpr uint32_t RATE = 16000;  // asr-typhoon: PCM 16 kHz mono s16le
static constexpr size_t BLOCK = 1600;    // 0.1 s
static constexpr size_t MAX_SAMPLES = RATE * 10;
static constexpr size_t MIN_SPEECH = RATE * 5 / 10;  // shorter is a cough or a click
static constexpr int PREROLL = 3;                    // blocks kept before speech starts
static constexpr int END_QUIET = 12;                 // blocks of quiet that end a sentence (a pause
                                                     // before the name must not split it off)
static constexpr size_t MAX_WAV = 4 * 1024 * 1024;

// Listening: mic on, waiting for speech. Capturing: recording a sentence. Waiting: the bridge
// is answering. Speaking: the reply plays (mic off, they share the I2S bus).
enum class State { Off, Listening, Capturing, Waiting, Speaking };
static volatile State state = State::Off;

static int16_t* rec = nullptr;
static volatile size_t recLen = 0;
static int16_t block[BLOCK];
static int16_t preroll[PREROLL][BLOCK];

static volatile bool micActive = false;  // main -> mic task: keep recording
static volatile bool micParked = true;   // mic task -> main: not inside record()
static volatile bool holding = false;    // screen held: talk without the wake name
static volatile bool uttReady = false;   // mic task -> main: a sentence is in rec
static volatile bool uttWake = false;    // it was heard hands-free (needs the wake name)

static uint8_t* wav = nullptr;
static volatile size_t wavLen = 0;
static volatile int httpCode = 0;
static String path;  // /api/voice[?wake=1] or /api/pat for the running request
static String voiceUrl;  // of the network we're on (empty: no voice there)
static volatile int32_t micLevel = 0;  // last block's RMS, for the monitor page
static volatile float micNoise = 0;

void voiceSetUrl(const char* url) { voiceUrl = url; }

bool voiceBusy() { return state == State::Capturing || state == State::Waiting || state == State::Speaking; }
bool voiceOwnsAudio() { return state != State::Off; }

String voiceStatusJson() {
  static const char* NAMES[] = {"off", "listening", "capturing", "waiting", "speaking"};
  char buf[160];
  snprintf(buf, sizeof buf, "{\"state\":\"%s\",\"level\":%ld,\"noise\":%.0f,\"threshold\":%.0f,\"url\":\"%s\"}",
           NAMES[int(state)], long(micLevel), micNoise, max(float(P.vadMinRms), micNoise * P.vadRatio),
           voiceUrl.c_str());
  return buf;
}

static int32_t rms(const int16_t* s, size_t n) {
  int64_t sum = 0;
  for (size_t i = 0; i < n; ++i) sum += int32_t(s[i]) * s[i];
  return int32_t(sqrtf(float(sum) / n));
}

// Energy VAD over 0.1 s blocks: a sentence starts after two loud blocks (keeping the
// blocks just before it) and ends after 0.8 s of quiet.
static void micTask(void*) {
  float noise = 0;
  int loud = 0, quiet = 0, ring = 0, warmup = 0;
  bool fromHold = false;
  for (;;) {
    if (!micActive) {
      micParked = true;
      warmup = 10;  // after each (re)start: 1 s to settle and learn the room before listening
      delay(10);
      continue;
    }
    micParked = false;
    if (!M5.Mic.record(block, BLOCK, RATE)) {
      delay(5);
      continue;
    }
    while (M5.Mic.isRecording()) delay(1);
    if (!micActive) continue;
    const int32_t level = rms(block, BLOCK);
    micLevel = level;
    micNoise = noise;

    if (state == State::Listening) {
      if (noise == 0 || level < noise * 2) noise = noise == 0 ? level : noise * 0.95f + level * 0.05f;
      memcpy(preroll[ring], block, sizeof block);
      ring = (ring + 1) % PREROLL;
      loud = level > max(float(P.vadMinRms), noise * P.vadRatio) ? loud + 1 : 0;
      if (warmup > 0) {
        --warmup;
        loud = 0;
      }
      fromHold = holding;
      if (loud >= 2 || fromHold) {
        size_t n = 0;
        if (!fromHold) {
          for (int i = 0; i < PREROLL; ++i, n += BLOCK) memcpy(rec + n, preroll[(ring + i) % PREROLL], sizeof block);
        }
        recLen = n;
        quiet = 0;
        state = State::Capturing;
        M5_LOGW("voice: capture (%s, level %d, noise %d)", fromHold ? "screen" : "speech", int(level), int(noise));
      }
      continue;
    }

    if (state == State::Capturing) {
      if (recLen + BLOCK <= MAX_SAMPLES) {
        memcpy(rec + recLen, block, sizeof block);
        recLen += BLOCK;
      }
      quiet = level < max(float(P.vadMinRms) * 0.7f, noise * P.vadRatio * 0.7f) ? quiet + 1 : 0;
      const bool full = recLen + BLOCK > MAX_SAMPLES;
      const bool done = fromHold ? (!holding || full) : (quiet >= END_QUIET || full);
      if (!done) continue;
      const size_t len = recLen, speech = fromHold ? len : len - min(len, size_t(quiet) * BLOCK);
      loud = quiet = 0;
      if (speech < MIN_SPEECH) {
        state = State::Listening;
        continue;
      }
      uttWake = !fromHold;
      uttReady = true;
      micActive = false;  // main stops the mic and sends the sentence
    }
  }
}

// Scale the samples of a 16-bit PCM WAV in place (Wayu-TTS: 24 kHz mono, plain 44-byte header).
static void amplify(uint8_t* w, size_t len) {
  if (P.voiceGain == 1.0f || len < 44 || memcmp(w, "RIFF", 4) || memcmp(w + 8, "WAVE", 4) || w[34] != 16) return;
  int16_t* pcm = (int16_t*)(w + 44);
  for (size_t i = 0; i < (len - 44) / 2; ++i) {
    int32_t v = int32_t(pcm[i] * P.voiceGain);
    pcm[i] = v > 32767 ? 32767 : v < -32768 ? -32768 : v;
  }
}

static void sendTask(void*) {
  HTTPClient http;
  String url = voiceUrl;
  url.replace("/api/voice", path);
  http.begin(url);
  http.setAuthorization(VOICE_USER, VOICE_PASSWORD);
  http.addHeader("Content-Type", "application/octet-stream");
  http.setConnectTimeout(5000);
  http.setTimeout(60000);  // ASR + LLM + TTS
  const bool pat = path == "/api/pat";
  int code = http.POST(pat ? nullptr : (uint8_t*)rec, pat ? 0 : recLen * sizeof(int16_t));
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
      if (got == size_t(len)) {
        wavLen = got;
        amplify(wav, got);
      } else {
        code = -100;  // cut short
      }
    }
  }
  http.end();
  httpCode = code;
  state = wavLen ? State::Speaking : State::Off;  // Off: main restarts the mic
  vTaskDelete(nullptr);
}

static void stopMic() {
  micActive = false;
  while (!micParked) delay(1);
  M5.Mic.end();
}

static void startMic() {
  M5.Speaker.end();  // mic and speaker share the I2S bus
  M5.Mic.begin();
  micActive = true;
  state = State::Listening;
}

static void request(const char* p, const char* balloon) {
  path = p;
  wavLen = 0;
  httpCode = 0;
  if (balloon) avatar.setSpeechText(balloon);
  state = State::Waiting;
  if (xTaskCreate(sendTask, "voice", 8192, nullptr, 1, nullptr) != pdPASS) state = State::Off;
}

void voiceBegin() {
  rec = (int16_t*)ps_malloc(MAX_SAMPLES * sizeof(int16_t));
  if (!rec) {
    M5_LOGE("voice: no PSRAM for the recording buffer");
    return;
  }
#if STACKCHAN_OFFICIAL
  headTouch.begin();
#endif
  xTaskCreatePinnedToCore(micTask, "mic", 4096, nullptr, 2, nullptr, 0);
}

// Head stroke on the base's touch strip: held ~0.2 s, then a pause before the next one.
static bool patted() {
#if STACKCHAN_OFFICIAL
  static uint32_t lastPoll = 0, since = 0, lastPat = 0;
  if (millis() - lastPoll < 50) return false;
  lastPoll = millis();
  headTouch.read_touch_result();
  headTouch.parse_touch_result();
  const bool on = headTouch.point_type[0] || headTouch.point_type[1] || headTouch.point_type[2];
  if (!on) {
    since = 0;
    return false;
  }
  if (!since) since = millis();
  if (millis() - since < 200 || millis() - lastPat < 6000) return false;
  lastPat = millis();
  return true;
#else
  return false;
#endif
}

void voiceUpdate() {
  if (!rec) return;
  holding = M5.Touch.getCount() > 0 && M5.Touch.getDetail(0).isPressed();
  static State shown = State::Off;
  switch (state) {
    case State::Off:
      if (httpCode) {  // a request just ended without a reply
        if (httpCode != 200 && httpCode != 204) M5_LOGW("voice: bridge answered %d", httpCode);
        avatar.setSpeechText(httpCode == 200 || httpCode == 204 ? "" : "voice error");
        httpCode = 0;
      }
      if (WiFi.status() == WL_CONNECTED && voiceUrl.length()) startMic();
      return;

    case State::Listening:
      if (shown == State::Capturing) avatar.setSpeechText("");
      shown = state;
      if (patted()) {
        stopMic();
        avatar.setExpression(m5avatar::Expression::Happy);
        request("/api/pat", nullptr);
      }
      return;

    case State::Capturing:
      if (shown != State::Capturing) avatar.setSpeechText("listening...");
      shown = state;
      if (!uttReady) return;
      uttReady = false;
      stopMic();
      request(uttWake ? "/api/voice?wake=1" : "/api/voice", "...");
      return;

    case State::Waiting:
      shown = state;
      return;  // sendTask moves on to Speaking or Off

    case State::Speaking: {
      static bool started = false;
      shown = state;
      if (!started) {
        avatar.setSpeechText("");
        M5.Speaker.begin();
        M5.Speaker.setVolume(P.voiceVolume);
        started = M5.Speaker.playWav(wav, wavLen);
        if (!started) M5_LOGW("voice: playWav failed (%u bytes)", unsigned(wavLen));
      }
      if (started && M5.Speaker.isPlaying()) {
        avatar.setMouthOpenRatio(0.2f + 0.6f * ((millis() / 90) % 3) / 2.0f);
        return;
      }
      avatar.setMouthOpenRatio(0);
      M5.Speaker.setVolume(64 * P.speakerVolumePct / 100);
      free(wav);
      wav = nullptr;
      wavLen = 0;
      started = false;
      httpCode = 0;
      state = State::Off;  // restarts the mic
      return;
    }
  }
}

#else
void voiceBegin() {}
void voiceSetUrl(const char*) {}
void voiceUpdate() {}
bool voiceBusy() { return false; }
bool voiceOwnsAudio() { return false; }
String voiceStatusJson() { return "{\"state\":\"disabled\"}"; }
#endif
