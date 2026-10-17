#include "look.h"

#include <M5Unified.h>

using namespace m5avatar;

namespace {

struct Colors {
  uint16_t bg, face, cheek, mouth;
};

uint16_t rgb(uint8_t r, uint8_t g, uint8_t b) { return M5.Display.color565(r, g, b); }

Colors colorsFor(Expression e) {
  switch (e) {
    case Expression::Happy:
      return {rgb(46, 34, 82), rgb(255, 244, 228), rgb(255, 112, 150), rgb(255, 150, 170)};
    case Expression::Sleepy:  // dimmed, like a screen at night
      return {rgb(12, 16, 32), rgb(140, 152, 190), rgb(110, 70, 105), rgb(140, 152, 190)};
    case Expression::Doubt:
      return {rgb(18, 44, 64), rgb(255, 244, 228), rgb(255, 140, 160), rgb(255, 244, 228)};
    case Expression::Angry:
      return {rgb(70, 22, 30), rgb(255, 230, 210), rgb(255, 90, 90), rgb(255, 230, 210)};
    case Expression::Sad:
      return {rgb(20, 30, 60), rgb(200, 215, 245), rgb(160, 120, 170), rgb(200, 215, 245)};
    default:  // Neutral: the resting face of an assistant
      return {rgb(24, 32, 58), rgb(255, 244, 228), rgb(255, 128, 160), rgb(255, 244, 228)};
  }
}

Colors current;  // read by the draw task each frame
volatile bool holdWanted = false, parked = false;

class RoundEye final : public Drawable {
 public:
  RoundEye(int r, bool isLeft) : r(r), isLeft(isLeft) {}
  void draw(M5Canvas* spr, BoundingRect rect, DrawContext* ctx) override {
    const Expression e = ctx->getExpression();
    const Gaze g = isLeft ? ctx->getLeftGaze() : ctx->getRightGaze();
    const float open = isLeft ? ctx->getLeftEyeOpenRatio() : ctx->getRightEyeOpenRatio();
    const int x = rect.getCenterX() + g.getHorizontal() * 4, y = rect.getCenterY() + g.getVertical() * 4;
    if (open <= 0 || e == Expression::Sleepy) {  // closed: a soft line
      spr->fillRoundRect(x - r, y - 2, 2 * r, 5, 2, current.face);
      return;
    }
    if (e == Expression::Happy) {  // ^ ^
      spr->fillArc(x, y + r / 2, r, r - 6, 200, 340, current.face);
      return;
    }
    spr->fillCircle(x, y, r, current.face);
    spr->fillCircle(x + r / 3, y - r / 3, r / 4 + 1, current.bg);  // the highlight, a glint of background
    if (e == Expression::Angry || e == Expression::Sad) {  // a lid slanting in or out
      const int x0 = x - r - 1, x1 = x + r + 1, y0 = y - r - 1;
      const int x2 = (isLeft != (e == Expression::Sad)) ? x0 : x1;
      spr->fillTriangle(x0, y0, x1, y0, x2, y0 + r, current.bg);
    }
  }

 private:
  int r;
  bool isLeft;
};

class SoftMouth final : public Drawable {
 public:
  void draw(M5Canvas* spr, BoundingRect rect, DrawContext* ctx) override {
    // the first part drawn each frame, into the canvas only: a safe place for the draw task to wait
    if (holdWanted) {
      parked = true;
      while (holdWanted) vTaskDelay(pdMS_TO_TICKS(5));
      parked = false;
    }
    const float open = ctx->getMouthOpenRatio(), breath = std::min(1.0f, ctx->getBreath());
    const int x = rect.getLeft(), y = rect.getTop() + breath * 2;
    // blush below and outside the eyes, under the mouth so it never covers it
    spr->fillEllipse(x - 88, y - 20, 20, 10, current.cheek);
    spr->fillEllipse(x + 88, y - 20, 20, 10, current.cheek);
    if (open < 0.08f && ctx->getExpression() == Expression::Happy) {  // a closed smile
      spr->fillArc(x, y - 16, 26, 20, 30, 150, current.mouth);
      return;
    }
    const int w = 56 - 22 * open, h = 6 + 34 * open;
    spr->fillRoundRect(x - w / 2, y - h / 2, w, h, std::min(h / 2, 12), current.mouth);
  }
};

ColorPalette paletteFor(const Colors& c) {
  ColorPalette cp;
  cp.set(COLOR_PRIMARY, c.face);
  cp.set(COLOR_SECONDARY, c.bg);
  cp.set(COLOR_BACKGROUND, c.bg);
  cp.set(COLOR_BALLOON_FOREGROUND, rgb(24, 32, 58));
  cp.set(COLOR_BALLOON_BACKGROUND, rgb(255, 244, 228));
  return cp;
}

}  // namespace

void lookBegin(Avatar& avatar) {
  auto* canvas = new M5Canvas(&M5.Display);
  canvas->setPsram(true);
  // the library's layout (Face()), with our parts and canvas
  auto* face = new Face(new SoftMouth(), new BoundingRect(148, 163), new RoundEye(14, false),
                        new BoundingRect(93, 90), new RoundEye(14, true), new BoundingRect(96, 230),
                        new Eyeblow(32, 0, false), new BoundingRect(67, 96), new Eyeblow(32, 0, true),
                        new BoundingRect(72, 230), new BoundingRect(0, 0, 320, 240), canvas,
                        new M5Canvas(&M5.Display));
  avatar.setFace(face);
  current = colorsFor(Expression::Neutral);
  avatar.setColorPalette(paletteFor(current));
  avatar.init(16);
}

bool lookHold(uint32_t timeoutMs) {
  holdWanted = true;
  for (uint32_t t0 = millis(); !parked && millis() - t0 < timeoutMs;) delay(5);
  return parked;
}

void lookRelease() { holdWanted = false; }

void lookExpression(Avatar& avatar, Expression e) {
  current = colorsFor(e);
  avatar.setColorPalette(paletteFor(current));
  avatar.setExpression(e);
}
