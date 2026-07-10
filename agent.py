"""小瓜主循环。

流程:
  患者说话
    → ① 意图识别 LLM(强制 JSON,温度 0)判断 命令 / 对话
    → 如果是【命令】: 调用对应机械臂动作(不调对话 LLM)
    → 如果是【对话】: 调用【小瓜人设】对话 LLM 温柔回话

两个 LLM 分别调用:意图识别只做判断,对话只做回话,职责不混。

用法:
  export MEALMATE_API_KEY=你的密钥
  python agent.py                 # 交互模式,逐句输入
  python agent.py "喂我吃饭吧"      # 单句模式
"""
from __future__ import annotations
import sys

import config
from actions import FeedingState, run_intent
from intent import recognize
from chat_agent import reply
from preferences import PreferenceMemory


# 实际动作 → 给患者的确认话术(固定模板,不交给自由对话 LLM)
COMMAND_FEEDBACK = {
    "FEED_FIRST": "好嘞,我先取一勺,慢慢喂给你,准备好了哦~",
    "FEED_CONTINUE": "好,那我们接着来,慢慢吃~",
    "STOP_FEED": "好的,那我们先停下,你辛苦啦。",
}


def handle(utterance: str, state: FeedingState, pref: PreferenceMemory) -> None:
    """处理患者一句话。"""
    print(f"\n患者: {utterance}")

    # ① 意图识别(第一个 LLM,只判 FEED/STOP_FEED/对话)
    result = recognize(utterance, state)
    print(f"  [意图识别] type={result['type']} intent={result['intent']} "
          f"conf={result['confidence']:.2f} · {result['reason']}")

    if result["type"] == "COMMAND":
        # ②-A 命令 → 代码按状态决定 FIRST/CONTINUE 并执行(不调对话 LLM)
        outcome = run_intent(result["intent"], state)
        resolved = outcome.get("resolved", result["intent"])
        print(f"小瓜: {COMMAND_FEEDBACK.get(resolved, '好的。')}")
        print(f"  [解析动作] {resolved}  [结果] {outcome}")
        print(f"  [喂饭状态] {state.describe()}")
        if outcome.get("meal_ended"):
            saved = pref.finalize_meal()
            if saved:
                print(f"  [偏好落盘] 本顿学到 {len(saved)} 条:{[p['value'] for p in saved]}")
    else:
        # ②-B 对话 → 小瓜回话(带已知偏好)+ 顺带抽取新偏好
        said = reply(utterance, pref.summary_for_prompt())
        print(f"小瓜: {said}")
        learned = pref.observe(utterance)
        if learned:
            print(f"  [学到偏好] {[p['value'] for p in learned]}")


def main() -> None:
    config.assert_configured()
    state = FeedingState()
    pref = PreferenceMemory()

    print("=" * 48)
    print("  小瓜 · 喂饭陪伴 Agent")
    print(f"  意图模型: {config.INTENT_MODEL}   对话模型: {config.CHAT_MODEL}")
    print("=" * 48)

    # 单句模式
    if len(sys.argv) > 1:
        handle(" ".join(sys.argv[1:]), state, pref)
        return

    # 交互模式
    print("直接输入患者说的话(输入 q 退出)。")
    while True:
        try:
            line = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见~")
            break
        if line.lower() in {"q", "quit", "exit"}:
            print("再见~")
            break
        if not line:
            continue
        handle(line, state, pref)


if __name__ == "__main__":
    main()
