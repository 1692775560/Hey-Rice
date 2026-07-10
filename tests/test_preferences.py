"""偏好记忆的离线单元测试。

用临时文件替换落盘路径,用 monkeypatch 替换 LLM 调用,全程不触发真实网络:
  - finalize_meal : 一顿饭结束时去重落盘,pending 清空
  - _load/_persist: 写入后能原样读回
  - summary_for_prompt : 有/无偏好时的提示文案
  - observe       : 打桩 chat 后验证抽取、累积与异常安全兜底
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import preferences  # noqa: E402
from preferences import PreferenceMemory  # noqa: E402


class PreferenceMemoryTest(unittest.TestCase):
    def setUp(self):
        # 把落盘路径指向临时目录,避免污染仓库里的 preferences_store.json
        self._orig_store_path = preferences.STORE_PATH
        self._tmpdir = tempfile.mkdtemp(prefix="xiaogua_pref_")
        preferences.STORE_PATH = os.path.join(self._tmpdir, "store.json")
        self._orig_chat = preferences.chat

    def tearDown(self):
        preferences.STORE_PATH = self._orig_store_path
        preferences.chat = self._orig_chat
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_finalize_empty_pending_returns_empty(self):
        mem = PreferenceMemory()
        self.assertEqual(mem.finalize_meal(), [])

    def test_finalize_persists_and_clears_pending(self):
        mem = PreferenceMemory()
        mem.pending = [{"category": "taste", "value": "喜欢清淡"}]
        saved = mem.finalize_meal()
        self.assertEqual(len(saved), 1)
        self.assertEqual(mem.pending, [])
        self.assertTrue(os.path.exists(preferences.STORE_PATH))

    def test_finalize_deduplicates(self):
        mem = PreferenceMemory()
        # 一个已落盘 + pending 里有重复项和一个新项
        mem.saved = [{"category": "taste", "value": "喜欢清淡"}]
        mem.pending = [
            {"category": "taste", "value": "喜欢清淡"},   # 与 saved 重复
            {"category": "texture", "value": "喜欢软的"},  # 新
            {"category": "texture", "value": "喜欢软的"},  # pending 内部重复
        ]
        newly = mem.finalize_meal()
        values = [p["value"] for p in newly]
        self.assertEqual(values, ["喜欢软的"])

    def test_persist_then_load_round_trip(self):
        mem = PreferenceMemory()
        mem.pending = [{"category": "dislike", "value": "不吃香菜"}]
        mem.finalize_meal()
        # 新实例从同一临时文件加载,应读回刚落盘的偏好
        reloaded = PreferenceMemory()
        self.assertTrue(any(p["value"] == "不吃香菜" for p in reloaded.saved))

    def test_summary_empty_when_no_saved(self):
        mem = PreferenceMemory()
        self.assertEqual(mem.summary_for_prompt(), "")

    def test_summary_lists_saved_preferences(self):
        mem = PreferenceMemory()
        mem.saved = [{"category": "temperature", "value": "不要太烫"}]
        summary = mem.summary_for_prompt()
        self.assertIn("temperature", summary)
        self.assertIn("不要太烫", summary)

    def test_observe_extracts_food_preference(self):
        preferences.chat = lambda **kwargs: '{"preferences":[{"category":"taste","value":"喜欢清淡"}]}'
        mem = PreferenceMemory()
        fresh = mem.observe("我口味比较清淡")
        self.assertEqual(len(fresh), 1)
        self.assertEqual(fresh[0]["value"], "喜欢清淡")
        self.assertIn(fresh[0], mem.pending)

    def test_observe_smalltalk_returns_empty(self):
        preferences.chat = lambda **kwargs: '{"preferences":[]}'
        mem = PreferenceMemory()
        self.assertEqual(mem.observe("今天天气不错"), [])
        self.assertEqual(mem.pending, [])

    def test_observe_ignores_entries_without_value(self):
        preferences.chat = lambda **kwargs: '{"preferences":[{"category":"taste"}]}'
        mem = PreferenceMemory()
        self.assertEqual(mem.observe("随便说说"), [])

    def test_observe_survives_llm_error(self):
        def boom(**kwargs):
            raise RuntimeError("network down")

        preferences.chat = boom
        mem = PreferenceMemory()
        self.assertEqual(mem.observe("我喜欢软一点的饭"), [])


if __name__ == "__main__":
    unittest.main()
