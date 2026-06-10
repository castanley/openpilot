"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""

from types import SimpleNamespace

import numpy as np
import pytest

from openpilot.sunnypilot.selfdrive.controls.lib.accel_personality.accel_controller import AccelController
from openpilot.sunnypilot.selfdrive.controls.lib.accel_personality.constants import \
  ECO, NORMAL, SPORT, PERSONALITY_MIN, PERSONALITY_MAX, A_CRUISE_MAX_BP, RISE_RATE, \
  STOCK_A_CRUISE_MAX_V, STOCK_RISE_RATE, HARD_BRAKE_TARGET_ACCEL, AccelerationPersonality

# t<=2.5 frames feed the lookahead; the rest are beyond it.
T_IDXS = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 4.0]
_EPS = 1e-6


class FakeParams:
  def __init__(self, store=None):
    self.store = dict(store or {})

  def get_bool(self, key):
    return bool(self.store.get(key, False))

  def get(self, key, return_default=False):
    return int(self.store.get(key, 1))

  def put(self, key, val, block=False):
    self.store[key] = val

  def put_bool(self, key, val, block=False):
    self.store[key] = bool(val)


def make_sm(v_ego=20.0, lead_status=False, v_rel=0.0, d_rel=50.0):
  lead = SimpleNamespace(status=lead_status, vRel=v_rel, dRel=d_rel, vLead=v_ego + v_rel, aLeadK=0.0, modelProb=0.9)
  return {
    'carState': SimpleNamespace(vEgo=v_ego),
    'radarState': SimpleNamespace(leadOne=lead),
  }


def make_controller(enabled=True, personality=NORMAL, crash_cnt=0):
  store = {"AccelPersonalityEnabled": enabled, "AccelPersonality": int(personality)}
  mpc = SimpleNamespace(crash_cnt=crash_cnt)
  ctrl = AccelController(CP=SimpleNamespace(), mpc=mpc, params=FakeParams(store))
  ctrl.update(make_sm())
  return ctrl


def flat_traj(value):
  return [float(value)] * len(T_IDXS)


# --- enum source of truth ---

def test_enum_source_parity():
  assert (ECO, NORMAL, SPORT) == (AccelerationPersonality.eco, AccelerationPersonality.normal, AccelerationPersonality.sport)
  assert (PERSONALITY_MIN, PERSONALITY_MAX) == (0, 2)


# --- disabled / normal == stock ---

def test_disabled_forces_normal_and_stock_ceiling():
  ctrl = make_controller(enabled=False, personality=SPORT)
  assert ctrl.personality() == NORMAL
  assert not ctrl.enabled()
  for v in (0.0, 10.0, 25.0, 40.0):
    assert ctrl.get_max_accel(v) == pytest.approx(np.interp(v, A_CRUISE_MAX_BP, STOCK_A_CRUISE_MAX_V))
  assert ctrl.get_rise_rate() == STOCK_RISE_RATE


def test_disabled_passes_brake_through():
  ctrl = make_controller(enabled=False)
  for raw in (-1.5, -0.5, 0.0, 1.0):
    out = ctrl.smooth_target_accel(raw, flat_traj(raw), T_IDXS, should_stop=False)
    assert out == pytest.approx(raw, abs=_EPS)


def test_normal_matches_stock():
  ctrl = make_controller(personality=NORMAL)
  for v in (0.0, 5.0, 10.0, 25.0, 40.0):
    assert ctrl.get_max_accel(v) == pytest.approx(np.interp(v, A_CRUISE_MAX_BP, STOCK_A_CRUISE_MAX_V))
  assert ctrl.get_rise_rate() == STOCK_RISE_RATE


# --- per-tier ordering ---

def test_ceiling_ordering_eco_lt_normal_lt_sport():
  eco, normal, sport = (make_controller(personality=p) for p in (ECO, NORMAL, SPORT))
  for v in (0.0, 10.0, 25.0, 40.0):
    assert eco.get_max_accel(v) < normal.get_max_accel(v) < sport.get_max_accel(v)


def test_rise_rate_ordering():
  assert RISE_RATE[ECO] < RISE_RATE[NORMAL] < RISE_RATE[SPORT]
  assert make_controller(personality=ECO).get_rise_rate() == RISE_RATE[ECO]
  assert make_controller(personality=SPORT).get_rise_rate() == RISE_RATE[SPORT]


# --- early soft braking front-loads ---

