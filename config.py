"""集中管理模型与 API 配置。

所有密钥只从环境变量读取,绝不硬编码。
中转站是 OpenAI 兼容格式(/v1/chat/completions),用里面的 Claude 模型。
"""
import os


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


# ---- 中转 API(OpenAI 兼容) ----
API_BASE = _env("MEALMATE_API_BASE", "https://api.inferera.com/v1")
API_KEY = _env("MEALMATE_API_KEY")  # 必填,从环境变量读;不要写进代码

# ---- LLM 分别指定模型(不同任务用不同模型,兼顾速度与质量)----
# 意图识别:任务简单(判 FEED/STOP/对话),用快模型 Haiku,几乎不掉准度但快很多
INTENT_MODEL = _env("MEALMATE_INTENT_MODEL", "claude-haiku-4-5-20251001")
# 对话(小瓜回话):要有人设温度与自然度,用 Opus
CHAT_MODEL = _env("MEALMATE_CHAT_MODEL", "claude-opus-4-8")
# 偏好抽取:也是结构化小任务,用快模型
PREF_MODEL = _env("MEALMATE_PREF_MODEL", "claude-haiku-4-5-20251001")

# ---- 调用参数 ----
INTENT_TEMPERATURE = float(_env("MEALMATE_INTENT_TEMP", "0"))    # 意图识别要确定性,温度 0
CHAT_TEMPERATURE = float(_env("MEALMATE_CHAT_TEMP", "0.7"))      # 对话可以活一点
REQUEST_TIMEOUT = float(_env("MEALMATE_TIMEOUT", "30"))          # 秒(Opus 稍慢,给足)
LLM_RETRIES = int(_env("MEALMATE_LLM_RETRIES", "2"))             # 网络抖动时的额外重试次数

# 新版 Claude 模型(如 claude-opus-4-8)不再接受 temperature 参数,默认不发送。
# 若你的模型/中转支持,可 export MEALMATE_SEND_TEMPERATURE=1 打开。
SEND_TEMPERATURE = _env("MEALMATE_SEND_TEMPERATURE", "0") == "1"


def assert_configured() -> None:
    """启动时检查关键配置,缺失就早报错(而不是调用时才崩)。"""
    if not API_KEY:
        raise RuntimeError(
            "缺少 MEALMATE_API_KEY 环境变量。请先设置:\n"
            "  export MEALMATE_API_KEY=你的密钥\n"
            "(不要把密钥写进代码或提交到仓库)"
        )
