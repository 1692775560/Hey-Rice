#!/bin/bash
# 在 Galbot 机器人上启动常驻播放服务(robot_player.py)。
# 路径按机器人实际部署调整;下面为参考部署路径。
cd /home/galbot/wei_fan/wei_audio
export LD_LIBRARY_PATH=/home/galbot/wei_fan/GalbotSDK/galbot_sdk/linux-aarch64-gcc940/lib:/userdata/update/manual_update/lib
export PYTHONPATH=/home/galbot/wei_fan/GalbotSDK/galbot_sdk/linux-aarch64-gcc940/lib/python
exec python3 -u robot_player.py