def test_early_soft_braking_brakes_before_plan():
  # plan not braking yet (raw ~ 0) but a decel is predicted in the lookahead -> command an early gentle brake
  ctrl = make_controller(personality=NORMAL)
  out = ctrl.smooth_target_accel(0.0, flat_traj(-1.0), T_IDXS, should_stop=False)
  assert out < 0.0
  assert ctrl.smooth_active()
  assert ctrl.brake_need() == pytest.approx(1.0)


# --- never-weaken invariant (route 000003da regression guard) ---

@pytest.mark.parametrize("personality", [ECO, NORMAL, SPORT])
def test_never_weaker_than_plan_sustained_closing(personality):
  # Sustained moderate closing lead: plan ramps to -1.5 and holds. The controller must NEVER command
  # less braking than the plan on any frame (this is the 000003da driver-takeover failure mode).
  ctrl = make_controller(personality=personality)
  raw_seq = [0.0, -0.2, -0.5, -0.9, -1.2, -1.5] + [-1.5] * 40
  for raw in raw_seq:
    ctrl.update(make_sm(v_ego=15.0))  # no closing-bypass lead -> stays in the shaping zone
    out = ctrl.smooth_target_accel(raw, flat_traj(raw), T_IDXS, should_stop=False)
    assert out <= raw + _EPS, f"under-braked: out={out} > raw={raw}"


@pytest.mark.parametrize("personality", [ECO, NORMAL, SPORT])
def test_never_weaker_random_walk(personality):
  # Braking invariant: whenever the plan is braking (raw < 0), the controller must never command less
  # braking. (For raw >= 0 the accel rate-limiter may legitimately ride above the plan.)
  rng = np.random.default_rng(0)
  ctrl = make_controller(personality=personality)
  for _ in range(500):
    raw = float(rng.uniform(-1.9, 1.5))            # stay above the hard-brake bypass threshold
    traj_min = raw - float(rng.uniform(0.0, 0.6))  # predicted decel at or below the current plan
    traj = flat_traj(traj_min)
    ctrl.update(make_sm(v_ego=20.0))
    out = ctrl.smooth_target_accel(raw, traj, T_IDXS, should_stop=False)
    if raw < 0.0:
      assert out <= raw + _EPS


# --- bypasses hand fully back to the plan ---

def test_hard_brake_bypass():
  ctrl = make_controller(personality=ECO)
  raw = HARD_BRAKE_TARGET_ACCEL - 0.5
  out = ctrl.smooth_target_accel(raw, flat_traj(raw), T_IDXS, should_stop=False)
  assert out == pytest.approx(raw, abs=_EPS)
  assert ctrl.bypassed()


def test_should_stop_bypass():
  ctrl = make_controller(personality=ECO)
  out = ctrl.smooth_target_accel(-1.0, flat_traj(-1.0), T_IDXS, should_stop=True)
  assert out == pytest.approx(-1.0, abs=_EPS)
  assert ctrl.bypassed()


def test_fcw_crash_cnt_bypass():
  ctrl = make_controller(personality=ECO, crash_cnt=3)
  out = ctrl.smooth_target_accel(-1.0, flat_traj(-1.0), T_IDXS, should_stop=False)
  assert out == pytest.approx(-1.0, abs=_EPS)
  assert ctrl.bypassed()


def test_closing_lead_bypass():
  # a fast-closing lead must bypass shaping even in the soft (-0.05..-2.0) zone
  ctrl = make_controller(personality=ECO)
  ctrl.update(make_sm(v_ego=20.0, lead_status=True, v_rel=-10.0, d_rel=40.0))
  out = ctrl.smooth_target_accel(-1.2, flat_traj(-1.2), T_IDXS, should_stop=False)
  assert out == pytest.approx(-1.2, abs=_EPS)
  assert ctrl.bypassed()


def test_e2e_brake_passthrough():
  # blended/e2e path: braking is never reshaped
  ctrl = make_controller(personality=ECO)
  out = ctrl.smooth_target_accel(-1.0, flat_traj(-1.0), T_IDXS, should_stop=False, stock_brake=True)
  assert out == pytest.approx(-1.0, abs=_EPS)
  assert not ctrl.smooth_active()


# --- rubber-band fixes: pull-away suppression + hysteresis ---

def test_lead_pullaway_suppresses_early_brake():
  # lead pulling away (vRel>0.5) -> model-predicted decel is spurious -> no anticipatory brake
  ctrl = make_controller(personality=ECO)
  ctrl.update(make_sm(v_ego=10.0, lead_status=True, v_rel=1.5, d_rel=20.0))
  out = ctrl.smooth_target_accel(0.0, flat_traj(-1.5), T_IDXS, should_stop=False)
  assert not ctrl.smooth_active()
  assert out >= -_EPS


