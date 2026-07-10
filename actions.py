"""动作调用层。

把意图翻译成机械臂动作。现在是 mock(打印),留好接口,
等真实动作 API 给了,替换每个函数体里的 TODO 即可。

喂饭分两种,机械臂动作不同:
  - 第一次喂饭 FEED_FIRST : 先【取餐】再【送餐】(固定两步)
  - 继续喂饭   FEED_CONTINUE: 只【送餐】(固定一步,已经取过餐了)
  - 不喂了     STOP_FEED   : 停 → 这顿饭结束

★ 重要原则一:动作是固定的,顺序不可随意改动。
  - 每个意图对应的动作序列写死在下面的函数里,代码不擅自增减或调换步骤。

★ 重要原则二:FIRST 还是 CONTINUE 由【代码的状态】决定,不靠 LLM 判断。
  - LLM 只判断"要喂饭 / 不喂了 / 对话"(语义),【不区分】第一次还是继续。
  - 代码根据 meal_active(这顿饭是否进行中)决定:
      这顿饭还没开始(meal_active=False)+ 要喂饭 → FEED_FIRST(取餐→送餐),标记开始
      这顿饭进行中  (meal_active=True) + 要喂饭 → FEED_CONTINUE(只送餐)
      不喂了 STOP_FEED → 这顿饭结束(meal_active=False),下次喂饭又从 FIRST 开始
  - 这样 first/continue 铁定由状态控制,LLM 判错也不影响。
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class FeedingState:
    """喂饭状态。核心是 meal_active:这顿饭是否进行中。

    生命周期:
      一顿饭开始(第一次喂饭成功)→ meal_active=True
      一顿饭结束(STOP_FEED)      → meal_active=False
    """
    meal_active: bool = False     # 这顿饭是否进行中(决定 FIRST/CONTINUE 的唯一依据)
    food_acquired: bool = False   # 勺子里有没有取到餐(动作层内部用)

    def describe(self) -> str:
        """一句话状态描述(展示/日志用)。"""
        return "这顿饭进行中(继续喂即可)" if self.meal_active else "这顿饭还没开始(下次喂饭需先取餐)"


# ============ 真实动作调用(现在是 mock)============
# TODO: 拿到动作 API 后,把下面每个函数体替换成真实的 HTTP 调用。
# 例如:
#   import requests
#   requests.post(f"{ARM_API_BASE}/acquire", json={...}, timeout=...)

def _call_arm(action: str, **params) -> dict:
    """统一的机械臂 API 调用入口(mock)。

    真实实现时,这里改成对动作 API 的 HTTP 请求;
    保持返回 {"ok": bool, "action": str, ...} 的形状,上层不用改。
    """
    print(f"  [机械臂] → 调用动作: {action}  参数={params}")
    # TODO(真实 API): return requests.post(ARM_ENDPOINT[action], json=params).json()
    return {"ok": True, "action": action, "params": params}


def acquire_food(state: FeedingState) -> dict:
    """取餐:勺子舀取食物。"""
    r = _call_arm("acquire_food")
    if r.get("ok"):
        state.food_acquired = True
    return r


def deliver_food(state: FeedingState) -> dict:
    """送餐:把食物送到嘴边。"""
    r = _call_arm("deliver_food")
    if r.get("ok"):
        state.food_acquired = False   # 已送出,勺子空了
    return r


def stop_feeding(state: FeedingState) -> dict:
    """不喂了:停止并撤回。"""
    r = _call_arm("stop_feeding")
    return r


# ============ 意图 → 动作 的分发 ============

def feed_first(state: FeedingState) -> dict:
    """第一次喂饭:固定两步——先取餐,再送餐。顺序写死,不可改动。"""
    print("  执行【第一次喂饭】:取餐 → 送餐(固定顺序)")
    r1 = acquire_food(state)
    if not r1.get("ok"):
        return {"ok": False, "step": "acquire_food", "detail": r1}
    r2 = deliver_food(state)
    ok = r2.get("ok", False)
    if ok:
        state.meal_active = True   # 这顿饭正式开始
    return {"ok": ok, "resolved": "FEED_FIRST", "steps": ["acquire_food", "deliver_food"]}


def feed_continue(state: FeedingState) -> dict:
    """继续喂饭:固定一步——只送餐。"""
    print("  执行【继续喂饭】:送餐(固定一步)")
    r = deliver_food(state)
    return {"ok": r.get("ok", False), "resolved": "FEED_CONTINUE", "steps": ["deliver_food"]}


def stop_feed(state: FeedingState) -> dict:
    """不喂了 → 这顿饭结束。"""
    print("  执行【不喂了】:停止 + 撤回 → 这顿饭结束")
    r = stop_feeding(state)
    ok = r.get("ok", False)
    meal_was_active = state.meal_active
    if ok:
        state.meal_active = False   # 这顿饭结束,下次喂饭又从 FIRST 开始
        state.food_acquired = False
    return {
        "ok": ok,
        "resolved": "STOP_FEED",
        "steps": ["stop_feeding"],
        "meal_ended": meal_was_active,   # 是否真的结束了一顿进行中的饭(供上层沉淀偏好)
    }


def run_intent(intent: str, state: FeedingState) -> dict:
    """★ 核心:根据【代码的状态】把语义意图映射成实际动作。

    LLM 只给出 FEED(要喂饭) / STOP_FEED(不喂了)。
    FIRST 还是 CONTINUE 由 meal_active 决定,不靠 LLM:
      FEED + 这顿饭没开始 → FEED_FIRST
      FEED + 这顿饭进行中 → FEED_CONTINUE
    """
    if intent == "FEED":
        if state.meal_active:
            return feed_continue(state)
        return feed_first(state)
    if intent == "STOP_FEED":
        return stop_feed(state)
    return {"ok": False, "error": f"未知命令意图: {intent}", "steps": []}
