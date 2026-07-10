"""动作层与喂饭状态机的离线单元测试。

只覆盖纯逻辑(状态流转、动作分发),不发起任何网络请求:
机械臂调用当前是 mock(打印后返回固定形状),天然离线可测。

重点保护的行为:
  - FEED 第一次(meal_active=False)-> FEED_FIRST(取餐 -> 送餐)
  - FEED 进行中(meal_active=True) -> FEED_CONTINUE(只送餐)
  - STOP_FEED -> 结束这顿饭,并正确回报 meal_ended
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from actions import (  # noqa: E402
    FeedingState,
    acquire_food,
    deliver_food,
    run_intent,
)


class FeedingStateTest(unittest.TestCase):
    """FeedingState 字段默认值与描述文案。"""

    def test_default_state_is_not_active(self):
        state = FeedingState()
        self.assertFalse(state.meal_active)
        self.assertFalse(state.food_acquired)

    def test_describe_reflects_meal_active(self):
        state = FeedingState()
        self.assertIn("还没开始", state.describe())
        state.meal_active = True
        self.assertIn("进行中", state.describe())


class ArmActionTest(unittest.TestCase):
    """取餐/送餐对 food_acquired 标记的影响。"""

    def test_acquire_sets_food_acquired(self):
        state = FeedingState()
        result = acquire_food(state)
        self.assertTrue(result["ok"])
        self.assertTrue(state.food_acquired)

    def test_deliver_clears_food_acquired(self):
        state = FeedingState(food_acquired=True)
        result = deliver_food(state)
        self.assertTrue(result["ok"])
        self.assertFalse(state.food_acquired)


class RunIntentTest(unittest.TestCase):
    """核心:意图 -> 动作的状态机分发。"""

    def test_feed_first_when_meal_not_active(self):
        state = FeedingState()
        outcome = run_intent("FEED", state)
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["resolved"], "FEED_FIRST")
        self.assertEqual(outcome["steps"], ["acquire_food", "deliver_food"])
        self.assertTrue(state.meal_active)

    def test_feed_continue_when_meal_active(self):
        state = FeedingState(meal_active=True)
        outcome = run_intent("FEED", state)
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["resolved"], "FEED_CONTINUE")
        self.assertEqual(outcome["steps"], ["deliver_food"])
        self.assertTrue(state.meal_active)

    def test_stop_feed_while_active_reports_meal_ended(self):
        state = FeedingState(meal_active=True, food_acquired=True)
        outcome = run_intent("STOP_FEED", state)
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["resolved"], "STOP_FEED")
        self.assertTrue(outcome["meal_ended"])
        self.assertFalse(state.meal_active)
        self.assertFalse(state.food_acquired)

    def test_stop_feed_while_idle_not_meal_ended(self):
        state = FeedingState()
        outcome = run_intent("STOP_FEED", state)
        self.assertTrue(outcome["ok"])
        self.assertFalse(outcome["meal_ended"])

    def test_unknown_intent_is_rejected_safely(self):
        state = FeedingState()
        outcome = run_intent("DANCE", state)
        self.assertFalse(outcome["ok"])
        self.assertIn("error", outcome)
        self.assertFalse(state.meal_active)

    def test_full_meal_cycle(self):
        """一顿完整的饭:first -> continue -> stop,状态首尾一致。"""
        state = FeedingState()
        self.assertEqual(run_intent("FEED", state)["resolved"], "FEED_FIRST")
        self.assertEqual(run_intent("FEED", state)["resolved"], "FEED_CONTINUE")
        stop = run_intent("STOP_FEED", state)
        self.assertTrue(stop["meal_ended"])
        # 结束后再喂,应重新从 FEED_FIRST 开始
        self.assertEqual(run_intent("FEED", state)["resolved"], "FEED_FIRST")


if __name__ == "__main__":
    unittest.main()
