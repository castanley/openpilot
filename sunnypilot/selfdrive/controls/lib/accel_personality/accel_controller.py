"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

Acceleration Personality controller (Eco / Normal / Sport).

Three independent, per-tier levers keyed by the cereal AccelerationPersonality ordinal:
  1. Accel ceiling  - get_max_accel(v_ego), feeds the planner accel_clip upper bound.
  2. Accel rise rate - get_rise_rate(), slews the accel ceiling upward.
  3. Early soft braking - smooth_target_accel(), front-loads a gentle decel BEFORE the plan brakes,
     never commanding less braking than the plan (never-weaken invariant), with hard-brake / FCW /
     should_stop / closing-lead / e2e bypass back to the stock plan. Engagement is hysteretic and
     suppressed when a lead is pulling away (anti rubber-band), and a stop-hold latch prevents the
     stop -> creep -> stop "double stop" on small lead twitches.

Disabled or Normal == stock by construction: Normal tier uses the stock ceiling/rise literals, and a
disabled controller forces Normal, passes the target through untouched, and never latches stop-hold.
"""

from collections.abc import Sequence

import numpy as np

from cereal import messaging
from opendbc.car import structs
from openpilot.common.params import Params
from openpilot.common.realtime import DT_MDL
from openpilot.sunnypilot import get_sanitize_int_param
from openpilot.sunnypilot.selfdrive.controls.lib.accel_personality.constants import \
  NORMAL, PERSONALITY_MIN, PERSONALITY_MAX, A_CRUISE_MAX_BP, A_CRUISE_MAX_V, RISE_RATE, SMOOTH_DECEL_BP, \
  SMOOTH_DECEL_V, BRAKE_DEEPENING_JERK, BRAKE_RELEASE_JERK, ACCEL_RISE_JERK, SMOOTH_DECEL_LOOKAHEAD_T, \
  MIN_SMOOTH_BRAKE_NEED, HARD_BRAKE_TARGET_ACCEL, HARD_BRAKE_NEED, CLOSING_LEAD_VREL, CLOSING_LEAD_TTC, \
  SMOOTH_ENTER, SMOOTH_EXIT, EARLY_BRAKE_PULLAWAY_VREL, EARLY_BRAKE_SPEED_BP, EARLY_BRAKE_SPEED_V, \
  STOP_HOLD_EGO_V, STOP_HOLD_LEAD_V, STOP_HOLD_RELEASE_LEAD_V, STOP_HOLD_RELEASE_DREL, STOP_HOLD_ACCEL

_ZERO_ACCEL_EPS = 1e-6


class AccelController:
  def __init__(self, CP: structs.CarParams, mpc, params=None):
    self._CP = CP
    self._mpc = mpc
    self._params = params or Params()
    self._frame = 0
    self._enabled: bool = self._params.get_bool("AccelPersonalityEnabled")
    self._personality = NORMAL  # cereal AccelerationPersonality ordinal
    self._v_ego = 0.0
    self._lead_status = False
    self._v_rel = 0.0
    self._v_lead = 0.0
    self._d_rel = 999.0
    self._lead_closing = False
    self._last_target_accel = 0.0
    self._brake_need = 0.0
    self._decel_target = 0.0
    self._smooth_active = False
    self._smooth_latched = False
    self._bypassed = False
    self._stop_held = False
    self._stop_d_rel = 0.0
    self._read_params()

  def _read_params(self) -> None:
    self._enabled = self._params.get_bool("AccelPersonalityEnabled")
    if not self._enabled:
      self._personality = NORMAL
      return

    self._personality = get_sanitize_int_param("AccelPersonality", PERSONALITY_MIN, PERSONALITY_MAX, self._params)

  def update(self, sm: messaging.SubMaster) -> None:
    if self._frame % int(1. / DT_MDL) == 0:
      self._read_params()
    self._v_ego = sm['carState'].vEgo
    lead = sm['radarState'].leadOne
    self._lead_status = bool(lead.status)
    self._v_rel = float(lead.vRel) if lead.status else 0.0
    self._v_lead = float(lead.vLead) if lead.status else 0.0
    self._d_rel = float(lead.dRel) if lead.status else 999.0
    self._lead_closing = self._compute_lead_closing()
    self._update_stop_hold()
    self._frame += 1

  def _compute_lead_closing(self) -> bool:
    if not self._lead_status or self._v_rel >= 0.0:
      return False
    ttc = self._d_rel / max(-self._v_rel, 1e-3)
    return self._v_rel <= CLOSING_LEAD_VREL or ttc <= CLOSING_LEAD_TTC

  def _update_stop_hold(self) -> None:
    # Latch a stop behind a near-stopped lead; release only on a real departure (not a 1m twitch).
    if not self._enabled:
      self._stop_held = False
      return

    if not self._stop_held:
      if self._v_ego < STOP_HOLD_EGO_V and self._lead_status and self._v_lead < STOP_HOLD_LEAD_V:
        self._stop_held = True
        self._stop_d_rel = self._d_rel
    else:
      departed = (self._v_lead > STOP_HOLD_RELEASE_LEAD_V) or (self._d_rel - self._stop_d_rel > STOP_HOLD_RELEASE_DREL)
      if departed or self._v_ego > 1.5 or not self._lead_status:
        self._stop_held = False

  # --- positive accel levers ---

  def get_max_accel(self, v_ego: float) -> float:
    return float(np.interp(v_ego, A_CRUISE_MAX_BP, A_CRUISE_MAX_V[self._personality]))

  def get_rise_rate(self) -> float:
    return RISE_RATE[self._personality]

  # --- early soft braking ---

  def get_decel_target(self, brake_need: float) -> float:
    return float(np.interp(max(0.0, float(brake_need)), SMOOTH_DECEL_BP, SMOOTH_DECEL_V[self._personality]))

  def smooth_target_accel(self, raw_target_accel: float, accel_trajectory: Sequence[float], t_idxs: Sequence[float],
                          should_stop: bool, reset: bool = False, stock_brake: bool = False) -> float:
    raw_target_accel = float(raw_target_accel)
    self._brake_need = self._compute_brake_need(raw_target_accel, accel_trajectory, t_idxs)
    self._decel_target = 0.0
    out = self._smooth_core(raw_target_accel, should_stop, reset, stock_brake)
    return self._apply_stop_hold(out)

  def _smooth_core(self, raw_target_accel: float, should_stop: bool, reset: bool, stock_brake: bool) -> float:
    if reset or not self._enabled:
      self._bypassed = False
      self._smooth_latched = False
      return self._passthrough(raw_target_accel)

    # e2e/blended path: never reshape braking (the planner already min-blends e2e/mpc, and vision stops
    # are the model's job per the lead->ACC policy).
    if stock_brake and (raw_target_accel < 0.0 or self._brake_need >= MIN_SMOOTH_BRAKE_NEED):
      self._bypassed = False
      self._smooth_latched = False
      return self._passthrough(raw_target_accel)

    self._bypassed = self._emergency_bypass(raw_target_accel, should_stop)
    if self._bypassed:
      self._smooth_latched = False
      return self._passthrough(raw_target_accel)

    # A present lead pulling away makes a predicted decel spurious in a following context -> suppress
    # the anticipatory brake (kills the low-speed rubber-band). The plan's real brake still passes below.
    eff_brake_need = self._brake_need
    if self._lead_status and self._v_rel > EARLY_BRAKE_PULLAWAY_VREL:
      eff_brake_need = 0.0
    # Taper the anticipatory brake out at low speed (stop-and-go rubber-band zone).
    eff_brake_need *= float(np.interp(self._v_ego, EARLY_BRAKE_SPEED_BP, EARLY_BRAKE_SPEED_V))

    # hysteresis: engage only on a clear predicted decel, hold until it clearly clears (no toggling)
    if self._smooth_latched:
      if eff_brake_need < SMOOTH_EXIT:
        self._smooth_latched = False
    elif eff_brake_need >= SMOOTH_ENTER:
      self._smooth_latched = True

    if not self._smooth_latched:
      # no anticipatory brake: jerk-limit the (positive) accel, but never weaken an active brake
      self._smooth_active = False
      slewed = self._slew(raw_target_accel)
      out = min(slewed, raw_target_accel) if raw_target_accel < 0.0 else slewed
      return self._finalize(out)

    # decel predicted: front-load a gentle EARLY target, but NEVER weaker than the plan.
    self._smooth_active = True
    self._decel_target = self.get_decel_target(eff_brake_need)
    commanded = min(raw_target_accel, self._decel_target)        # early-soft onset, can only brake >= plan
    slewed = self._slew(commanded)
    return self._finalize(min(slewed, raw_target_accel))         # post-slew clamp: never weaker than plan

  def _apply_stop_hold(self, out: float) -> float:
    # Latched stop: hold gently (no creep), but never weaken a deeper plan brake.
    if self._stop_held:
      out = self._clean_accel(min(out, STOP_HOLD_ACCEL))
      self._last_target_accel = out
    return out

  def _compute_brake_need(self, raw_target_accel: float, accel_trajectory: Sequence[float], t_idxs: Sequence[float]) -> float:
    min_accel = float(raw_target_accel)
    for accel, t in zip(accel_trajectory, t_idxs, strict=False):
      if float(t) <= SMOOTH_DECEL_LOOKAHEAD_T:
        min_accel = min(min_accel, float(accel))
    return max(0.0, -min_accel)

  def _emergency_bypass(self, raw_target_accel: float, should_stop: bool) -> bool:
    return (self._mpc.crash_cnt > 0 or should_stop or
            raw_target_accel <= HARD_BRAKE_TARGET_ACCEL or
            self._brake_need >= HARD_BRAKE_NEED or
            self._lead_closing)

  # --- slew / jerk limiting ---

  def _slew(self, target_accel: float) -> float:
    target_accel = float(target_accel)
    if target_accel > self._last_target_accel:
      return self._slew_up(target_accel)
    step = BRAKE_DEEPENING_JERK[self._personality] * DT_MDL
    return self._clean_accel(max(target_accel, self._last_target_accel - step))

  def _slew_up(self, target_accel: float) -> float:
    if self._last_target_accel < 0.0:
      released = min(target_accel, self._last_target_accel + BRAKE_RELEASE_JERK * DT_MDL)
      if released <= 0.0:
        return self._clean_accel(released)
      return self._clean_accel(min(target_accel, ACCEL_RISE_JERK[self._personality] * DT_MDL))

    step = ACCEL_RISE_JERK[self._personality] * DT_MDL
    return self._clean_accel(min(target_accel, self._last_target_accel + step))

  def _passthrough(self, target_accel: float) -> float:
    self._smooth_active = False
    return self._finalize(target_accel)

  def _finalize(self, target_accel: float) -> float:
    target_accel = self._clean_accel(target_accel)
    self._last_target_accel = target_accel
    return target_accel

  @staticmethod
  def _clean_accel(accel: float) -> float:
    accel = float(accel)
    return 0.0 if abs(accel) < _ZERO_ACCEL_EPS else accel

  # --- publishers (for longitudinalPlanSP.acceleration telemetry) ---

  def enabled(self) -> bool:
    return self._enabled

  def personality(self):
    return self._personality  # cereal AccelerationPersonality ordinal

  def max_accel(self) -> float:
    # Cached value for publishing; publish_longitudinal_plan_sp has no v_ego in scope.
    return self.get_max_accel(self._v_ego)

  def brake_need(self) -> float:
    return self._brake_need

  def decel_target(self) -> float:
    return self._decel_target

  def smooth_active(self) -> bool:
    return self._smooth_active

  def bypassed(self) -> bool:
    return self._bypassed

  def stop_held(self) -> bool:
    return self._stop_held
