"""小瓜后端 HTTP 服务。

把 agent 逻辑包成 web 服务,前端页面调用它。
密钥留在后端,前端永远拿不到。

提供:
  GET  /            → 前端聊天页面(index.html)
  GET  /api/health  → 健康检查(服务状态、模型、是否已配置密钥、会话数)
  POST /api/chat    → {"text": "...", "session"?: "..."} → 意图识别 + 命令调动作/对话回话
  POST /api/reset   → {"session"?: "...", "clearPreferences"?: bool} → 重置喂饭状态(可选清偏好)

多会话隔离:每个浏览器/患者按 session id 各自维护喂饭状态与偏好记忆,互不串味。
  session id 来源优先级:请求体 session > Cookie(mm_session)> 自动新建并回种 Cookie。
  默认会话(session=default)沿用原来的 preferences_store.json,兼容旧的单会话行为。

用法:
  export MEALMATE_API_KEY=你的密钥
  python server.py            # 默认 http://127.0.0.1:8000
"""
from __future__ import annotations
import json
import os
import re
import threading
import time
import uuid
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config
from actions import FeedingState, run_intent
from intent import recognize
from chat_agent import reply
import preferences
from preferences import PreferenceMemory

HERE = os.path.dirname(os.path.abspath(__file__))
HOST = os.environ.get("MEALMATE_HOST", "127.0.0.1")
PORT = int(os.environ.get("MEALMATE_PORT", "8000"))

# 多会话:cookie 名、默认会话 id、各会话偏好文件目录、服务启动时刻。
SESSION_COOKIE = "mm_session"
DEFAULT_SESSION_ID = "default"
SESSION_DIR = os.path.join(HERE, "data", "sessions")
START_TIME = time.time()

# 命令确认话术(固定模板,不交给自由对话 LLM)。key 是代码解析出的实际动作。
COMMAND_FEEDBACK = {
    "FEED_FIRST": "好嘞,我先取一勺,慢慢喂给你,准备好了哦~",
    "FEED_CONTINUE": "好,那我们接着来,慢慢吃~",
    "STOP_FEED": "好的,那我们先停下,你辛苦啦。",
}


def _safe_session_stem(session_id: str) -> str:
    """把外部传入的 session id 收敛成安全的文件名主干,防止路径穿越。

    只保留字母/数字/下划线/连字符并截断长度;清空后回退到 "anon"。
    """
    stem = re.sub(r"[^A-Za-z0-9_-]", "", session_id or "")[:64]
    return stem or "anon"


def _session_store_path(session_id: str) -> str:
    """会话偏好落盘路径。默认会话沿用旧文件,其它会话各自一份,互不覆盖。"""
    if session_id == DEFAULT_SESSION_ID:
        return preferences.STORE_PATH
    return os.path.join(SESSION_DIR, f"{_safe_session_stem(session_id)}.json")


class Session:
    """单个会话的状态:喂饭状态机 + 偏好记忆(各自独立落盘)。"""

    def __init__(self, session_id: str):
        self.id = session_id
        self.state = FeedingState()
        self.pref = PreferenceMemory(store_path=_session_store_path(session_id))


# session id -> Session;并发下用锁保护字典本身的读写。
_SESSIONS: dict[str, Session] = {}
_SESSIONS_LOCK = threading.Lock()


def get_session(session_id: str) -> Session:
    """按 id 取会话,不存在则新建(线程安全)。"""
    with _SESSIONS_LOCK:
        sess = _SESSIONS.get(session_id)
        if sess is None:
            sess = Session(session_id)
            _SESSIONS[session_id] = sess
        return sess


def _resolve_session(cookie_header: str, data: dict) -> tuple[str, bool]:
    """决定本次请求用哪个 session id,并返回是否需要回种 Cookie。

    优先级:请求体 session > Cookie(mm_session)> 自动新建(需回种 Cookie)。
    """
    explicit = ""
    if isinstance(data, dict):
        explicit = (data.get("session") or "").strip()
    if explicit:
        return explicit, False

    if cookie_header:
        jar = SimpleCookie()
        try:
            jar.load(cookie_header)
        except Exception:  # noqa: BLE001 - 非法 Cookie 头忽略,走自动新建
            jar = None
        if jar and SESSION_COOKIE in jar:
            val = (jar[SESSION_COOKIE].value or "").strip()
            if val:
                return val, False

    return uuid.uuid4().hex, True


