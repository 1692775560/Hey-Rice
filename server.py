"""小瓜后端 HTTP 服务。

把 agent 逻辑包成 web 服务,前端页面调用它。
密钥留在后端,前端永远拿不到。

提供:
  GET  /            → 前端聊天页面(index.html)
  POST /api/chat    → {"text": "..."} → 意图识别 + 命令调动作/对话回话
  POST /api/reset   → 重置喂饭状态

用法:
  export MEALMATE_API_KEY=你的密钥
  python server.py            # 默认 http://127.0.0.1:8000
"""
from __future__ import annotations
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config
from actions import FeedingState, run_intent
from intent import recognize
from chat_agent import reply
from preferences import PreferenceMemory

HERE = os.path.dirname(os.path.abspath(__file__))
HOST = os.environ.get("MEALMATE_HOST", "127.0.0.1")
PORT = int(os.environ.get("MEALMATE_PORT", "8000"))

# 命令确认话术(固定模板,不交给自由对话 LLM)。key 是代码解析出的实际动作。
COMMAND_FEEDBACK = {
    "FEED_FIRST": "好嘞,我先取一勺,慢慢喂给你,准备好了哦~",
    "FEED_CONTINUE": "好,那我们接着来,慢慢吃~",
    "STOP_FEED": "好的,那我们先停下,你辛苦啦。",
}

# 单会话的喂饭状态(演示用;多用户可按 session 扩展)
STATE = FeedingState()
# 偏好记忆:对话(TALK)里学到的患者偏好,一顿饭结束时沉淀落盘
PREF = PreferenceMemory()


def process(text: str) -> dict:
    """处理一句话,返回给前端的结构化结果。"""
    result = recognize(text, STATE)
    resp = {
        "type": result["type"],
        "intent": result["intent"],
        "confidence": result["confidence"],
        "reason": result["reason"],
        "feedingState": STATE.describe(),
    }

    if result["type"] == "COMMAND":
        # 代码根据状态决定 FIRST/CONTINUE(不靠 LLM),LLM 只给了 FEED/STOP_FEED
        outcome = run_intent(result["intent"], STATE)
        resolved = outcome.get("resolved", result["intent"])
        resp["intent"] = resolved   # 回传实际执行的动作(FEED_FIRST/FEED_CONTINUE/STOP_FEED)
        resp["reply"] = COMMAND_FEEDBACK.get(resolved, "好的。")
        resp["action"] = outcome
        resp["feedingState"] = STATE.describe()
        # 命令不记录偏好;但如果这顿饭刚结束,沉淀本顿累积的偏好
        if outcome.get("meal_ended"):
            saved = PREF.finalize_meal()
            resp["prefSaved"] = saved
    else:
        # 对话:小瓜回话立刻返回;偏好抽取放后台线程,不挡着用户等待。
        resp["reply"] = reply(text, PREF.summary_for_prompt())
        resp["action"] = None
        threading.Thread(target=PREF.observe, args=(text,), daemon=True).start()

    return resp


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, content_type):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self.send_error(404, "Not Found")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send_file(os.path.join(HERE, "index.html"), "text/html; charset=utf-8")
        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send_json({"error": "invalid json"}, 400)
            return

        if self.path == "/api/chat":
            text = (data.get("text") or "").strip()
            if not text:
                self._send_json({"error": "empty text"}, 400)
                return
            if not config.API_KEY:
                self._send_json({
                    "type": "TALK", "intent": "UNCLEAR", "confidence": 0.0,
                    "reason": "未配置密钥",
                    "reply": "(还没配置 API 密钥哦~ 请在终端运行:export MEALMATE_API_KEY=你的密钥,再重启服务)",
                    "action": None, "feedingState": STATE.describe(),
                })
                return
            try:
                self._send_json(process(text))
            except Exception as e:  # noqa: BLE001
                self._send_json({"error": str(e)}, 500)
        elif self.path == "/api/reset":
            STATE.food_acquired = False
            STATE.feeding = False
            self._send_json({"ok": True, "feedingState": STATE.describe()})
        else:
            self.send_error(404, "Not Found")

    def log_message(self, fmt, *args):
        # 精简日志
        print("  ", fmt % args)


def main():
    # 不在启动时硬性要求 key —— 先让页面能打开;真正发消息时再检查(缺 key 会返回清晰提示)。
    print("=" * 52)
    print("  小瓜 · 喂饭陪伴 Agent · Web 服务")
    print(f"  意图模型: {config.INTENT_MODEL}   对话模型: {config.CHAT_MODEL}")
    if not config.API_KEY:
        print("  ⚠️  尚未设置 MEALMATE_API_KEY —— 页面可打开,但发消息会提示配置密钥。")
        print("      设置方法:  export MEALMATE_API_KEY=你的密钥")
    print(f"  打开浏览器访问:  http://{HOST}:{PORT}")
    print("=" * 52)
    try:
        ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
