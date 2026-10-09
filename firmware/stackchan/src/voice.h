// Voice turn with the bridge (VOICE_URL in flybot_config.h): hold the screen to talk, the
// recording goes to the bridge's /api/voice (Thai ASR -> LLM -> Thai TTS) and the WAV reply
// plays on the speaker.
#pragma once

void voiceBegin();
void voiceUpdate();  // call every loop()
bool voiceBusy();    // listening, waiting or speaking: the brain's tones stay quiet
