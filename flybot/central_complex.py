"""Central complex: E-PG heading ring, FC2 goal ring and PFL3 steering.

Body yaw from the IMU moves an activity bump around a ring of E-PG wedges
(ellipsoid body). The decoded change in heading drives a vestibulo-ocular
style counter-rotation of the pan servo, and a PFL3-like comparison between
the remembered gaze goal (FC2) and the current gaze returns the head to the
spot it was looking at.

Angles here are degrees, positive = clockwise seen from above (to the right),
the same convention as ``pan_angle``.
"""
from __future__ import annotations

import numpy as np


def _wrap(deg: float) -> float:
    return (deg + 180.0) % 360.0 - 180.0


class CentralComplex:
    def __init__(self, n_wedges: int = 16, kappa: float = 4.0, pfl3_offset_deg: float = 45.0):
        self.theta = np.linspace(0.0, 2 * np.pi, n_wedges, endpoint=False)
        self.kappa = kappa
        self.pfl3_shift = np.deg2rad(pfl3_offset_deg)
        self.epg = self._bump(0.0)
        self.fc2: np.ndarray | None = None
        self._heading = 0.0

    def _bump(self, deg: float, shift: float = 0.0) -> np.ndarray:
        act = np.exp(self.kappa * (np.cos(self.theta - np.deg2rad(deg) - shift) - 1.0))
        return act / act.sum()

    def _decode(self, act: np.ndarray) -> float:
        return float(np.rad2deg(np.angle(np.sum(act * np.exp(1j * self.theta)))))

    @property
    def heading(self) -> float:
        """Body heading decoded from the E-PG bump (deg)."""
        return self._decode(self.epg)

    def update_heading(self, yaw_rate_deg_s: float | None, dt: float, yaw_deg: float | None = None) -> float:
        """Shift the bump by IMU yaw (P-EN input) or anchor it to an absolute yaw.

        Returns the decoded heading change in degrees.
        """
        before = self.heading
        if yaw_deg is not None:
            self._heading = yaw_deg
        elif yaw_rate_deg_s is not None:
            self._heading += yaw_rate_deg_s * dt
        self.epg = self._bump(self._heading)
        return _wrap(self.heading - before)

    def set_goal(self, gaze_world_deg: float) -> None:
        """Store the direction of interest (world frame) in the FC2 ring."""
        self.fc2 = self._bump(gaze_world_deg)

    def steering(self, pan_deg: float) -> float:
        """PFL3 output in [-1, 1] ~ sin(goal - gaze); positive = turn right."""
        if self.fc2 is None:
            return 0.0
        gaze = self.heading + pan_deg
        # each PFL3 hemisphere reads the gaze bump shifted by ±offset against the goal ring
        right = float(np.dot(self._bump(gaze, +self.pfl3_shift), self.fc2))
        left = float(np.dot(self._bump(gaze, -self.pfl3_shift), self.fc2))
        norm = float(np.dot(self.fc2, self.fc2))
        return float(np.clip((right - left) / max(norm, 1e-9), -1.0, 1.0))
