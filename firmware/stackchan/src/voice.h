// Voice turn with the bridge (VOICE_URL in flybot_config.h): hold the screen to talk, the
// recording goes to the bridge's /api/voice (Thai ASR -> LLM -> Thai TTS) and the WAV reply
// plays on the speaker.
#pragma once

#include <Arduino.h>

void voiceBegin();
// the bridge's /api/voice on the current network; wakeName: hands-free speech must call the name
void voiceSetUrl(const char* url, bool wakeName);
void voiceUpdate();  // call every loop()
bool voiceBusy();        // capturing, waiting or speaking: the face stays steady
bool voiceOwnsAudio();   // the mic or a reply holds the I2S bus: no tones (they would crash it)
String voiceStatusJson();  // for the monitor page
