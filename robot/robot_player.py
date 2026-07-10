#!/usr/bin/env python3
# -*- coding:utf-8 -*-
"""常驻播放服务(运行在 Galbot 机器人上)。

接收 PCM(s16le / 24000Hz / mono),通过 galbot 扬声器播放。
Hey-Rice(Mac/服务端) 把 agent 回复经 edge-tts 合成的语音 POST 到 /play 播放。

依赖:flask、galbot_sdk(vendored,需 run_player.sh 设好 PYTHONPATH/LD_LIBRARY_PATH)。
启动:见同目录 run_player.sh。
"""
import time
import threading

from flask import Flask, request, jsonify
from galbot_sdk.g1 import GalbotRobot

CHUNK = 2560  # 与 galbot 音频流分片一致
app = Flask(__name__)
_robot = None
_lock = threading.Lock()  # 串行播放,避免多请求抢扬声器交错


def get_robot():
    global _robot
    if _robot is None:
        r = GalbotRobot()
        if not r.init():
            raise RuntimeError("robot init failed")
        time.sleep(2)
        _robot = r
        print("[player] robot ready", flush=True)
    return _robot


@app.route("/health")
def health():
    return jsonify(ok=True, ready=_robot is not None)


@app.route("/play", methods=["POST"])
def play():
    pcm = request.get_data()
    if not pcm:
        return jsonify(error="empty pcm"), 400
    with _lock:
        robot = get_robot()
        for i in range(0, len(pcm), CHUNK):
            robot.write_audio_stream_output(pcm[i:i + CHUNK], "speaker")
            time.sleep(0.02)
        time.sleep(0.3)
    print(f"[player] played {len(pcm)} bytes", flush=True)
    return jsonify(ok=True, bytes=len(pcm))


if __name__ == "__main__":
    print("[player] initializing robot ...", flush=True)
    get_robot()
    print("[player] serving on 0.0.0.0:5002", flush=True)
    app.run(host="0.0.0.0", port=5002, threaded=True)
