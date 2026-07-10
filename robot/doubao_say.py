#!/usr/bin/env python3
# -*- coding:utf-8 -*-
"""豆包音色逐字 TTS(SayHello)。

用现有「实时对话」服务(volc.speech.dialog)的 SayHello 事件,把给定文本逐字合成
语音(不经大模型改写)。向豆包请求原生 24kHz(音质好),再用 ffmpeg 重采样到机器人
扬声器的 16kHz(清亮不闷)。

  synth(text, speaker) -> PCM(s16le / 16000 / mono)

密钥从环境变量读取(由 run_player.sh 注入),不硬编码、不入库:
  DOUBAO_APP_ID / DOUBAO_ACCESS_TOKEN
"""
import os
import struct
import uuid
import json
import time
import subprocess
import websocket

APP_ID = os.environ.get("DOUBAO_APP_ID", "")
ACCESS_TOKEN = os.environ.get("DOUBAO_ACCESS_TOKEN", "")
API_URL = os.environ.get("DOUBAO_DIALOG_ENDPOINT",
                         "wss://openspeech.bytedance.com/api/v3/realtime/dialogue")
RESOURCE_ID = os.environ.get("DOUBAO_DIALOG_RESOURCE_ID", "volc.speech.dialog")
APP_KEY = os.environ.get("DOUBAO_APP_KEY", "PlgvMymc7f3tQnJ6")  # 该产品的固定公开 app key

DOUBAO_RATE = 24000   # 向豆包请求的原生采样率(高音质)
DEVICE_RATE = 16000   # 机器人扬声器实际采样率
_FULL = 0x1
_SER = 0x1
_FLAG = 0b0100


def _pk(eid, meta, sid=None):
    h = struct.pack('BBBB', (0x1 << 4) | 0x1, (_FULL << 4) | _FLAG, (_SER << 4) | 0x0, 0)
    d = h + struct.pack('>I', eid)
    if sid is not None:
        s = sid.encode()
        d += struct.pack('>I', len(s)) + s
    b = json.dumps(meta, ensure_ascii=False).encode()
    d += struct.pack('>I', len(b)) + b
    return d


def _parse(data):
    if len(data) < 4:
        return None, None
    b1 = data[1]
    b2 = data[2]
    mt = (b1 >> 4) & 0xF
    ser = (b2 >> 4) & 0xF
    off = 4
    if mt == 0xF:
        off += 4
        ps = struct.unpack('>I', data[off:off + 4])[0]
        off += 4
        return ("ERR",), (data[off:off + ps] if ps else b'')
    eid = None
    if mt in (0x9, 0xB):
        eid = struct.unpack('>I', data[off:off + 4])[0]
        off += 4
        if eid >= 100:
            sl = struct.unpack('>I', data[off:off + 4])[0]
            off += 4
            off += sl if sl > 0 else 0
    if off + 4 > len(data):
        return eid, b''
    ps = struct.unpack('>I', data[off:off + 4])[0]
    off += 4
    pl = data[off:off + ps] if ps > 0 and off + ps <= len(data) else b''
    if ser == _SER and pl:
        try:
            pl = json.loads(pl.decode('utf-8'))
        except Exception:
            pass
    return eid, pl


def _resample(pcm, src, dst):
    if src == dst or not pcm:
        return pcm
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-f", "s16le", "-ar", str(src), "-ac", "1", "-i", "pipe:0",
         "-f", "s16le", "-ar", str(dst), "-ac", "1", "pipe:1"],
        input=pcm, capture_output=True)
    return p.stdout or pcm


def synth(text, speaker="zh_female_xiaohe_jupiter_bigtts", out_rate=DEVICE_RATE, timeout=15):
    ws = websocket.create_connection(API_URL, header={
        "X-Api-App-ID": APP_ID, "X-Api-Access-Key": ACCESS_TOKEN,
        "X-Api-Resource-Id": RESOURCE_ID, "X-Api-App-Key": APP_KEY,
        "X-Api-Connect-Id": str(uuid.uuid4())}, timeout=timeout)

    def r1():
        x = ws.recv()
        return _parse(x.encode() if isinstance(x, str) else x)

    try:
        ws.send_binary(_pk(1, {}))
        r1()
        sid = str(uuid.uuid4())
        cfg = {"dialog": {"bot_name": "小瓜", "extra": {"model": "1.2.1.1"}},
               "tts": {"speaker": speaker,
                       "audio_config": {"format": "pcm_s16le", "sample_rate": DOUBAO_RATE, "channel": 1}},
               "asr": {"extra": {"end_smooth_window_ms": 1500}}}
        ws.send_binary(_pk(100, cfg, sid))
        r1()
        ws.send_binary(_pk(300, {"content": text}, sid))  # SayHello:逐字合成
        ws.settimeout(timeout)
        audio = bytearray()
        t0 = time.time()
        while time.time() - t0 < timeout:
            e, p = r1()
            if e == 352 and isinstance(p, (bytes, bytearray)):
                audio += p
            elif e in (359, 600, 152):
                break
            elif isinstance(e, tuple):
                break
        try:
            ws.send_binary(_pk(102, {}, sid))
        except Exception:
            pass
        return _resample(bytes(audio), DOUBAO_RATE, out_rate)
    finally:
        try:
            ws.close()
        except Exception:
            pass
