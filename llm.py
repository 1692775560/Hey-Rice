"""底层 LLM 调用(OpenAI 兼容格式,走中转 API)。

意图识别 LLM 和对话 LLM 都用这个客户端,只是传不同的 model / system / 参数。

实现说明:优先用系统 curl 发请求。
原因:不同机器上 curl / urllib 的 TLS 表现并不完全一致。
这里会先试 curl; 若失败,自动回退到 urllib(带 certifi 证书),
尽量把偶发的 SSL 握手问题收掉。
"""
from __future__ import annotations
import json
import shutil
import subprocess
import time

import config


def _build_payload(system: str, user: str, model: str, temperature: float,
                   force_json: bool) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    # 注意:claude-opus-4-8 等新模型不再接受 temperature 参数(会报 deprecated)。
    # 仅当显式配置 MEALMATE_SEND_TEMPERATURE=1 时才发送,默认不发。
    if config.SEND_TEMPERATURE:
        payload["temperature"] = temperature
    if force_json:
        payload["response_format"] = {"type": "json_object"}
    return payload


def _via_curl(payload: dict) -> dict:
    """用系统 curl 发请求,返回解析后的 JSON body。"""
    body = json.dumps(payload)
    cmd = [
        "curl", "-sS", "--fail-with-body",
        "-X", "POST", f"{config.API_BASE}/chat/completions",
        "-H", "Content-Type: application/json",
        "-H", f"Authorization: Bearer {config.API_KEY}",
        "--max-time", str(int(config.REQUEST_TIMEOUT)),
        "-d", body,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"LLM 调用失败(curl 退出码 {proc.returncode}): "
            f"{(proc.stdout or proc.stderr or '').strip()[:300]}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"LLM 返回非 JSON: {proc.stdout[:300]}") from e


def _via_urllib(payload: dict) -> dict:
    """回退实现:urllib + certifi 证书。"""
    import ssl
    import urllib.request
    import urllib.error
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        ctx = ssl.create_default_context()

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url=f"{config.API_BASE}/chat/completions",
        data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.API_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=config.REQUEST_TIMEOUT, context=ctx) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"LLM 调用失败 HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"LLM 网络错误: {e.reason}") from e


_HAS_CURL = shutil.which("curl") is not None


def chat(
    system: str,
    user: str,
    *,
    model: str,
    temperature: float,
    force_json: bool = False,
) -> str:
    """调一次 chat completion,返回模型输出的文本(assistant content)。

    force_json=True 时要求模型只吐 JSON 对象(用于意图识别)。
    """
    config.assert_configured()
    payload = _build_payload(system, user, model, temperature, force_json)

    last_error = None
    attempts = max(1, config.LLM_RETRIES + 1)

    for attempt in range(1, attempts + 1):
        try:
            if _HAS_CURL:
                try:
                    body = _via_curl(payload)
                except Exception as curl_error:  # noqa: BLE001
                    try:
                        body = _via_urllib(payload)
                    except Exception as urllib_error:  # noqa: BLE001
                        raise RuntimeError(
                            f"{curl_error}; 回退 urllib 也失败: {urllib_error}"
                        ) from urllib_error
            else:
                body = _via_urllib(payload)
            break
        except Exception as e:  # noqa: BLE001
            last_error = e
            if attempt == attempts:
                raise RuntimeError(str(last_error)) from last_error
            time.sleep(0.6 * attempt)

    try:
        return body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"LLM 返回格式异常: {json.dumps(body, ensure_ascii=False)[:300]}") from e
