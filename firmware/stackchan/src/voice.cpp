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
static int16_t stereo[BLOCK * 2];  // L/R pairs from the two mics; block is their mean
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
// The last turns, for the monitor page: what was sent, what came back, whether it played.
struct Turn { uint32_t at; uint16_t ms; char kind; int16_t code; uint32_t bytes; int8_t played; };
static Turn turns[10];
static int turnCount = 0;
static void logTurn(char kind, uint16_t ms) {
  turns[turnCount % 10] = {millis() / 1000, ms, kind, 0, 0, -1};
  ++turnCount;
}
static Turn& lastTurn() { return turns[(turnCount + 9) % 10]; }
static volatile int32_t micLevel = 0;  // last block's RMS, for the monitor page
static volatile float micNoise = 0;
static volatile float micShare = 0;  // voice-band share of the last block

static bool needName = true;  // hands-free speech must call the robot by name
static String pendingAnnounce;  // an announcement waiting for the robot to be free

void voiceAnnounce(const char* id) { pendingAnnounce = id; }

void voiceSetUrl(const char* url, bool wakeName) {
  voiceUrl = url;
  needName = wakeName;
}

bool voiceBusy() { return state == State::Capturing || state == State::Waiting || state == State::Speaking; }
bool voiceOwnsAudio() { return state != State::Off; }

size_t voiceRecording(const int16_t** pcm) {
  *pcm = rec;
  return rec ? recLen : 0;
}

String voiceStatusJson() {
  static const char* NAMES[] = {"off", "listening", "capturing", "waiting", "speaking"};
  char buf[200];
  snprintf(buf, sizeof buf,
           "{\"state\":\"%s\",\"level\":%ld,\"share\":%.2f,\"noise\":%.0f,\"threshold\":%.0f,\"url\":\"%s\",\"turns\":[",
           NAMES[int(state)], long(micLevel), micShare, micNoise, max(float(P.vadMinRms), micNoise * P.vadRatio),
           voiceUrl.c_str());
  String out = buf;
  for (int i = max(0, turnCount - 10); i < turnCount; ++i) {
    const Turn& t = turns[i % 10];
    char row[96];
    snprintf(row, sizeof row, "%s{\"t\":%lu,\"kind\":\"%c\",\"ms\":%u,\"code\":%d,\"bytes\":%lu,\"played\":%d}",
             i > max(0, turnCount - 10) ? "," : "", (unsigned long)t.at, t.kind, t.ms, t.code, (unsigned long)t.bytes,
             t.played);
    out += row;
  }
  return out + "]}";
}

// RBJ biquad, run over the mic blocks so its state carries across them.
struct Biquad {
  float b0, b1, b2, a1, a2, x1 = 0, x2 = 0, y1 = 0, y2 = 0;
  Biquad(float fc, bool highPass) {
    const float w = 2 * PI * fc / RATE, a = sinf(w) / (2 * 0.7071f), c = cosf(w), a0 = 1 + a;
    b0 = b2 = (highPass ? (1 + c) : (1 - c)) / 2 / a0;
    b1 = (highPass ? -(1 + c) : (1 - c)) / a0;
    a1 = -2 * c / a0;
    a2 = (1 - a) / a0;
  }
  float operator()(float x) {
    const float y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2;
    x2 = x1, x1 = x, y2 = y1, y1 = y;
    return y;
  }
};

// RMS of the voice band (100-1000 Hz), and that band's share of the whole block's RMS. A
// steady ~4 kHz noise in the room is as loud as a voice in this band but has a share of
// 0.07-0.15, while speech has 0.85-0.95 (0.3+ even with that noise on top).
static int32_t voiceBand(const int16_t* s, size_t n, float* share) {
  static Biquad hp(100, true), lp(1000, false);
  double band = 0, all = 0;
  for (size_t i = 0; i < n; ++i) {
    const float v = lp(hp(s[i]));
    band += v * v;
    all += float(s[i]) * s[i];
  }
  *share = all > 0 ? sqrtf(band / all) : 0;
  return int32_t(sqrtf(band / n));
}

// Direction from the two mics: cross-correlate them over the voice blocks of a sentence
// (300-3000 Hz, so room rumble and the 4 kHz noise weigh little) and take the best lag.
static constexpr int MAX_LAG = 8;            // samples; the mics are a few cm apart
static constexpr int DIRECTION_BLOCKS = 5;   // voice blocks to collect before reporting
static double xcorr[2 * MAX_LAG + 1], energyL, energyR;
static int dirBlocks = 0;
static volatile bool dirReady = false;
static SoundDirection dirResult;
static float bandL[BLOCK], bandR[BLOCK];

static void directionReset() {
  memset(xcorr, 0, sizeof xcorr);
  energyL = energyR = 0;
  dirBlocks = 0;
}

