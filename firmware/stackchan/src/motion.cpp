#include "motion.h"

#include "params.h"
#include "servo_drv.h"

static constexpr uint32_t SERVO_SETTLE_MS = 800;  // a move plus its wobble
static constexpr uint32_t COOLDOWN_MS = 5000;     // one reaction per handling

static float g0[3] = {0, 0, 0};  // resting gravity direction (unit), learnt while still
static bool haveG0 = false;
static int liftRun = 0, tiltRun = 0;
static uint32_t shakeHits[4] = {0, 0, 0, 0};
static uint32_t lastEvent = 0;
static float lastDev = 0, lastAngle = 0, lastGyro = 0;

const char* motionUpdate(float ax, float ay, float az, float gx, float gy, float gz) {
  const uint32_t now = millis();
  const float mag = sqrtf(ax * ax + ay * ay + az * az);
  const float dev = fabsf(mag - 1.0f);
  const float gyro = sqrtf(gx * gx + gy * gy + gz * gz);
  lastDev = dev, lastGyro = gyro;
  if (mag < 0.2f) return nullptr;  // no reading

  // Shaking is violent enough to count even while the servos move.
  if (dev > P.motionShakeG) {
    memmove(shakeHits, shakeHits + 1, sizeof shakeHits - sizeof *shakeHits);
    shakeHits[3] = now;
    if (now - shakeHits[0] < 1500 && now - lastEvent > COOLDOWN_MS) {
      lastEvent = now;
      return "shake";
    }
  }

  const bool still = now - servoLastMoveMs() > SERVO_SETTLE_MS;
  if (!still) {  // the head itself is moving: the resting direction changes, learn it again
    haveG0 = false;
    liftRun = tiltRun = 0;
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

  tiltRun = angle > P.motionTiltDeg ? tiltRun + 1 : 0;
  liftRun = dev > P.motionLiftG || gyro > 60.0f ? liftRun + 1 : 0;
  if (now - lastEvent < COOLDOWN_MS) return nullptr;
  if (tiltRun >= 10) {  // ~0.3 s held over
    lastEvent = now;
    return "tilt";
  }
  if (liftRun >= 3) {  // ~0.1 s of a push or a turn the servos did not make
    lastEvent = now;
    return "lift";
  }
  return nullptr;
}

String motionStatusJson() {
  char buf[96];
  snprintf(buf, sizeof buf, "{\"dev_g\":%.2f,\"tilt_deg\":%.1f,\"gyro\":%.0f,\"rest\":%d}", lastDev, lastAngle,
           lastGyro, haveG0);
  return buf;
}
