"""Forward controller events (presence, escape, game record) to MQTT and a webhook.

Each event is published on ``<base>/event`` and, if a webhook URL is set, POSTed as
JSON from a background thread so a slow endpoint never stalls the control loop.
Events of the same type within ``cooldown_s`` are dropped.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger(__name__)

MESSAGES = {
    "presence": "StackChan: มีคนเข้ามาใกล้",
    "escape": "StackChan: สะดุ้งหลบสิ่งที่พุ่งเข้ามา",
    "record": "StackChan: สถิติใหม่ในเกมดึงความสนใจ",
}


class EventSink:
    def __init__(self, publish: Callable[[str, str], None] | None, base_topic: str = "stackchan",
                 webhook_url: str | None = None, cooldown_s: float = 30.0, robot: str | None = None):
        self.publish = publish
        self.topic = f"{base_topic.rstrip('/')}/event"
        self.webhook_url = webhook_url
        self.cooldown_s = cooldown_s
        self.robot = robot or base_topic
        self._last: dict[str, float] = {}

    def emit(self, event: dict, now: float | None = None, brain: dict | None = None) -> bool:
        """Send ``event`` unless its type is cooling down; return whether it was sent."""
        now = time.monotonic() if now is None else now
        kind = event.get("type", "event")
        if now - self._last.get(kind, -float("inf")) < self.cooldown_s:
            return False
        self._last[kind] = now
        payload = {
            **{k: v for k, v in event.items() if k != "time"},  # controller clock, meaningless outside
            "robot": self.robot,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "message": MESSAGES.get(kind, kind),
        }
        if brain:
            payload["brain"] = brain
        body = json.dumps(payload, ensure_ascii=False)
        if self.publish:
            self.publish(self.topic, body)
        if self.webhook_url:
            threading.Thread(target=self._post, args=(body,), daemon=True).start()
        log.info("event %s", body)
        return True

    def _post(self, body: str) -> None:
        req = urllib.request.Request(self.webhook_url, data=body.encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
        except Exception as e:  # network errors must not reach the control loop
            log.warning("webhook %s failed: %s", self.webhook_url, e)
