"""把 agent 回复文本发给机器人语音服务,由机器人用豆包音色逐字念出来。

链路:  reply 文本 --HTTP POST /say--> 机器人 robot_player.py
       --豆包 SayHello 逐字合成(16kHz)--> galbot 扬声器 --> 语音输出

说明:
- TTS 在机器人端用豆包实时对话服务的 SayHello 完成(逐字念、不经大模型改写),
  Mac 端只需发文本,无需 edge-tts / ffmpeg。
- 仅当环境变量 HEYRICE_ROBOT_TTS_URL 配置时启用;未配置则不做任何事,
  Web 端行为与之前一致(机器人语音是可选增强)。
- 发送放后台线程,绝不阻塞 Web 回复;任何失败都静默降级(只打印日志)。
"""
from __future__ import annotations

import json
import os
import threading
import urllib.request

# 机器人语音服务地址,例如 http://172.16.20.160:5002/say;为空则禁用机器人语音。
ROBOT_TTS_URL = os.environ.get("HEYRICE_ROBOT_TTS_URL", "")
# 豆包音色;留空则用机器人端默认(zh_female_xiaohe_jupiter_bigtts 小何·甜美台湾腔)。
SPEAKER = os.environ.get("HEYRICE_ROBOT_TTS_SPEAKER", "")
_HTTP_TIMEOUT = 25


def enabled() -> bool:
    return bool(ROBOT_TTS_URL)


def _worker(text: str) -> None:
    try:
        payload = {"text": text}
        if SPEAKER:
            payload["speaker"] = SPEAKER
        req = urllib.request.Request(
            ROBOT_TTS_URL,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT):
            pass
    except Exception as e:  # noqa: BLE001 —— 机器人语音是增强项,失败不影响 Web
        print(f"[robot-tts] 播放失败({type(e).__name__}): {str(e)[:160]}")


def speak_async(text: str) -> None:
    """把一句回复交给机器人念出来(后台线程,不阻塞)。未配置或空文本时直接跳过。"""
    text = (text or "").strip()
    if not text or not ROBOT_TTS_URL:
        return
    threading.Thread(target=_worker, args=(text,), daemon=True, name="robot-tts").start()


def speak_blocking(text: str) -> None:
    """同步念出一句回复(阻塞到机器人播完;供"先念后送餐"顺序编排)。"""
    text = (text or "").strip()
    if not text or not ROBOT_TTS_URL:
        return
    _worker(text)
