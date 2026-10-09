#include "servo_drv.h"

#include <M5Unified.h>

#if __has_include("flybot_config.h")
#include "flybot_config.h"
#else
#include "flybot_config.example.h"
#endif

#if STACKCHAN_OFFICIAL
// Values from M5Stack's factory firmware (github.com/m5stack/StackChan,
// firmware/main/hal/hal_servo.cpp and hal_io_expander.cpp).
#include <Preferences.h>
#include <SCServo.h>

#ifndef SCS_YAW_SIGN
#define SCS_YAW_SIGN 1
#endif
#ifndef SCS_PITCH_SIGN
#define SCS_PITCH_SIGN 1
#endif
#ifndef SCS_YAW_LIMIT_DEG  // factory limit is 128
#define SCS_YAW_LIMIT_DEG 90
#endif
#ifndef SCS_PITCH_MIN_DEG  // M5Stack: keep the vertical axis within 5..85 or it may stall
#define SCS_PITCH_MIN_DEG 5
#endif
#ifndef SCS_PITCH_MAX_DEG
#define SCS_PITCH_MAX_DEG 85
#endif
#ifndef SCS_IDLE_RELEASE_MS  // the factory firmware releases ~0.2 s after the move ends
#define SCS_IDLE_RELEASE_MS 1500
#endif
#ifndef SCS_MAX_SPEED_DEG_S  // = ControllerConfig.max_rate_deg_s
#define SCS_MAX_SPEED_DEG_S 200
#endif

static constexpr uint8_t YAW_ID = 1, PITCH_ID = 2;
#ifndef SCS_YAW_ZERO  // raw position for pan 0 / tilt 0 when NVS has no calibration
#define SCS_YAW_ZERO 460
#endif
#ifndef SCS_PITCH_ZERO
#define SCS_PITCH_ZERO 620
#endif
static constexpr int YAW_ZERO_DEFAULT = SCS_YAW_ZERO, PITCH_ZERO_DEFAULT = SCS_PITCH_ZERO;
static constexpr int RAW_MIN = 0, RAW_MAX = 1000;
static constexpr float RAW_PER_DEG = 3.2f;  // one step = 0.3125 deg
static constexpr int SERVO_TX = 6, SERVO_RX = 7;
static constexpr uint32_t SERVO_BAUD = 1000000;

// PY32 IO expander on the internal I2C bus; its pin 0 switches the servo supply (VM EN).
static constexpr uint8_t PY32_ADDR = 0x6F;
static constexpr uint8_t PY32_VERSION = 0x02, PY32_DIR_L = 0x03, PY32_OUT_L = 0x05, PY32_PU_L = 0x09,
                         PY32_PD_L = 0x0B;
static constexpr uint32_t I2C_FREQ = 100000;  // as M5Stack's PY32 drivers; it misses reads at 400 kHz

static SCSCL scs;
static bool vmOk = false;
static char info[96];

struct Axis {
  uint8_t id;
  int zero;
  bool stallProtection;
  int last = -1;  // last goal (raw), -1 = unknown
  int rawMin = RAW_MIN, rawMax = RAW_MAX;  // narrowed where a stall was detected
  uint32_t lastMoveMs = 0;
  bool torque = false;
  // stall detector state (M5Stack hal_servo.cpp update_stall_protection)
  uint32_t lastCheckMs = 0;
  int lastPos = 0, lastCurrent = 0, lastLoad = 0, lastDir = 0, confirm = 0;
  bool feedbackValid = false;
  Axis(uint8_t id, int zero, bool stallProtection) : id(id), zero(zero), stallProtection(stallProtection) {}
};
static Axis yawAxis(YAW_ID, YAW_ZERO_DEFAULT, false);
static Axis pitchAxis(PITCH_ID, PITCH_ZERO_DEFAULT, true);

static bool enableServoPower() {
  // the PY32 boots slowly
  for (uint32_t t0 = millis(); millis() - t0 < 1200; delay(200)) {
    uint8_t v = M5.In_I2C.readRegister8(PY32_ADDR, PY32_VERSION, I2C_FREQ);
    if (v != 0 && v != 0xFF) {
      M5.In_I2C.bitOn(PY32_ADDR, PY32_DIR_L, 0x01, I2C_FREQ);   // output
      M5.In_I2C.bitOff(PY32_ADDR, PY32_PD_L, 0x01, I2C_FREQ);   // pull-up
      M5.In_I2C.bitOn(PY32_ADDR, PY32_PU_L, 0x01, I2C_FREQ);
      M5.In_I2C.bitOn(PY32_ADDR, PY32_OUT_L, 0x01, I2C_FREQ);   // VM on
      delay(200);
      return true;
    }
  }
  return false;
}

// Zero positions calibrated in the factory app survive only while NVS does.
static int zeroFromNvs(Preferences& p, const char* key, int fallback) {
  int v = p.getInt(key, -1);
  return v >= RAW_MIN && v <= RAW_MAX ? v : fallback;
}

static void resetStall(Axis& a) {
  a.feedbackValid = false;
  a.lastDir = 0;
  a.confirm = 0;
}

