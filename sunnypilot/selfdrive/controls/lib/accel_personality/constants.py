"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""

from cereal import custom

# Profile ids come from cereal: eco @0, normal @1, sport @2 (single source of truth).
AccelerationPersonality = custom.LongitudinalPlanSP.AccelerationPersonality
ECO = AccelerationPersonality.eco
NORMAL = AccelerationPersonality.normal
SPORT = AccelerationPersonality.sport

PERSONALITY_MIN = min(AccelerationPersonality.schema.enumerants.values())
PERSONALITY_MAX = max(AccelerationPersonality.schema.enumerants.values())

# --- Positive acceleration ceiling (feeds the planner accel_clip upper bound) ---
A_CRUISE_MAX_BP = [0., 10., 25., 40.]

# Stock openpilot acceleration ceiling. Normal and disabled mode intentionally match this path.
STOCK_A_CRUISE_MAX_V = [1.6, 1.2, 0.8, 0.6]
STOCK_RISE_RATE = 0.05

# Speed-indexed accel ceiling. NORMAL is LOCKED to stock so a disabled controller (forced to NORMAL)
# is byte-identical to stock. Sport stays modestly above stock (responsive, not aggressive); eco gentle.
A_CRUISE_MAX_V = {
  ECO:    [1.20, 0.85, 0.45, 0.30],
  NORMAL: STOCK_A_CRUISE_MAX_V,
  SPORT:  [1.75, 1.30, 0.90, 0.65],
}

# Upward slew of the accel ceiling, m/s^2 per planner cycle (DT_MDL). NORMAL locked to stock.
# Sport only slightly quicker than stock (smooth roll-on, not a launch).
RISE_RATE = {
  ECO:    0.02,
  NORMAL: STOCK_RISE_RATE,
  SPORT:  0.06,
}

# --- Early soft braking ---
# Predicted brake need (m/s^2, positive) -> early comfort decel target (m/s^2, negative).
# Gentle, human-like progression: lead the brake early and softly rather than late and hard.
SMOOTH_DECEL_BP = [0.0, 0.4, 0.8, 1.2, 1.6, 2.0, 2.4]
SMOOTH_DECEL_V = {
  ECO:    [0.00, -0.08, -0.20, -0.38, -0.60, -0.82, -1.05],
  NORMAL: [0.00, -0.13, -0.30, -0.55, -0.84, -1.12, -1.40],
  SPORT:  [0.00, -0.17, -0.40, -0.72, -1.05, -1.35, -1.65],
}

# Jerk limits (m/s^3) - all kept gentle for smoothness ("no jerk" goal).
# Deepening only shapes the EARLY front-loaded brake; the never-weaken clamp lets a real plan brake
# through immediately, so a soft deepening rate never delays genuine braking.
BRAKE_DEEPENING_JERK = {
  ECO:    0.5,
  NORMAL: 0.8,
  SPORT:  1.0,
}
BRAKE_RELEASE_JERK = 2.0   # how fast the brake lets off (kept brisk so resume/SnG isn't laggy)

# Positive-accel onset jerk (m/s^3). This is the "smooth, not crazy fast" knob: stock has no output
# accel-jerk limit, so enabling the controller makes accel onset gentler than stock on every tier.
ACCEL_RISE_JERK = {
  ECO:    0.7,
  NORMAL: 1.2,
  SPORT:  1.6,
}

# Look this far into the planned decel trajectory to anticipate braking and start early.
SMOOTH_DECEL_LOOKAHEAD_T = 3.0
# Below this predicted decel we treat the situation as "no braking coming".
MIN_SMOOTH_BRAKE_NEED = 0.05

# Hand the target fully back to the stock plan (never shape) once braking is genuinely hard.
HARD_BRAKE_TARGET_ACCEL = -2.0
HARD_BRAKE_NEED = 2.6

# Closing-lead bypass: hand fully back to the plan on a real closing threat regardless of the fixed
# accel thresholds above (mirrors the route 000003da lesson - shaping must yield to closing dynamics).
CLOSING_LEAD_VREL = -8.0   # m/s, lead approaching faster than this
CLOSING_LEAD_TTC = 4.0     # s, time-to-collision below this

# --- early-soft-braking consistency (anti rubber-band, route 00000423 finding) ---
# Hysteresis on engagement so the anticipatory brake doesn't toggle on transient predicted-decel dips.
SMOOTH_ENTER = 0.40   # brake_need to ENGAGE early-soft braking
SMOOTH_EXIT = 0.15    # brake_need to DISENGAGE once engaged
# A present lead pulling away makes the model's predicted decel spurious in a following context;
# suppress the anticipatory brake then (the plan's real brake still passes via never-weaken).
EARLY_BRAKE_PULLAWAY_VREL = 0.5   # m/s, lead opening faster than this -> no anticipatory brake
# Taper the anticipatory brake out at low speed: in stop-and-go it only adds rubber-band and the stops
# are owned by stop-hold / should_stop anyway (route 00000423: the flips lived < 7 m/s). The plan's real
# brake still passes via never-weaken, so no braking is lost - only the spurious early shaping.
EARLY_BRAKE_SPEED_BP = [6.0, 9.0]   # m/s
EARLY_BRAKE_SPEED_V = [0.0, 1.0]    # anticipatory-brake gain

# --- stop-hold / anti-creep (no double-stop / roll, route 00000423 finding) ---
STOP_HOLD_EGO_V = 0.5             # ego considered stopped below this (m/s)
STOP_HOLD_LEAD_V = 1.0            # lead considered stopped below this (m/s)
STOP_HOLD_RELEASE_LEAD_V = 1.5    # lead clearly departing -> release latch (m/s); above a 1m twitch
STOP_HOLD_RELEASE_DREL = 2.0      # ... or the gap opened this much from the stop (m)
STOP_HOLD_ACCEL = -0.3            # gentle hold while latched (no creep); never weakens a deeper plan brake
