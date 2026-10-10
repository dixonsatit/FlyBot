// Voice turn with the bridge (VOICE_URL in flybot_config.h): hold the screen to talk, the
// recording goes to the bridge's /api/voice (Thai ASR -> LLM -> Thai TTS) and the WAV reply
// plays on the speaker.
#pragma once

#include <Arduino.h>

void voiceBegin();
// the bridge's /api/voice on the current network; wakeName: hands-free speech must call the name
void voiceSetUrl(const char* url, bool wakeName);
void voiceAnnounce(const char* id);  // play the bridge's /api/announce/<id> when free
void voiceUpdate();  // call every loop()
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