// Port of M5Stack's stall protection: when the servo is pushing (current or load rising)
// but not moving, stop at the current position and never command past it again.
static bool stalled(Axis& a, int target) {
  if (!a.stallProtection || millis() - a.lastCheckMs < 50) return false;
  a.lastCheckMs = millis();
  if (scs.FeedBack(a.id) < 0) {
    resetStall(a);
    return false;
  }
  const int pos = scs.ReadPos(-1), current = abs(scs.ReadCurrent(-1)), load = abs(scs.ReadLoad(-1));
  if (pos < RAW_MIN || pos > RAW_MAX || abs(target - pos) < 8) {
    resetStall(a);
    return false;
  }
  const int dir = target > pos ? 1 : -1;
  if (a.feedbackValid && dir == a.lastDir) {
    const int moved = abs(pos - a.lastPos);
    const bool spike = current >= 350 || current - a.lastCurrent >= 80 || load >= 650 || load - a.lastLoad >= 150;
    if (moved <= 1 && spike) {
      ++a.confirm;
    } else if (moved > 1) {
      a.confirm = 0;
    }
  } else {
    a.confirm = 0;
  }
  a.lastPos = pos;
  a.lastCurrent = current;
  a.lastLoad = load;
  a.lastDir = dir;
  a.feedbackValid = true;
  if (a.confirm < 2) return false;

  if (dir > 0) a.rawMax = min(a.rawMax, pos);
  else a.rawMin = max(a.rawMin, pos);
  scs.WritePos(a.id, pos, 20, 0);
  a.last = pos;
  resetStall(a);
  M5_LOGW("servo %d stall at raw %d (current %d load %d), limit now %d..%d", a.id, pos, current, load, a.rawMin,
          a.rawMax);
  return true;
}

// The servo's own move time paces the step, so a big jump (boot, self-test) is not taken
// at full speed and the 20 Hz command stream from the bridge moves smoothly.
static void moveTo(Axis& a, float deg) {
  const int raw = constrain(a.zero + int(lroundf(deg * RAW_PER_DEG)), a.rawMin, a.rawMax);
  if (stalled(a, raw)) return;
  if (a.last >= 0 && abs(raw - a.last) < 2) return;  // < 0.6 deg: let the head rest
  const int span = a.last < 0 ? int(RAW_MAX * 0.3f) : abs(raw - a.last);
  const uint16_t ms = max(20, int(span / RAW_PER_DEG / SCS_MAX_SPEED_DEG_S * 1000));
  scs.WritePos(a.id, raw, ms, 0);  // also turns the torque back on
  a.last = raw;
  a.lastMoveMs = millis();
  a.torque = true;
}

// Like the factory firmware, release the torque once the head has rested, so the servos
// do not hold (and heat) all the time.
static void releaseIdle(Axis& a) {
  if (a.torque && millis() - a.lastMoveMs > SCS_IDLE_RELEASE_MS) {
    scs.EnableTorque(a.id, 0);
    a.torque = false;
  }
}

bool servoBegin() {
  vmOk = enableServoPower();
  if (!vmOk) {  // which of the base's chips answer: none = the base has no power or no contact
    char found[160] = "";
    for (uint8_t addr = 0x08; addr < 0x78; ++addr) {
      if (M5.In_I2C.scanID(addr, I2C_FREQ)) snprintf(found + strlen(found), sizeof found - strlen(found), " 0x%02X", addr);
    }
    M5_LOGW("servo: PY32 not found; board %d; base I2C devices:%s", int(M5.getBoard()), found[0] ? found : " none");
  }
  Preferences p;
  if (p.begin("servo", true)) {
    yawAxis.zero = zeroFromNvs(p, "zero_pos_1", YAW_ZERO_DEFAULT);
    pitchAxis.zero = zeroFromNvs(p, "zero_pos_2", PITCH_ZERO_DEFAULT);
    p.end();
  }
  Serial1.begin(SERVO_BAUD, SERIAL_8N1, SERVO_RX, SERVO_TX);
  scs.pSerial = &Serial1;
  bool yawOk = scs.Ping(YAW_ID) == YAW_ID;
  bool pitchOk = scs.Ping(PITCH_ID) == PITCH_ID;
  for (Axis* a : {&yawAxis, &pitchAxis}) {
    int pos = scs.ReadPos(a->id);
    a->last = pos >= RAW_MIN && pos <= RAW_MAX ? pos : -1;
  }
  snprintf(info, sizeof info, "SCS vm=%d yaw=%d pitch=%d zero=%d/%d", vmOk, yawOk, pitchOk, yawAxis.zero,
           pitchAxis.zero);
  return vmOk && yawOk && pitchOk;
}

void servoWrite(float pan, float tilt) {
  moveTo(yawAxis, constrain(SCS_YAW_SIGN * pan, -SCS_YAW_LIMIT_DEG, SCS_YAW_LIMIT_DEG));
  moveTo(pitchAxis, constrain(SCS_PITCH_SIGN * tilt, SCS_PITCH_MIN_DEG, SCS_PITCH_MAX_DEG));
}

void servoUpdate() {
  releaseIdle(yawAxis);
  releaseIdle(pitchAxis);
}

#else
#include <ESP32Servo.h>

static Servo servoX, servoY;

bool servoBegin() {
  servoX.setPeriodHertz(50);
  servoY.setPeriodHertz(50);
  servoX.attach(SERVO_PIN_X, 500, 2400);
  servoY.attach(SERVO_PIN_Y, 500, 2400);
  return true;
}

void servoWrite(float pan, float tilt) {
  servoX.write(constrain(SERVO_X_CENTER + SERVO_X_SIGN * pan, SERVO_X_MIN, SERVO_X_MAX));
  servoY.write(constrain(SERVO_Y_CENTER + SERVO_Y_SIGN * tilt, SERVO_Y_MIN, SERVO_Y_MAX));
}

void servoUpdate() {}

static char info[] = "SG90 PWM";
#endif

const char* servoInfo() { return info; }