static void directionAdd() {
  static Biquad hpL(300, true), lpL(3000, false), hpR(300, true), lpR(3000, false);
  for (size_t i = 0; i < BLOCK; ++i) {
    bandL[i] = lpL(hpL(stereo[2 * i]));
    bandR[i] = lpR(hpR(stereo[2 * i + 1]));
  }
  for (size_t i = MAX_LAG; i < BLOCK - MAX_LAG; ++i) {
    energyL += bandL[i] * bandL[i];
    energyR += bandR[i] * bandR[i];
    for (int k = -MAX_LAG; k <= MAX_LAG; ++k) xcorr[k + MAX_LAG] += bandL[i] * bandR[i + k];
  }
  if (++dirBlocks != DIRECTION_BLOCKS || dirReady) return;
  int best = 0;
  for (int k = 1; k <= 2 * MAX_LAG; ++k)
    if (xcorr[k] > xcorr[best]) best = k;
  float lag = best - MAX_LAG;
  if (best > 0 && best < 2 * MAX_LAG) {  // parabola through the peak and its neighbours
    const double a = xcorr[best - 1], b = xcorr[best], c = xcorr[best + 1], d = a - 2 * b + c;
    if (d < 0) lag += 0.5f * float((a - c) / d);
  }
  dirResult.lag = lag;
  dirResult.ildDb = 10 * log10f(float((energyL + 1) / (energyR + 1)));
  dirResult.corr = float(xcorr[best] / sqrt(energyL * energyR + 1));
  dirReady = true;
}

bool voiceTakeDirection(SoundDirection* out) {
  if (!dirReady) return false;
  *out = dirResult;
  dirReady = false;
  return true;
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
    if (!M5.Mic.record(stereo, BLOCK * 2, RATE, true)) {
      delay(5);
      continue;
    }
    while (M5.Mic.isRecording()) delay(1);
    if (!micActive) continue;
    for (size_t i = 0; i < BLOCK; ++i) block[i] = int16_t((int32_t(stereo[2 * i]) + stereo[2 * i + 1]) / 2);
    float share;
    const int32_t level = voiceBand(block, BLOCK, &share);  // only voices start or hold a sentence
    const bool voice = share >= P.vadSpeechFrac;
    micLevel = level;
    micShare = share;
    micNoise = noise;

    if (state == State::Listening) {
      if (noise == 0 || level < noise * 2) noise = noise == 0 ? level : noise * 0.95f + level * 0.05f;
      memcpy(preroll[ring], block, sizeof block);
      ring = (ring + 1) % PREROLL;
      loud = voice && level > max(float(P.vadMinRms), noise * P.vadRatio) ? loud + 1 : 0;
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
        directionReset();
        if (voice) directionAdd();
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
      quiet = !voice || level < max(float(P.vadMinRms) * 0.7f, noise * P.vadRatio * 0.7f) ? quiet + 1 : 0;
      if (!quiet) directionAdd();
      const bool full = recLen + BLOCK > MAX_SAMPLES;
      const bool done = fromHold ? (!holding || full) : (quiet >= END_QUIET || full);
      if (!done) continue;
      const size_t len = recLen, speech = fromHold ? len : len - min(len, size_t(quiet) * BLOCK);
      loud = quiet = 0;
      if (speech < MIN_SPEECH) {
        state = State::Listening;
        continue;
      }
      uttWake = !fromHold && needName;
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

// vTaskDelete() never returns, so locals' destructors don't run: the request lives in its own
// function and closes its socket explicitly (each turn leaked one until the 16 ran out).
static int voiceRequest() {
  HTTPClient http;
  http.setReuse(false);
  String url = voiceUrl;
  url.replace("/api/voice", path);
  http.begin(url);
  http.setAuthorization(VOICE_USER, VOICE_PASSWORD);
  http.addHeader("Content-Type", "application/octet-stream");
  http.setConnectTimeout(5000);
  http.setTimeout(60000);  // ASR + LLM + TTS
  const bool pat = path == "/api/pat";
  int code = path.startsWith("/api/announce/") ? http.GET()
                                                : http.POST(pat ? nullptr : (uint8_t*)rec, pat ? 0 : recLen * sizeof(int16_t));
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
  return code;
}

static void sendTask(void*) {
  httpCode = voiceRequest();
  if (turnCount) lastTurn().code = httpCode, lastTurn().bytes = wavLen;
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
  logTurn(strstr(p, "wake=1") ? 'w' : strstr(p, "/api/voice") ? 'v' : strstr(p, "/api/pat") ? 'p' : 'a',
          strstr(p, "/api/voice") ? recLen * 1000 / RATE : 0);
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
      } else if (pendingAnnounce.length()) {  // speak up on our own (reminder, CI news)
        String id = pendingAnnounce;
        pendingAnnounce = "";
        stopMic();
        request(("/api/announce/" + id).c_str(), nullptr);
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
        if (turnCount) lastTurn().played = started;
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
void voiceSetUrl(const char*, bool) {}
void voiceAnnounce(const char*) {}
void voiceUpdate() {}
bool voiceBusy() { return false; }
bool voiceOwnsAudio() { return false; }
String voiceStatusJson() { return "{\"state\":\"disabled\"}"; }
size_t voiceRecording(const int16_t** pcm) {
  *pcm = nullptr;
  return 0;
}
#endif