def _health_payload() -> dict:
    """健康检查返回体。只暴露服务元信息,绝不包含任何密钥值。"""
    with _SESSIONS_LOCK:
        session_count = len(_SESSIONS)
    return {
        "ok": True,
        "service": "mealmate",
        "intentModel": config.INTENT_MODEL,
        "chatModel": config.CHAT_MODEL,
        "prefModel": config.PREF_MODEL,
        "apiKeyConfigured": bool(config.API_KEY),
        "sessions": session_count,
        "uptimeSeconds": round(time.time() - START_TIME, 1),
    }


def process(text: str, session: Session) -> dict:
    """处理一句话,在指定会话上下文中返回给前端的结构化结果。"""
    state = session.state
    pref = session.pref
    result = recognize(text, state)
    resp = {
        "type": result["type"],
        "intent": result["intent"],
        "confidence": result["confidence"],
        "reason": result["reason"],
        "feedingState": state.describe(),
    }

    if result["type"] == "COMMAND":
        # 代码根据状态决定 FIRST/CONTINUE(不靠 LLM),LLM 只给了 FEED/STOP_FEED
        outcome = run_intent(result["intent"], state)
        resolved = outcome.get("resolved", result["intent"])
        resp["intent"] = resolved   # 回传实际执行的动作(FEED_FIRST/FEED_CONTINUE/STOP_FEED)
        resp["reply"] = COMMAND_FEEDBACK.get(resolved, "好的。")
        resp["action"] = outcome
        resp["feedingState"] = state.describe()
        # 命令不记录偏好;但如果这顿饭刚结束,沉淀本顿累积的偏好
        if outcome.get("meal_ended"):
            saved = pref.finalize_meal()
            resp["prefSaved"] = saved
    else:
        # 对话:小瓜回话立刻返回;偏好抽取放后台线程,不挡着用户等待。
        resp["reply"] = reply(text, pref.summary_for_prompt())
        resp["action"] = None
        threading.Thread(target=pref.observe, args=(text,), daemon=True).start()

    return resp


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, obj, code=200, extra_headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or []):
            self.send_header(key, value)
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

    def _session_cookie_header(self, session_id):
        """构造 Set-Cookie 头。

        HttpOnly:前端不需要读取它,fetch 同源会自动带上;
        SameSite=Lax:避免跨站请求误带;Max-Age 一年,让会话在同一浏览器里保持。
        """
        return (
            "Set-Cookie",
            f"{SESSION_COOKIE}={session_id}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax",
        )

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send_file(os.path.join(HERE, "index.html"), "text/html; charset=utf-8")
        elif self.path == "/api/health":
            self._send_json(_health_payload())
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
        if not isinstance(data, dict):
            self._send_json({"error": "invalid json"}, 400)
            return

        # 解析会话:决定用哪个 session id,以及是否需要回种 Cookie。
        session_id, set_cookie = _resolve_session(self.headers.get("Cookie", ""), data)
        cookie_headers = [self._session_cookie_header(session_id)] if set_cookie else None
        session = get_session(session_id)

        if self.path == "/api/chat":
            text = (data.get("text") or "").strip()
            if not text:
                self._send_json({"error": "empty text"}, 400, cookie_headers)
                return
            if not config.API_KEY:
                self._send_json({
                    "type": "TALK", "intent": "UNCLEAR", "confidence": 0.0,
                    "reason": "未配置密钥",
                    "reply": "(还没配置 API 密钥哦~ 请在终端运行:export MEALMATE_API_KEY=你的密钥,再重启服务)",
                    "action": None, "feedingState": session.state.describe(),
                }, 200, cookie_headers)
                return
            try:
                self._send_json(process(text, session), 200, cookie_headers)
            except Exception as e:  # noqa: BLE001
                self._send_json({"error": str(e)}, 500, cookie_headers)
        elif self.path == "/api/reset":
            # 重置这顿饭的完整状态:meal_active 决定 FIRST/CONTINUE,必须一并归零,
            # 否则下次喂饭不会从"取餐"开始。food_acquired 是动作层内部标记,同步清空。
            session.state.meal_active = False
            session.state.food_acquired = False
            resp = {"ok": True, "feedingState": session.state.describe()}
            # 可选:一并遗忘该会话已学到的偏好(默认不清,保留旧的 reset 行为)。
            if data.get("clearPreferences"):
                session.pref.clear_saved()
                resp["preferencesCleared"] = True
            self._send_json(resp, 200, cookie_headers)
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
