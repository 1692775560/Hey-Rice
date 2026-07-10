# 小瓜 · 喂饭陪伴 Agent(代码骨架)

用中转的 Claude 模型(默认快模型 Haiku,可切 Opus,见 `config.py` / `.env.example`)做意图识别,区分「命令」和「对话」:
- **命令** → 调用对应的机械臂动作(现在是 mock,等真实动作 API 直接替换)
- **对话** → 用「小瓜」人设(温柔家人感)温柔回话

**两个 LLM 分别调用**:意图识别只做判断(强制 JSON、温度 0),对话只做回话(带人设)。职责不混。

## 命令与动作

| 患者说 | 意图 | 机械臂动作 |
|---|---|---|
| 「喂我吃饭」(还没取餐) | `FEED_FIRST` | **取餐 → 送餐**(两步) |
| 「继续」「再来一口」(喂饭中) | `FEED_CONTINUE` | **送餐**(一步) |
| 「不吃了」「够了」 | `STOP_FEED` | 停 + 撤回 |
| 「今天天气真好」「这是什么菜」 | (对话) | 不动,小瓜温柔回话 |

> `FEED_FIRST` vs `FEED_CONTINUE` 由**当前喂饭状态**+ 语义共同判断:没取餐时任何"喂"都先取再喂。

## 怎么跑

```bash
# 1. 设置密钥(从环境变量读,不要写进代码)
export MEALMATE_API_KEY=你的密钥
# 可选:export MEALMATE_API_BASE=https://api.inferera.com/v1
```

**最简方式 · 一键脚本**
```bash
./run.sh          # 启动 Web 服务(默认 http://127.0.0.1:8000)
./run.sh cli      # 命令行交互模式
./run.sh cli "喂我吃饭吧"   # 命令行单句模式
```

**方式 A · 前台聊天页面(推荐,可视化对话)**
```bash
python server.py
# 浏览器打开 http://127.0.0.1:8000
```
页面上直接对小瓜说话,能看到:小瓜的温柔回复 + 意图识别结果(命令/对话)+ 触发的机械臂动作。

**方式 B · 命令行**
```bash
python agent.py               # 交互模式,逐句输入
python agent.py "喂我吃饭吧"    # 单句模式
```

只依赖 Python 标准库,无需 pip install。密钥留在后端,前端页面永远拿不到。

## 跑测试

离线单元测试,不联网、不需要真实密钥(用 dummy 占位即可):

```bash
MEALMATE_API_KEY=dummy python -m unittest discover -s tests -v
```

覆盖:意图 JSON 抠取与收容校验、本地快路径规则、喂饭状态机(FEED_FIRST / FEED_CONTINUE / STOP_FEED)、偏好去重落盘。需要 LLM 的路径用打桩替换,验证异常/非 JSON 一律安全兜底为对话,绝不误触发命令。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 前端聊天页面(index.html) |
| GET | `/api/health` | 健康检查:服务状态、模型、是否已配置密钥、会话数、运行时长(不含任何密钥) |
| POST | `/api/chat` | `{"text": "...", "session"?: "..."}` → 意图识别 + 命令调动作 / 对话回话 |
| POST | `/api/reset` | `{"session"?: "...", "clearPreferences"?: true}` → 重置这顿饭状态,可选一并遗忘该会话偏好 |

**多会话隔离**:每个浏览器/患者按 session id 各自维护喂饭状态与偏好记忆,互不串味。
session id 来源优先级:请求体 `session` > Cookie(`mm_session`)> 自动新建并回种 Cookie。
默认会话(`session=default`)沿用原来的 `preferences_store.json`,兼容旧的单会话行为;
其它会话的偏好各自落盘在 `data/sessions/<id>.json`(已在 `.gitignore` 忽略)。

## 文件结构

| 文件 | 作用 |
|---|---|
| `config.py` | 从环境变量读 API / 模型配置 |
| `llm.py` | 底层 LLM 调用(OpenAI 兼容,两个 LLM 共用) |
| `intent.py` | **意图识别 LLM**:命令/对话区分 + FEED_FIRST/CONTINUE 判断,强制 JSON + 收容校验 |
| `chat_agent.py` | **对话 LLM**:小瓜人设(温柔家人感)回话 |
| `actions.py` | 动作层:取餐/送餐/停(**现在 mock**,留好接口) |
| `agent.py` | 主循环:输入 → 意图识别 → 命令调动作 / 对话调小瓜 |

## 接真实动作 API

动作 API 给了之后,只改 [`actions.py`](actions.py) 里的 `_call_arm()` 一个函数:

```python
def _call_arm(action, **params):
    import urllib.request, json
    # 换成对真实动作 API 的 HTTP 请求,保持返回 {"ok": bool, ...} 的形状即可
    ...
```

上层(意图识别、主循环)完全不用动。

## 安全要点(已内建)

- **意图识别用语义,不用关键词**:「别停」不会被误判成停,「不想吃了」正确识别为拒绝。
- **收容兜底**:模型返回非法/超时/非 JSON → 一律安全落到「当对话去追问」,**绝不误触发命令**。
- **命令确认话术用固定模板**,不交给自由对话 LLM(保证"说的"和"做的"一致)。
- **密钥只从环境变量读**,不硬编码、不提交。
```
