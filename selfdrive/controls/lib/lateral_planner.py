import os
import math
import numpy as np
from common.params import Params
from common.realtime import sec_since_boot, DT_MDL
from common.numpy_fast import interp
from selfdrive.swaglog import cloudlog
from selfdrive.controls.lib.lateral_mpc import libmpc_py
from selfdrive.controls.lib.drive_helpers import CONTROL_N, MPC_COST_LAT, LAT_MPC_N, CAR_ROTATION_RADIUS
from selfdrive.controls.lib.lane_planner import LanePlanner, TRAJECTORY_SIZE
from selfdrive.config import Conversions as CV
import cereal.messaging as messaging
from cereal import log

LaneChangeState = log.LateralPlan.LaneChangeState
LaneChangeDirection = log.LateralPlan.LaneChangeDirection

LOG_MPC = os.environ.get('LOG_MPC', False)

LANE_CHANGE_SPEED_MIN = 25 * CV.MPH_TO_MS
LANE_CHANGE_TIME_MAX = 10.

DESIRES = {
  LaneChangeDirection.none: {
    LaneChangeState.off: log.LateralPlan.Desire.none,
    LaneChangeState.preLaneChange: log.LateralPlan.Desire.none,
    LaneChangeState.laneChangeStarting: log.LateralPlan.Desire.none,
    LaneChangeState.laneChangeFinishing: log.LateralPlan.Desire.none,
  },
  LaneChangeDirection.left: {
    LaneChangeState.off: log.LateralPlan.Desire.none,
    LaneChangeState.preLaneChange: log.LateralPlan.Desire.none,
    LaneChangeState.laneChangeStarting: log.LateralPlan.Desire.laneChangeLeft,
    LaneChangeState.laneChangeFinishing: log.LateralPlan.Desire.laneChangeLeft,
  },
  LaneChangeDirection.right: {
    LaneChangeState.off: log.LateralPlan.Desire.none,
    LaneChangeState.preLaneChange: log.LateralPlan.Desire.none,
    LaneChangeState.laneChangeStarting: log.LateralPlan.Desire.laneChangeRight,
    LaneChangeState.laneChangeFinishing: log.LateralPlan.Desire.laneChangeRight,
  },
}


