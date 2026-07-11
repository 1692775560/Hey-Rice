"""送餐命令触发机器人机械臂轨迹。

当意图执行到「送餐」(deliver_food) 时,POST 到机器人的 /arm 接口,由机器人用
它自己那台 robot 回放 robo_arm/joint.json 轨迹(见 robot/arm_action.py)。

- 仅当环境变量 HEYRICE_ROBOT_ARM_URL 配置时启用;未配置则不做任何事,
  动作层保持 mock,不会有真实机械臂运动。
- 触发放后台线程,不阻塞 Web 回复;失败静默降级(只打印日志)。
"""
from __future__ import annotations

import json
import os
import threading
import urllib.request

# 机器人机械臂接口,例如 http://172.16.20.160:5002/arm;为空则禁用真实机械臂。
ROBOT_ARM_URL = os.environ.get("HEYRICE_ROBOT_ARM_URL", "")
_HTTP_TIMEOUT = 30


def enabled() -> bool:
    return bool(ROBOT_ARM_URL)


def _worker(action: str) -> None:
    try:
        req = urllib.request.Request(
            ROBOT_ARM_URL,
            data=json.dumps({"action": action}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT):
            pass
    except Exception as e:  # noqa: BLE001 —— 真实机械臂是增强项,失败不影响 Web
        print(f"[robot-arm] 触发失败({type(e).__name__}): {str(e)[:160]}")


def trigger_async(action: str = "deliver_food") -> None:
    """触发一次机械臂轨迹(后台线程,不阻塞)。未配置时直接跳过。"""
    if not ROBOT_ARM_URL:
        return
    threading.Thread(target=_worker, args=(action,), daemon=True, name="robot-arm").start()


def trigger_blocking(action: str = "deliver_food") -> None:
    """同步触发机械臂轨迹(阻塞到机器人接受请求;供"先念后送餐"顺序编排)。"""
    if not ROBOT_ARM_URL:
        return
    _worker(action)
