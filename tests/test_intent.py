"""意图识别的离线单元测试。

覆盖三块纯逻辑与一块打桩逻辑,全程不触发真实网络:
  - extract_json  : 从多种格式里健壮抠 JSON
  - _contain      : 收容校验,把模型输出钳进白名单
  - _local_fast_path : 常见演示话术的本地秒回规则
  - recognize     : 本地快路径直接返回;走 LLM 的分支用 monkeypatch 打桩,
                    验证异常/非 JSON 一律安全兜底为对话,绝不误触发命令。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import intent  # noqa: E402
from intent import (  # noqa: E402
    _contain,
    _local_fast_path,
    extract_json,
    recognize,
)
from actions import FeedingState  # noqa: E402


class ExtractJsonTest(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_markdown_fenced_json(self):
        text = "```json\n{\"type\": \"TALK\"}\n```"
        self.assertEqual(extract_json(text), {"type": "TALK"})

    def test_bare_fenced_block(self):
        text = "```\n{\"x\": true}\n```"
        self.assertEqual(extract_json(text), {"x": True})

    def test_json_embedded_in_prose(self):
        text = '好的,这是判断结果:{"intent": "FEED"} 仅供参考'
        self.assertEqual(extract_json(text), {"intent": "FEED"})

    def test_invalid_returns_none(self):
        self.assertIsNone(extract_json("这里根本没有 json"))

    def test_empty_returns_none(self):
        self.assertIsNone(extract_json(""))


class ContainTest(unittest.TestCase):
    def test_valid_feed_command(self):
        out = _contain({"type": "COMMAND", "intent": "FEED", "confidence": 0.9, "reason": "r"})
        self.assertEqual(out["type"], "COMMAND")
        self.assertEqual(out["intent"], "FEED")
        self.assertAlmostEqual(out["confidence"], 0.9)

    def test_valid_stop_command(self):
        out = _contain({"type": "COMMAND", "intent": "STOP_FEED", "confidence": 1})
        self.assertEqual(out["intent"], "STOP_FEED")

    def test_illegal_type_falls_back_to_talk(self):
        out = _contain({"type": "ACTION", "intent": "FEED"})
        self.assertEqual(out["type"], "TALK")
        self.assertEqual(out["intent"], "UNCLEAR")

    def test_command_with_illegal_intent_falls_back(self):
        out = _contain({"type": "COMMAND", "intent": "DANCE"})
        self.assertEqual(out["type"], "TALK")
        self.assertEqual(out["intent"], "UNCLEAR")

    def test_talk_forces_unclear_intent(self):
        out = _contain({"type": "TALK", "intent": "FEED"})
        self.assertEqual(out["type"], "TALK")
        self.assertEqual(out["intent"], "UNCLEAR")

    def test_bad_confidence_defaults_to_zero(self):
        out = _contain({"type": "COMMAND", "intent": "FEED", "confidence": "not-a-number"})
        self.assertEqual(out["confidence"], 0.0)


class LocalFastPathTest(unittest.TestCase):
    def test_feed_request_recognized(self):
        out = _local_fast_path("喂我吃饭吧", FeedingState())
        self.assertIsNotNone(out)
        self.assertEqual(out["intent"], "FEED")

    def test_stop_request_recognized(self):
        out = _local_fast_path("不吃了,够了", FeedingState())
        self.assertIsNotNone(out)
        self.assertEqual(out["intent"], "STOP_FEED")

    def test_dont_stop_maps_to_feed(self):
        out = _local_fast_path("别停", FeedingState())
        self.assertIsNotNone(out)
        self.assertEqual(out["intent"], "FEED")

    def test_bare_continue_needs_active_meal(self):
        # 没有进行中的饭时,单说"继续"不该本地触发命令(交给上层/LLM 追问)
        self.assertIsNone(_local_fast_path("继续", FeedingState()))
        # 进行中时,"继续"识别为继续喂饭
        active = FeedingState(meal_active=True)
        out = _local_fast_path("继续", active)
        self.assertIsNotNone(out)
        self.assertEqual(out["intent"], "FEED")

    def test_smalltalk_not_a_command(self):
        self.assertIsNone(_local_fast_path("今天天气真好啊", FeedingState()))

    def test_empty_returns_none(self):
        self.assertIsNone(_local_fast_path("   ", FeedingState()))


class RecognizeTest(unittest.TestCase):
    """recognize 的本地快路径与 LLM 兜底,均不触发真实网络。"""

    def test_fast_path_returns_without_network(self):
        out = recognize("喂我吃饭吧", FeedingState())
        self.assertEqual(out["type"], "COMMAND")
        self.assertEqual(out["intent"], "FEED")

    def test_llm_valid_json_is_contained(self):
        original = intent.chat
        intent.chat = lambda **kwargs: '{"type":"COMMAND","intent":"STOP_FEED","confidence":0.8,"reason":"ok"}'
        try:
            out = recognize("我打算歇一会儿了", FeedingState())
        finally:
            intent.chat = original
        self.assertEqual(out["type"], "COMMAND")
        self.assertEqual(out["intent"], "STOP_FEED")

    def test_llm_exception_falls_back_to_talk(self):
        original = intent.chat

        def boom(**kwargs):
            raise RuntimeError("network down")

        intent.chat = boom
        try:
            out = recognize("这道菜是什么食材做的呢", FeedingState())
        finally:
            intent.chat = original
        self.assertEqual(out["type"], "TALK")
        self.assertEqual(out["intent"], "UNCLEAR")

    def test_llm_non_json_falls_back_to_talk(self):
        original = intent.chat
        intent.chat = lambda **kwargs: "抱歉我不太确定"
        try:
            out = recognize("这道菜是什么食材做的呢", FeedingState())
        finally:
            intent.chat = original
        self.assertEqual(out["type"], "TALK")
        self.assertEqual(out["intent"], "UNCLEAR")


if __name__ == "__main__":
    unittest.main()
