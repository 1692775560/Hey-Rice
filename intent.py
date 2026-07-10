"""意图识别 LLM(第一次 LLM 调用)。

职责单一:判断患者这句话是【命令】还是【对话】,如果是命令是哪个。
用 Opus + 强制 JSON 输出 + 温度 0,要准、要稳、可控。
它不负责回话(回话是对话 LLM 的事)。

区分命令必须靠语义理解,不能靠关键词:
  「别停」含"停"却是不要停;「不想吃了」含"吃"却是拒绝。
"""
from __future__ import annotations
import json
import re

import config
from llm import chat
from actions import FeedingState

# 命令白名单。模型只判断"要喂饭 / 不喂了",【不区分】第一次还是继续
# —— 那由代码根据状态(meal_active)决定,见 actions.run_intent。
COMMAND_INTENTS = {"FEED", "STOP_FEED"}
ALL_TYPES = {"COMMAND", "TALK"}

SYSTEM_PROMPT = """\
你是喂饭机器人"小瓜"的意图识别模块。你的唯一任务是判断患者说的这句话属于哪一类,并输出结构化 JSON。你不负责回复患者,也不需要判断这是不是"第一次"喂饭(那由程序根据状态决定)。

# 你要区分两大类
1. COMMAND(命令):患者想让机器人做喂饭相关的动作。
2. TALK(对话):闲聊、提问、情绪表达、与喂饭动作无关的话。

# 命令白名单(只能从这两个里选一个)
- FEED       要喂饭 —— "喂我 / 开始吃 / 来一口 / 继续 / 再来一口 / 接着吃"(不管第一次还是继续,统一都是 FEED)
- STOP_FEED  不喂了 —— "不吃了 / 够了 / 停 / 别喂了 / 吃饱了"

# 必须用语义理解,不能被字面骗
- 「别停」= 不要停,不是 STOP_FEED(通常是想继续 → FEED,或 TALK)
- 「我不想吃了」= 拒绝进食 = STOP_FEED
- 「这个菜是什么」「今天天气真好」= TALK,不是命令
- 拿不准 / 信息不全 / 有歧义 → type=TALK 且 intent=UNCLEAR,让机器人去追问,绝不猜一个命令。

# 输出格式(严格 JSON,不要多余文字)
{
  "type": "COMMAND" 或 "TALK",
  "intent": "FEED" / "STOP_FEED" / "UNCLEAR",
  "confidence": 0.0~1.0,
  "reason": "一句话中文说明你的判断依据(尤其是识别到的否定/歧义)"
}
- type=TALK 时 intent 一律填 "UNCLEAR"。
- 只有 type=COMMAND 时 intent 才是 FEED 或 STOP_FEED。
"""


def _safe_default(reason: str) -> dict:
    """兜底:任何异常都落到安全的'当对话去追问',绝不误触发命令。"""
    return {"type": "TALK", "intent": "UNCLEAR", "confidence": 0.0, "reason": reason}


def extract_json(text: str) -> dict | None:
    """从模型输出里健壮地抠出 JSON,兼容多种格式:
      1) 纯 JSON
      2) markdown 代码块 ```json ... ``` 或 ``` ... ```
      3) 夹在文字里的 JSON(取第一个 {...} 平衡括号块)
    抠不出返回 None。
    """
    if not text:
        return None
    s = text.strip()

    # 1) 直接尝试
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    # 2) 剥 markdown 代码块围栏 ```json ... ``` / ``` ... ```
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.DOTALL | re.IGNORECASE)
    if fence:
        inner = fence.group(1).strip()
        try:
            return json.loads(inner)
        except json.JSONDecodeError:
            s = inner  # 继续往下用括号法兜底

    # 3) 从第一个 { 开始做平衡括号扫描,取出第一个完整对象
    start = s.find("{")
    if start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(s)):
            c = s[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(s[start:i + 1])
                        except json.JSONDecodeError:
                            break
    return None


def _contain(raw: dict) -> dict:
    """收容校验:把模型输出钳到白名单内,模型犯错也安全。"""
    t = raw.get("type")
    intent = raw.get("intent", "UNCLEAR")
    conf = raw.get("confidence", 0.0)
    reason = raw.get("reason", "")

    if t not in ALL_TYPES:
        return _safe_default(f"type 非法({t}),兜底为对话")

    if t == "COMMAND":
        # 命令必须在白名单里,否则降级为对话追问
        if intent not in COMMAND_INTENTS:
            return _safe_default(f"命令意图不在白名单({intent}),兜底为对话")
    else:  # TALK
        intent = "UNCLEAR"

    try:
        conf = float(conf)
    except (TypeError, ValueError):
        conf = 0.0

    return {"type": t, "intent": intent, "confidence": conf, "reason": reason}


def recognize(utterance: str, state: FeedingState) -> dict:
    """识别一句话的意图。返回收容后的 dict。

    出参:{type, intent, confidence, reason}
    """
    user = (
        f"【当前喂饭状态】{state.describe()}\n"
        f"【患者说的话】{utterance}"
    )
    try:
        text = chat(
            system=SYSTEM_PROMPT,
            user=user,
            model=config.INTENT_MODEL,
            temperature=config.INTENT_TEMPERATURE,
            force_json=True,
        )
    except Exception as e:  # noqa: BLE001 - 任何调用失败都要安全兜底
        return _safe_default(f"意图识别调用失败,兜底为对话:{e}")

    raw = extract_json(text)
    if raw is None:
        return _safe_default(f"意图识别无法解析出 JSON,兜底为对话:{text[:80]}")

    return _contain(raw)
