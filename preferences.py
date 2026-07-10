"""偏好记忆。

从对话(TALK)里学习患者的吃饭偏好,一顿饭结束时沉淀落盘。
命令(COMMAND)不记录偏好——它只是执行动作,不表达偏好。

设计要点(用户决策):
  - 一顿饭结束(STOP_FEED)= 这次对话结束 → 把本顿累积的偏好落盘。
  - 只从 TALK 里抽【与吃饭相关】的偏好(口味、软硬、节奏、喜恶、温度…),闲聊不记。
  - 抽取用 LLM(语义),不靠关键词。
"""
from __future__ import annotations
import json
import os
import time

import config
from llm import chat

STORE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "preferences_store.json")

# 偏好抽取用的 system prompt:只抽吃饭相关偏好,没有就返回空
EXTRACT_PROMPT = """\
你是喂饭机器人"小瓜"的偏好提取模块。从患者这句话里,提取【与吃饭相关】的个人偏好,输出 JSON。

只提取这些类别的偏好(和吃饭有关才算):
- taste     口味(咸淡、甜、清淡、重口…)
- texture   软硬/质地(喜欢软的、不要太干、糊状…)
- temperature 温度(不要太烫、喜欢温的…)
- pace      节奏(喜欢慢一点、快一点…)
- like      喜欢的食物
- dislike   不喜欢/不吃的食物
- amount    份量(一口小一点、别太多…)

# 规则
- 只有明确表达了吃饭偏好才提取。纯闲聊(天气、家人、心情)→ 返回空列表。
- 用语义理解,不要被字面骗:「这个不会太烫吧」是【提问】不是偏好;「我不喜欢太烫的」才是偏好。

# 输出格式(严格 JSON)
{
  "preferences": [
    { "category": "上面7类之一", "value": "简短描述这个偏好" }
  ]
}
没有可提取的偏好时:{ "preferences": [] }
"""


class PreferenceMemory:
    def __init__(self, store_path: str | None = None):
        # 落盘路径可注入:多会话时每个会话各用一份文件;默认沿用模块级 STORE_PATH。
        self.store_path: str = store_path or STORE_PATH
        # 本顿饭累积的偏好(结束时落盘)
        self.pending: list[dict] = []
        # 已落盘的历史偏好(启动时加载)
        self.saved: list[dict] = self._load()

    # ---- 从一句 TALK 里抽偏好 ----
    def observe(self, utterance: str) -> list[dict]:
        """从对话里抽取吃饭偏好,累积到本顿。返回本次新抽到的偏好(可能为空)。"""
        try:
            text = chat(
                system=EXTRACT_PROMPT,
                user=utterance,
                model=config.PREF_MODEL,   # 结构化小任务,用快模型
                temperature=0,
                force_json=True,
            )
            from intent import extract_json
            data = extract_json(text) or {}
            prefs = data.get("preferences", [])
        except Exception:  # noqa: BLE001 - 抽取失败不影响主流程
            return []

        fresh = [p for p in prefs if isinstance(p, dict) and p.get("value")]
        self.pending.extend(fresh)
        return fresh

    # ---- 一顿饭结束时落盘 ----
    def finalize_meal(self) -> list[dict]:
        """把本顿累积的偏好去重后落盘。返回本次落盘的偏好。"""
        if not self.pending:
            return []
        # 去重(按 category+value)
        seen = {(p["category"], p["value"]) for p in self.saved}
        newly = []
        for p in self.pending:
            key = (p.get("category"), p.get("value"))
            if key not in seen:
                seen.add(key)
                record = {**p, "ts": int(time.time())}
                self.saved.append(record)
                newly.append(record)
        self.pending = []
        self._persist()
        return newly

    # ---- 遗忘已学到的偏好(reset 按需调用)----
    def clear_saved(self) -> None:
        """清空已落盘的历史偏好(内存 + 文件),并丢弃本顿未落盘的累积。

        用于患者/护理者主动要求"忘掉之前记录的吃饭偏好"的场景。
        """
        self.saved = []
        self.pending = []
        self._persist()

    # ---- 给对话 LLM 的偏好摘要(让小瓜记得患者喜好)----
    def summary_for_prompt(self) -> str:
        if not self.saved:
            return ""
        lines = [f"- [{p['category']}] {p['value']}" for p in self.saved[-12:]]
        return "已知患者的吃饭偏好(供参考,自然体现,别生硬复述):\n" + "\n".join(lines)

    # ---- 持久化 ----
    def _load(self) -> list[dict]:
        try:
            with open(self.store_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _persist(self) -> None:
        try:
            parent = os.path.dirname(self.store_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self.store_path, "w", encoding="utf-8") as f:
                json.dump(self.saved, f, ensure_ascii=False, indent=2)
        except OSError:
            pass
