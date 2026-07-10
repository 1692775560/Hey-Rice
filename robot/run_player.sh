#!/bin/bash
# 在 Galbot 机器人上启动常驻语音服务(robot_player.py),带崩溃自动重启。
# 路径按机器人实际部署;下面为参考部署路径。
cd /home/galbot/wei_fan/wei_audio

# 豆包密钥从同目录 robot.env 读取(该文件不入库,内容示例见 robot.env.example)
[ -f robot.env ] && set -a && . ./robot.env && set +a

# galbot_sdk(vendored)运行环境
export LD_LIBRARY_PATH=/home/galbot/wei_fan/GalbotSDK/galbot_sdk/linux-aarch64-gcc940/lib:/userdata/update/manual_update/lib
export PYTHONPATH=/home/galbot/wei_fan/GalbotSDK/galbot_sdk/linux-aarch64-gcc940/lib/python

# 守护:进程退出(含 SDK 退出段错误)则 2 秒后自动拉起
while true; do
  python3 -u robot_player.py
  echo "[run_player] 退出($?),2s 后重启..."
  sleep 2
done
