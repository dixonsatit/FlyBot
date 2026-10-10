// Voice turn with the bridge (VOICE_URL in flybot_config.h): hold the screen to talk, the
// recording goes to the bridge's /api/voice (Thai ASR -> LLM -> Thai TTS) and the WAV reply
// plays on the speaker.
#pragma once

#include <Arduino.h>

void voiceBegin();
// the bridge's /api/voice on the current network; wakeName: hands-free speech must call the name
void voiceSetUrl(const char* url, bool wakeName);
void voiceAnnounce(const char* id);  // play the bridge's /api/announce/<id> when free
// The bridge is still making the reply: play the kept "search" or "wait" line meanwhile.
void voiceProgress(const char* name);
// The robot was lifted, tilted or shaken ("lift", "tilt", "shake"): say a kept line about it.
void voiceReact(const char* kind);
// A swipe on the head strip since the last call: "swipe_forward" (stops speech) or "swipe_back" (repeats).
bool voiceTakeGesture(String* name);
uint32_t voiceLastTouchMs();  // millis() of the last touch on the head strip (0: never)
void voiceUpdate();  // call every loop()
const char* voiceStateName();  // off, listening, capturing, waiting, speaking, reacting
bool voiceBusy();        // capturing, waiting or speaking: the face stays steady
bool voiceOwnsAudio();   // the mic or a reply holds the I2S bus: no tones (they would crash it)
String voiceStatusJson();  // for the monitor page
// Where the last voice came from: the delay of the right mic behind the left in samples at
// 16 kHz (sub-sample), the left-right level difference in dB and the peak correlation (0..1).
// Once per sentence, about 0.5 s into it.
struct SoundDirection {
  float lag, ildDb, corr;
};
bool voiceTakeDirection(SoundDirection* out);
size_t voiceRecording(const int16_t** pcm);  // the last sentence (16 kHz mono), for /rec.wav