def test_low_speed_tapers_anticipatory_brake():
  # at low speed the anticipatory brake is tapered out (rubber-band zone); plan brake still passes
  ctrl = make_controller(personality=ECO)
  ctrl.update(make_sm(v_ego=3.0))   # < 6 m/s -> gain 0
  out = ctrl.smooth_target_accel(0.0, flat_traj(-1.5), T_IDXS, should_stop=False)
  assert not ctrl.smooth_active()
  assert out >= -_EPS
  # same predicted decel at speed engages
  ctrl.update(make_sm(v_ego=15.0))
  out = ctrl.smooth_target_accel(0.0, flat_traj(-1.5), T_IDXS, should_stop=False)
  assert ctrl.smooth_active()


def test_smooth_hysteresis_no_toggle():
  ctrl = make_controller(personality=NORMAL)
  ctrl.update(make_sm(v_ego=20.0))
  # brake_need 0.25 (between EXIT 0.15 and ENTER 0.40): does NOT engage from idle
  ctrl.smooth_target_accel(0.0, flat_traj(-0.25), T_IDXS, should_stop=False)
  assert not ctrl.smooth_active()
  # clear decel engages
  ctrl.smooth_target_accel(0.0, flat_traj(-0.6), T_IDXS, should_stop=False)
  assert ctrl.smooth_active()
  # drops to 0.25 (still > EXIT): stays engaged (hysteresis, no toggle)
  ctrl.smooth_target_accel(0.0, flat_traj(-0.25), T_IDXS, should_stop=False)
  assert ctrl.smooth_active()


# --- stop-hold / anti-creep (double-stop fix) ---

def test_stop_hold_prevents_creep():
  ctrl = make_controller(personality=ECO)
  ctrl.update(make_sm(v_ego=0.0, lead_status=True, v_rel=0.0, d_rel=5.0))  # stopped behind stopped lead
  assert ctrl.stop_held()
  out = ctrl.smooth_target_accel(0.3, flat_traj(0.3), T_IDXS, should_stop=False)  # plan wants to creep
  assert out <= 0.0  # creep suppressed


def test_stop_hold_ignores_lead_twitch():
  ctrl = make_controller(personality=ECO)
  ctrl.update(make_sm(v_ego=0.0, lead_status=True, v_rel=0.0, d_rel=5.0))
  assert ctrl.stop_held()
  ctrl.update(make_sm(v_ego=0.0, lead_status=True, v_rel=1.0, d_rel=5.8))  # twitch: vLead 1.0<1.5, dRel +0.8<2.0
  assert ctrl.stop_held()


def test_stop_hold_releases_on_real_departure():
  ctrl = make_controller(personality=ECO)
  ctrl.update(make_sm(v_ego=0.0, lead_status=True, v_rel=0.0, d_rel=5.0))
  assert ctrl.stop_held()
  ctrl.update(make_sm(v_ego=0.0, lead_status=True, v_rel=2.0, d_rel=7.5))  # lead clearly departing (vLead 2.0)
  assert not ctrl.stop_held()
  out = ctrl.smooth_target_accel(0.5, flat_traj(0.5), T_IDXS, should_stop=False)
  assert out > 0.0  # launch allowed


def test_stop_hold_disabled_is_stock():
  ctrl = make_controller(enabled=False)
  ctrl.update(make_sm(v_ego=0.0, lead_status=True, v_rel=0.0, d_rel=5.0))
  assert not ctrl.stop_held()  # never latches when disabled (off == stock)
  out = ctrl.smooth_target_accel(0.3, flat_traj(0.3), T_IDXS, should_stop=False)
  assert out == pytest.approx(0.3, abs=_EPS)


# --- param sanitation ---

def test_out_of_range_personality_clamps():
  store = {"AccelPersonalityEnabled": True, "AccelPersonality": 99}
  ctrl = AccelController(CP=SimpleNamespace(), mpc=SimpleNamespace(crash_cnt=0), params=FakeParams(store))
  ctrl.update(make_sm())
  assert ctrl.personality() == PERSONALITY_MAX


def test_reset_passes_through():
  ctrl = make_controller(personality=ECO)
  out = ctrl.smooth_target_accel(0.0, flat_traj(-1.0), T_IDXS, should_stop=False, reset=True)
  assert out == pytest.approx(0.0, abs=_EPS)
  assert not ctrl.bypassed()
