#!/usr/bin/env python3
# -*- coding:utf-8 -*-
"""送餐机械臂轨迹回放。

复用 robo_arm.py 里的帧解析/回放函数,但用「播放服务已经 init 好的那个
GalbotRobot 实例」来跑轨迹,不再自己 init()/destroy()——避免与播放服务
争用同一台机器人(两个 GalbotRobot 实例可能冲突甚至崩溃)。

轨迹数据:robo_arm/joint.json(录制好的关节序列)。
"""
import sys
import time

ROBO_ARM_DIR = "/home/galbot/wei_fan/robo_arm"
if ROBO_ARM_DIR not in sys.path:
    sys.path.insert(0, ROBO_ARM_DIR)

import robo_arm  # noqa: E402  (依赖上面的 sys.path 注入)


def run_trajectory(
    robot,
    json_path=None,
    *,
    joint_speed=0.5,
    timeout_s=15.0,
    gripper_speed=100.0,
    command_dt=0.05,
    speed_scale=1.0,
):
    """用已 init 的 robot 回放 joint.json 送餐轨迹(不做 init/destroy)。"""
    path = json_path or robo_arm.get_default_joint_json_path()
    frames = robo_arm.load_frames_from_joint_json(path)
    # 先把机器人摆到轨迹首帧,再流式回放剩余帧。
    robo_arm.prepare_first_frame_with_set_joint_positions(
        robot, frames[0], joint_speed, timeout_s
    )
    time.sleep(0.8)
    robo_arm.replay_with_set_joint_commands(
        robot, frames, gripper_speed, command_dt, speed_scale
    )
