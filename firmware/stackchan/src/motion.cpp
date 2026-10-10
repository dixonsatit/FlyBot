#include "motion.h"

#include "params.h"
#include "servo_drv.h"

static constexpr uint32_t SERVO_SETTLE_MS = 800;  // a move plus its wobble
static constexpr uint32_t COOLDOWN_MS = 5000;     // one reaction per handling

static float g0[3] = {0, 0, 0};  // resting gravity direction (unit), learnt while still
static bool haveG0 = false;
static uint32_t windowStart = 0;
static int samples = 0, tiltSamples = 0, jolts = 0;
static constexpr uint32_t WINDOW_MS = 600;
static char lastDecision[64] = "";  // why the last event was named what it was
static uint32_t shakeHits[4] = {0, 0, 0, 0};
static uint32_t lastEvent = 0;
static float peakDev = 0, lastDev = 0, lastAngle = 0, lastGyro = 0;

const char* motionUpdate(float ax, float ay, float az, float gx, float gy, float gz) {
  const uint32_t now = millis();
  const float mag = sqrtf(ax * ax + ay * ay + az * az);
  const float dev = fabsf(mag - 1.0f);
  const float gyro = sqrtf(gx * gx + gy * gy + gz * gz);
  lastDev = dev, lastGyro = gyro;
  if (mag < 0.2f) return nullptr;  // no reading

  // Three hard jolts count as shaking even while the servos move.
  if (dev > 3 * P.motionShakeG) {
    memmove(shakeHits, shakeHits + 1, sizeof shakeHits - sizeof *shakeHits);
    shakeHits[3] = now;
    if (now - shakeHits[1] < 1500 && now - lastEvent > COOLDOWN_MS) {  // three jolts
      lastEvent = now;
      snprintf(lastDecision, sizeof lastDecision, "shake: 3 jolts > %.2f g in %lu ms", P.motionShakeG,
               (unsigned long)(now - shakeHits[1]));
      windowStart = 0;
      return "shake";
    }
  }
  peakDev = max(peakDev * 0.98f, dev);

  const bool still = now - servoLastMoveMs() > SERVO_SETTLE_MS;
  if (!still) {  // the head itself is moving: the resting direction changes, learn it again
    haveG0 = false;
    windowStart = 0;
    return nullptr;
  }
  const float u[3] = {ax / mag, ay / mag, az / mag};
  if (!haveG0) {
    memcpy(g0, u, sizeof g0);
    haveG0 = true;
    return nullptr;
  }
  const float dot = constrain(u[0] * g0[0] + u[1] * g0[1] + u[2] * g0[2], -1.0f, 1.0f);
  const float angle = acosf(dot) * 180.0f / PI;
  lastAngle = angle;
  if (dev < 0.05f && gyro < 5.0f && angle < 3.0f) {  // at rest: follow slow drift
    for (int i = 0; i < 3; ++i) g0[i] += 0.02f * (u[i] - g0[i]);
    const float n = sqrtf(g0[0] * g0[0] + g0[1] * g0[1] + g0[2] * g0[2]);
    for (int i = 0; i < 3; ++i) g0[i] /= n;
  }

  // Something is happening: watch it for WINDOW_MS, then name it. Tilting and shaking both
  // start with a turn or a jolt, so deciding on the first sample called everything a lift.
  const bool trigger = angle > P.motionTiltDeg || dev > P.motionLiftG || gyro > 60.0f;
  if (!windowStart) {
    if (!trigger || now - lastEvent < COOLDOWN_MS) return nullptr;
    windowStart = now, samples = tiltSamples = jolts = 0;
  }
  ++samples;
  if (angle > P.motionTiltDeg) ++tiltSamples;
  if (dev > P.motionShakeG) ++jolts;
  if (now - windowStart < WINDOW_MS) return nullptr;
  windowStart = 0;
  lastEvent = now;
  const bool tilted = angle > P.motionTiltDeg && tiltSamples * 2 >= samples;  // still over, most of the time
  const char* kind = jolts >= 5 ? "shake" : tilted ? "tilt" : "lift";  // a shake gave 0.26 g peaks, a lift one 0.17
  snprintf(lastDecision, sizeof lastDecision, "%s: tilt %d/%d now %.0f deg, jolts %d, peak %.2f g", kind, tiltSamples,
           samples, angle, jolts, peakDev);
  return kind;
}

String motionStatusJson() {
  char buf[192];
  snprintf(buf, sizeof buf, "{\"last\":\"%s\",\"dev_g\":%.2f,\"peak_g\":%.2f,\"tilt_deg\":%.1f,\"gyro\":%.0f,\"rest\":%d}", lastDecision, lastDev,
           peakDev, lastAngle, lastGyro, haveG0);
  return buf;
}
