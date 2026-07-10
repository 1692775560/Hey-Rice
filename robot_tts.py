"""把 agent 回复文本合成语音,推给机器人播放服务(robot_player.py)念出来。

链路:  reply 文本 --edge-tts--> mp3 --ffmpeg--> PCM(s16le/24000/mono)
       --HTTP POST--> 机器人 /play --galbot 扬声器--> 语音输出

设计:
- 仅当环境变量 HEYRICE_ROBOT_TTS_URL 配置时启用;未配置则完全不做任何事,
  Web 端行为与之前一致(机器人语音是可选增强)。
- 合成+推送放后台线程,绝不阻塞 Web 回复;任何失败都静默降级(只打印日志)。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import threading
import urllib.request

# 机器人播放服务地址,例如 http://172.16.20.160:5002/play;为空则禁用机器人语音。
ROBOT_TTS_URL = os.environ.get("HEYRICE_ROBOT_TTS_URL", "")
# edge-tts 音色(中文女声);可换 zh-CN-YunxiNeural 等。
VOICE = os.environ.get("HEYRICE_ROBOT_TTS_VOICE", "zh-CN-XiaoxiaoNeural")
FFMPEG = os.environ.get("HEYRICE_FFMPEG", "ffmpeg")
# 机器人 galbot 扬声器采样率(与 robot_player.py 一致)
SAMPLE_RATE = "24000"
_HTTP_TIMEOUT = 20


def enabled() -> bool:
    return bool(ROBOT_TTS_URL)


async def _synth_mp3(text: str) -> bytes:
    """edge-tts 流式合成,聚合成完整 mp3 字节。"""
    import edge_tts

    comm = edge_tts.Communicate(text, VOICE)
    buf = bytearray()
    async for chunk in comm.stream():
        if chunk["type"] == "audio":
            buf += chunk["data"]
    if not buf:
        raise RuntimeError("edge-tts 未返回音频")
    return bytes(buf)


def _mp3_to_pcm(mp3: bytes) -> bytes:
    """用 ffmpeg 把 mp3 转成机器人扬声器需要的裸 PCM。"""
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error",
         "-i", "pipe:0", "-f", "s16le", "-acodec", "pcm_s16le",
         "-ac", "1", "-ar", SAMPLE_RATE, "pipe:1"],
        input=mp3, capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError("ffmpeg 转码失败: " + proc.stderr.decode("utf-8", "replace")[:200])
    return proc.stdout


def _worker(text: str) -> None:
    try:
        mp3 = asyncio.run(_synth_mp3(text))
        pcm = _mp3_to_pcm(mp3)
        req = urllib.request.Request(
            ROBOT_TTS_URL, data=pcm,
            headers={"Content-Type": "application/octet-stream"},
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