class LateralPlanner():
  def __init__(self, CP, use_lanelines=True, wide_camera=False):
    self.use_lanelines = use_lanelines
    self.LP = LanePlanner(wide_camera)

    self.last_cloudlog_t = 0
    self.steer_rate_cost = CP.steerRateCost

    self.setup_mpc()
    self.solution_invalid_cnt = 0

    self.laneless_mode = int(Params().get("LanelessMode", encoding="utf8"))
    self.laneless_mode_status = False
    self.laneless_mode_status_buffer = False

    self.lane_change_state = LaneChangeState.off
    self.lane_change_direction = LaneChangeDirection.none
    self.lane_change_timer = 0.0
    self.lane_change_ll_prob = 1.0
    self.keep_pulse_timer = 0.0
    self.prev_one_blinker = False
    self.desire = log.LateralPlan.Desire.none

    self.path_xyz = np.zeros((TRAJECTORY_SIZE,3))
    self.path_xyz_stds = np.ones((TRAJECTORY_SIZE,3))
    self.plan_yaw = np.zeros((TRAJECTORY_SIZE,))
    self.t_idxs = np.arange(TRAJECTORY_SIZE)
    self.y_pts = np.zeros(TRAJECTORY_SIZE)

    self.second = 0.0

  def setup_mpc(self):
    self.libmpc = libmpc_py.libmpc
    self.libmpc.init()

    self.mpc_solution = libmpc_py.ffi.new("log_t *")
    self.cur_state = libmpc_py.ffi.new("state_t *")
    self.cur_state[0].x = 0.0
    self.cur_state[0].y = 0.0
    self.cur_state[0].psi = 0.0
    self.cur_state[0].curvature = 0.0

    self.desired_curvature = 0.0
    self.safe_desired_curvature = 0.0
    self.desired_curvature_rate = 0.0
    self.safe_desired_curvature_rate = 0.0

  def update(self, sm, CP):
    self.second += DT_MDL
    if self.second > 1.0:
      self.use_lanelines = not Params().get_bool("EndToEndToggle")
      self.laneless_mode = int(Params().get("LanelessMode", encoding="utf8"))
      self.second = 0.0
    v_ego = sm['carState'].vEgo
    active = sm['controlsState'].active
    measured_curvature = sm['controlsState'].curvature

    md = sm['modelV2']
    self.LP.parse_model(sm['modelV2'])
    if len(md.position.x) == TRAJECTORY_SIZE and len(md.orientation.x) == TRAJECTORY_SIZE:
      self.path_xyz = np.column_stack([md.position.x, md.position.y, md.position.z])
      self.t_idxs = np.array(md.position.t)
      self.plan_yaw = list(md.orientation.z)
    if len(md.orientation.xStd) == TRAJECTORY_SIZE:
      self.path_xyz_stds = np.column_stack([md.position.xStd, md.position.yStd, md.position.zStd])

    # Lane change logic
    one_blinker = sm['carState'].leftBlinker != sm['carState'].rightBlinker
    below_lane_change_speed = v_ego < LANE_CHANGE_SPEED_MIN

    if (not active) or (self.lane_change_timer > LANE_CHANGE_TIME_MAX):
      self.lane_change_state = LaneChangeState.off
      self.lane_change_direction = LaneChangeDirection.none
    else:
      # LaneChangeState.off
      if self.lane_change_state == LaneChangeState.off and one_blinker and not self.prev_one_blinker and not below_lane_change_speed:
        self.lane_change_state = LaneChangeState.preLaneChange
        self.lane_change_ll_prob = 1.0

      # LaneChangeState.preLaneChange
      elif self.lane_change_state == LaneChangeState.preLaneChange:
        # Set lane change direction
        if sm['carState'].leftBlinker:
          self.lane_change_direction = LaneChangeDirection.left
        elif sm['carState'].rightBlinker:
          self.lane_change_direction = LaneChangeDirection.right
        else:  # If there are no blinkers we will go back to LaneChangeState.off
          self.lane_change_direction = LaneChangeDirection.none

        torque_applied = sm['carState'].steeringPressed and \
                        ((sm['carState'].steeringTorque > 0 and self.lane_change_direction == LaneChangeDirection.left) or
                          (sm['carState'].steeringTorque < 0 and self.lane_change_direction == LaneChangeDirection.right))
        if Params().get_bool('NudgelessALC'):
          if v_ego >= 45.0 * CV.MPH_TO_MS:
            torque_applied = True

        blindspot_detected = ((sm['carState'].leftBlindspot and self.lane_change_direction == LaneChangeDirection.left) or
                              (sm['carState'].rightBlindspot and self.lane_change_direction == LaneChangeDirection.right))

        if not one_blinker or below_lane_change_speed:
          self.lane_change_state = LaneChangeState.off
        elif torque_applied and not blindspot_detected:
          self.lane_change_state = LaneChangeState.laneChangeStarting

      # LaneChangeState.laneChangeStarting
      elif self.lane_change_state == LaneChangeState.laneChangeStarting:
        # fade out over .5s
        self.lane_change_ll_prob = max(self.lane_change_ll_prob - 2*DT_MDL, 0.0)

        # 98% certainty
        lane_change_prob = self.LP.l_lane_change_prob + self.LP.r_lane_change_prob
        if lane_change_prob < 0.02 and self.lane_change_ll_prob < 0.01:
          self.lane_change_state = LaneChangeState.laneChangeFinishing

      # LaneChangeState.laneChangeFinishing
      elif self.lane_change_state == LaneChangeState.laneChangeFinishing:
        # fade in laneline over 1s
        self.lane_change_ll_prob = min(self.lane_change_ll_prob + DT_MDL, 1.0)
        if one_blinker and self.lane_change_ll_prob > 0.99:
          self.lane_change_state = LaneChangeState.preLaneChange
        elif self.lane_change_ll_prob > 0.99:
          self.lane_change_state = LaneChangeState.off

    if self.lane_change_state in [LaneChangeState.off, LaneChangeState.preLaneChange]:
      self.lane_change_timer = 0.0
    else:
      self.lane_change_timer += DT_MDL

    self.prev_one_blinker = one_blinker

    self.desire = DESIRES[self.lane_change_direction][self.lane_change_state]

    # Send keep pulse once per second during LaneChangeStart.preLaneChange
    if self.lane_change_state in [LaneChangeState.off, LaneChangeState.laneChangeStarting]:
      self.keep_pulse_timer = 0.0
    elif self.lane_change_state == LaneChangeState.preLaneChange:
      self.keep_pulse_timer += DT_MDL
      if self.keep_pulse_timer > 1.0:
        self.keep_pulse_timer = 0.0
      elif self.desire in [log.LateralPlan.Desire.keepLeft, log.LateralPlan.Desire.keepRight]:
        self.desire = log.LateralPlan.Desire.none

    # Turn off lanes during lane change
    if self.desire == log.LateralPlan.Desire.laneChangeRight or self.desire == log.LateralPlan.Desire.laneChangeLeft:
      self.LP.lll_prob *= self.lane_change_ll_prob
      self.LP.rll_prob *= self.lane_change_ll_prob
    if self.use_lanelines:
      d_path_xyz = self.LP.get_d_path(v_ego, self.t_idxs, self.path_xyz)
      self.libmpc.set_weights(MPC_COST_LAT.PATH, MPC_COST_LAT.HEADING, CP.steerRateCost)
      self.laneless_mode_status = False
    elif self.laneless_mode == 0:
      d_path_xyz = self.LP.get_d_path(v_ego, self.t_idxs, self.path_xyz)
      self.libmpc.set_weights(MPC_COST_LAT.PATH, MPC_COST_LAT.HEADING, CP.steerRateCost)
      self.laneless_mode_status = False
    elif self.laneless_mode == 1:
      d_path_xyz = self.path_xyz
      path_cost = np.clip(abs(self.path_xyz[0,1]/self.path_xyz_stds[0,1]), 0.5, 5.0) * MPC_COST_LAT.PATH
      # Heading cost is useful at low speed, otherwise end of plan can be off-heading
      heading_cost = interp(v_ego, [5.0, 10.0], [MPC_COST_LAT.HEADING, 0.0])
      self.libmpc.set_weights(path_cost, heading_cost, CP.steerRateCost)
      self.laneless_mode_status = True
    elif self.laneless_mode == 2 and ((self.LP.lll_prob + self.LP.rll_prob)/2 < 0.3) and self.lane_change_state == LaneChangeState.off:
      d_path_xyz = self.path_xyz
      path_cost = np.clip(abs(self.path_xyz[0,1]/self.path_xyz_stds[0,1]), 0.5, 5.0) * MPC_COST_LAT.PATH
      # Heading cost is useful at low speed, otherwise end of plan can be off-heading
      heading_cost = interp(v_ego, [5.0, 10.0], [MPC_COST_LAT.HEADING, 0.0])
      self.libmpc.set_weights(path_cost, heading_cost, CP.steerRateCost)
      self.laneless_mode_status = True
      self.laneless_mode_status_buffer = True
    elif self.laneless_mode == 2 and ((self.LP.lll_prob + self.LP.rll_prob)/2 > 0.5) and \
     self.laneless_mode_status_buffer and self.lane_change_state == LaneChangeState.off:
      d_path_xyz = self.LP.get_d_path(v_ego, self.t_idxs, self.path_xyz)
      self.libmpc.set_weights(MPC_COST_LAT.PATH, MPC_COST_LAT.HEADING, CP.steerRateCost)
      self.laneless_mode_status = False
      self.laneless_mode_status_buffer = False
    elif self.laneless_mode == 2 and self.laneless_mode_status_buffer == True and self.lane_change_state == LaneChangeState.off:
      d_path_xyz = self.path_xyz
      path_cost = np.clip(abs(self.path_xyz[0,1]/self.path_xyz_stds[0,1]), 0.5, 5.0) * MPC_COST_LAT.PATH
      # Heading cost is useful at low speed, otherwise end of plan can be off-heading
      heading_cost = interp(v_ego, [5.0, 10.0], [MPC_COST_LAT.HEADING, 0.0])
      self.libmpc.set_weights(path_cost, heading_cost, CP.steerRateCost)
      self.laneless_mode_status = True
    else:
      d_path_xyz = self.LP.get_d_path(v_ego, self.t_idxs, self.path_xyz)
      self.libmpc.set_weights(MPC_COST_LAT.PATH, MPC_COST_LAT.HEADING, CP.steerRateCost)
      self.laneless_mode_status = False
      self.laneless_mode_status_buffer = False

    y_pts = np.interp(v_ego * self.t_idxs[:LAT_MPC_N + 1], np.linalg.norm(d_path_xyz, axis=1), d_path_xyz[:,1])
    heading_pts = np.interp(v_ego * self.t_idxs[:LAT_MPC_N + 1], np.linalg.norm(self.path_xyz, axis=1), self.plan_yaw)
    self.y_pts = y_pts

    assert len(y_pts) == LAT_MPC_N + 1
    assert len(heading_pts) == LAT_MPC_N + 1
    # for now CAR_ROTATION_RADIUS is disabled
    # to use it, enable it in the MPC
    assert abs(CAR_ROTATION_RADIUS) < 1e-3
    self.libmpc.run_mpc(self.cur_state, self.mpc_solution,
                        float(v_ego),
                        CAR_ROTATION_RADIUS,
                        list(y_pts),
                        list(heading_pts))
    # init state for next
    self.cur_state.x = 0.0
    self.cur_state.y = 0.0
    self.cur_state.psi = 0.0
    self.cur_state.curvature = interp(DT_MDL, self.t_idxs[:LAT_MPC_N + 1], self.mpc_solution.curvature)

    #  Check for infeasable MPC solution
    mpc_nans = any(math.isnan(x) for x in self.mpc_solution.curvature)
    t = sec_since_boot()
    if mpc_nans:
      self.libmpc.init()
      self.cur_state.curvature = measured_curvature

      if t > self.last_cloudlog_t + 5.0:
        self.last_cloudlog_t = t
        cloudlog.warning("Lateral mpc - nan: True")

    if self.mpc_solution[0].cost > 20000. or mpc_nans:   # TODO: find a better way to detect when MPC did not converge
      self.solution_invalid_cnt += 1
    else:
      self.solution_invalid_cnt = 0

  def publish(self, sm, pm):
    plan_solution_valid = self.solution_invalid_cnt < 2
    plan_send = messaging.new_message('lateralPlan')
    plan_send.valid = sm.all_alive_and_valid(service_list=['carState', 'controlsState', 'modelV2'])
    plan_send.lateralPlan.laneWidth = float(self.LP.lane_width)
    plan_send.lateralPlan.dPathPoints = [float(x) for x in self.y_pts]
    plan_send.lateralPlan.psis = [float(x) for x in self.mpc_solution.psi[0:CONTROL_N]]
    plan_send.lateralPlan.curvatures = [float(x) for x in self.mpc_solution.curvature[0:CONTROL_N]]
    plan_send.lateralPlan.curvatureRates = [float(x) for x in self.mpc_solution.curvature_rate[0:CONTROL_N-1]] +[0.0]
    plan_send.lateralPlan.lProb = float(self.LP.lll_prob)
    plan_send.lateralPlan.rProb = float(self.LP.rll_prob)
    plan_send.lateralPlan.dProb = float(self.LP.d_prob)

    plan_send.lateralPlan.mpcSolutionValid = bool(plan_solution_valid)

    plan_send.lateralPlan.desire = self.desire
    plan_send.lateralPlan.laneChangeState = self.lane_change_state
    plan_send.lateralPlan.laneChangeDirection = self.lane_change_direction

    plan_send.lateralPlan.lanelessMode = bool(self.laneless_mode_status)

    pm.send('lateralPlan', plan_send)

    if LOG_MPC:
      dat = messaging.new_message('liveMpc')
      dat.liveMpc.x = list(self.mpc_solution.x)
      dat.liveMpc.y = list(self.mpc_solution.y)
      dat.liveMpc.psi = list(self.mpc_solution.psi)
      dat.liveMpc.curvature = list(self.mpc_solution.curvature)
      dat.liveMpc.cost = self.mpc_solution.cost
      pm.send('liveMpc', dat)
