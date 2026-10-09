// Copy to flybot_config.h (gitignored) and edit.
#pragma once

#define WIFI_SSID "your-ssid"
#define WIFI_PASSWORD "your-password"

#define MQTT_HOST "192.168.1.10"
#define MQTT_PORT 1883
#define MQTT_USER ""  // empty = anonymous
#define MQTT_PASSWORD ""
#define MQTT_BASE_TOPIC "stackchan"

// SG90 signal pins. CoreS3 Port.A X:2 Y:1, Port.B X:9 Y:8, Port.C X:17 Y:18
#define SERVO_PIN_X 17
#define SERVO_PIN_Y 18
// servo degrees = center + sign * command angle, then clamped to the limits.
// Flip a sign if the head turns the wrong way (pan_angle + = right, tilt_angle + = up).
#define SERVO_X_CENTER 90
#define SERVO_X_SIGN (-1)
#define SERVO_X_MIN 0
#define SERVO_X_MAX 180
#define SERVO_Y_CENTER 90
#define SERVO_Y_SIGN (-1)
#define SERVO_Y_MIN 60  // the SG90 StackChan tilt mechanism only travels ~60..90
#define SERVO_Y_MAX 90

// M5Stack StackChan (pio run -e stackchan-official): SCS0009 bus servos, angles in degrees
// from the factory zero (read from NVS if the factory app calibrated it).
#define SCS_YAW_SIGN 1
#define SCS_PITCH_SIGN 1
#define SCS_YAW_LIMIT_DEG 90  // factory firmware allows 128
#define SCS_PITCH_MIN_DEG 5   // M5Stack: keep the vertical axis within 5..85 or it may stall
#define SCS_PITCH_MAX_DEG 85

// Motion detector (frame differencing on 160x120 grayscale)
#define DIFF_THRESHOLD 25         // per-pixel |change| counted as motion
#define MIN_MOTION_PIXELS 200     // fewer changed pixels -> "detected": false (real camera: ~35 px of noise at rest)
#define MAX_MOTION_FRACTION 0.30f // more -> whole image moved (head turning / exposure), frame skipped
#define EGO_MOTION_DEG_S 10        // frames are skipped while the head turns faster (real scenes change whole-frame above ~10 deg/s)

// 1 = always run the full boot self-test (servo sweep); otherwise touch the screen at boot
#define SELF_TEST_FULL 0

// % of the volume the bridge asks for (0 = mute)
#define SPEAKER_VOLUME_PCT 100

// Voice: hold the screen to talk to the bridge (needs its dashboard with --stt-url, --tts-url, --llm).
// Basic auth defaults to MQTT_USER / MQTT_PASSWORD, like the k8s manifests.
// #define VOICE_URL "http://192.168.1.10:31880/api/voice"
// #define VOICE_VOLUME 140  // 0..255
// Hands-free: speech louder than VAD_MIN_RMS and VAD_RATIO x the room's noise is sent with
// ?wake=1, and the bridge answers only if it calls the robot by name (--wake-name).
// #define VAD_MIN_RMS 400
// #define VAD_RATIO 3.0f
// #define VOICE_GAIN 1.0f  // software gain on replies, past VOICE_VOLUME 255

// Camera monitor: http://<robot ip>/ shows what the motion detector sees (MJPEG, 5 fps)
// #define STREAM_PORT 80

#define IMU_PERIOD_MS 33
#define PROXIMITY_PERIOD_MS 100
