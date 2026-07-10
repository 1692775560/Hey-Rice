"""对话 LLM(第二次 LLM 调用)。

只有当意图识别判定为 TALK(对话)时才调用它。
它带【小瓜人设】——温柔家人感,负责自然、温暖地回话。
它不产生任何命令、不控制机械臂,只说话。
"""
from __future__ import annotations
import re

import config
from llm import chat

# 小瓜人设(温柔家人感)。这是"怎么说话"那一层,和"做什么"完全分开。
XIAOGUA_PERSONA = """\
你是"小瓜",一个陪帕金森患者吃饭的陪伴机器人。你现在只负责【聊天回应】,不控制任何动作。

# 你的性格:温柔家人感
- 像一个有耐心的晚辈家人:稳、暖、不催促。
- 多用"我们""慢慢来""不着急";先关心,再回应。
- 说话短、白话、放慢节奏。不用书面语,不长篇大论。

# 你面对的人
帕金森患者:手可能会抖、动作慢、容易累、说话可能慢而含糊。请格外耐心、给足空间。

# 三条铁律(任何时候都守住)
1. 不替患者做决定:吃什么、要不要吃。
2. 不假装:不知道就说不知道。医疗/用药/病情的问题(比如"这药能不能停")你【不回答】,
   温和地说这个要问医生,你可以帮忙记下来提醒护理者。
3. 安全永远第一:如果患者像是不舒服/呛到,别闲聊,提醒他慢点、需要就叫人。

# 能力边界
- 主业是喂饭陪伴。患者聊天气、家常、回忆 → 温和简短回应,然后可以轻轻问一句要不要继续吃。
- 不承诺你做不到的事。

# 回复要求
- 只输出你要对患者说的那句话本身,中文,口语,简短(通常一两句)。
- 不要输出任何 JSON、动作指令、括号说明。
"""

_GREETING_RE = re.compile(r"^(你好|您好|嗨|哈喽|在吗|早上好|中午好|晚上好)[呀啊哦呢嘛吗~！!，。 ]*$")
_THANKS_RE = re.compile(r"(谢谢|辛苦了|麻烦你了)")
_HUNGRY_RE = re.compile(r"(我饿了|饿了|有点饿)")


def _quick_reply(utterance: str) -> str | None:
    """对最常见的寒暄和演示话术直接本地回复,避免每次都走远端。"""
    text = utterance.strip()
    if not text:
        return None
    if _GREETING_RE.match(text):
        return "你好呀,我是小瓜。想吃饭了就跟我说,我们慢慢来。"
    if _THANKS_RE.search(text):
        return "不客气呀,我陪着你,我们慢慢来。"
    if _HUNGRY_RE.search(text):
        return "饿了好呀,那我们就慢慢开始吃,不着急。"
    return None


def reply(utterance: str, recent_context: str = "") -> str:
    """用小瓜人设生成一句温柔回话。

    utterance: 患者说的话
    recent_context: 可选,最近几轮对话摘要(先留接口,主循环可暂不传)
    """
    fast = _quick_reply(utterance)
    if fast is not None:
        return fast

    user = utterance if not recent_context else f"{recent_context}\n患者刚说:{utterance}"
    try:
        text = chat(
            system=XIAOGUA_PERSONA,
            user=user,
            model=config.CHAT_MODEL,
            temperature=config.CHAT_TEMPERATURE,
            force_json=False,
        )
        return text.strip()
    except Exception as e:  # noqa: BLE001 - 对话失败也要有温和兜底
        print(f"[chat_agent] 对话服务异常: {e}")
        return "我这会儿网络有点慢,你再说一遍,我马上听着。"
