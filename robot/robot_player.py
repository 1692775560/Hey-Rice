#!/usr/bin/env python3
# -*- coding:utf-8 -*-
"""机器人常驻语音服务(运行在 Galbot 机器人上)。

 /say  : 收 {"text","speaker"?} → 豆包 SayHello 逐字合成 → galbot 扬声器播放
 /play : 收裸 PCM(s16le/16000/mono) → 直接播放(备用)
 /health

依赖:flask、galbot_sdk(vendored,需 run_player.sh 设好环境)、同目录 doubao_say.py。
启动:见同目录 run_player.sh。
"""
import os
import time
import threading

from flask import Flask, request, jsonify
from galbot_sdk.g1 import GalbotRobot
import doubao_say

CHUNK = 2560
# 机器人扬声器实际采样率是 16000Hz;豆包按 24k 合成后由 doubao_say 重采样到这个值。
DEFAULT_SPEAKER = os.environ.get("ROBOT_TTS_SPEAKER", "zh_female_xiaohe_jupiter_bigtts")
app = Flask(__name__)
_robot = None
_play_lock = threading.Lock()


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


def _play_pcm(pcm):
    with _play_lock:
        robot = get_robot()
        for i in range(0, len(pcm), CHUNK):
            robot.write_audio_stream_output(pcm[i:i + CHUNK], "speaker")
            time.sleep(0.02)
        time.sleep(0.3)


@app.route("/health")
def health():
    return jsonify(ok=True, ready=_robot is not None, speaker=DEFAULT_SPEAKER)


@app.route("/say", methods=["POST"])
def say():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    speaker = data.get("speaker") or DEFAULT_SPEAKER
    if not text:
        return jsonify(error="empty text"), 400
    if len(text) > 500:          # 一句回复不该这么长;截断防滥用/超长合成
        text = text[:500]
    try:
        pcm = doubao_say.synth(text, speaker=speaker)   # 网络合成放锁外
    except Exception as e:  # noqa: BLE001
        print(f"[player] doubao synth 失败: {e}", flush=True)
        return jsonify(error=f"tts failed: {e}"), 502
    if not pcm:
        return jsonify(error="no audio"), 502
    _play_pcm(pcm)
    print(f"[player] said {len(text)} chars / {len(pcm)} bytes ({speaker})", flush=True)
    return jsonify(ok=True, bytes=len(pcm))


@app.route("/play", methods=["POST"])
def play():
    pcm = request.get_data()
    if not pcm:
        return jsonify(error="empty pcm"), 400
    _play_pcm(pcm)
    print(f"[player] played {len(pcm)} bytes", flush=True)
    return jsonify(ok=True, bytes=len(pcm))


if __name__ == "__main__":
    print("[player] initializing robot ...", flush=True)
    get_robot()
    print(f"[player] serving on 0.0.0.0:5002  speaker={DEFAULT_SPEAKER}", flush=True)
    app.run(host="0.0.0.0", port=5002, threaded=True)
