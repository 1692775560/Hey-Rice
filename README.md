# 小瓜 · 喂饭陪伴 Agent（Hey-Rice）

面向失能/老年人的**喂饭陪伴机器人**语音交互系统。患者用**语音或文字**和「小瓜」交流，
系统做**意图识别**区分「命令」和「对话」，命令触发喂饭机械臂动作，回复再由**机器人用真人音色说出来**。

- **命令** → 调用对应机械臂动作（取餐 / 送餐 / 停止）
- **对话** → 以「小瓜」人设（温柔家人感）温柔回话
- **两个 LLM 分工**：意图识别只做判断（强制 JSON、温度 0），对话只做回话（带人设），职责不混
- **语音输入**：本地唤醒词「小瓜小瓜」+ 豆包流式 ASR
- **语音输出**：机器人用豆包音色（默认小何·甜美台湾腔）**逐字**念出 agent 回复

---

## 系统架构

```text
                       ┌───────────────────────────── 服务端（Mac / 同网段主机）─────────────────────────────┐
  浏览器（网页）         │                                                                                    │
  ┌───────────┐        │   server.py (HTTP :8000)                                                            │
  │ 打字输入   │──POST /api/chat─▶  process(text)                                                             │
  │           │        │      ├─ intent.py  ─意图识别(LLM)─▶  命令 / 对话                                     │
  │ 麦克风     │        │      ├─ actions.py ─命令→机械臂动作(取餐/送餐/停)                                   │
  │  │PCM     │        │      ├─ chat_agent.py ─对话(LLM)─▶ 回复                                              │
  │  ▼        │        │      └─ preferences.py ─对话里学到的吃饭偏好落盘                                     │
  │ /ws/voice │◀─wss──▶│   voice_gateway.py (WS :8765)                                                       │
  └───────────┘        │      本地 sherpa-onnx 检测「小瓜小瓜」→ 唤醒后音频进豆包流式 ASR → 最终文本→process │
                       │                                                                                    │
                       │   robot_tts.py ── POST /say(回复文本) ─────────────────┐                            │
                       └───────────────────────────────────────────────────────┼────────────────────────────┘
                                                                                │  局域网
                       ┌──────────────── 机器人（Galbot, 同网段）────────────────▼────────────────┐
                       │  robot/robot_player.py (Flask :5002)                                       │
                       │     /say → doubao_say.py（豆包 SayHello 逐字合成 24k→重采样16k）→ 扬声器   │
                       └────────────────────────────────────────────────────────────────────────────┘

  LLM(意图/对话)：任意 OpenAI 兼容接口（示例用 DeepSeek）
  ASR / 机器人TTS：火山引擎豆包（流式语音识别 + 实时对话 SayHello）
```

> **关键约束**：机器人在局域网内，推送端（服务端）必须和机器人**同一网段**。云平台（Vercel 等 serverless）跑不了常驻语音网关、也够不到内网机器人，不适用。

---

## 命令与动作

| 患者说 | 意图 | 机械臂动作 |
|---|---|---|
| 「喂我吃饭」（还没取餐） | `FEED_FIRST` | **取餐 → 送餐**（两步） |
| 「继续」「再来一口」（喂饭中） | `FEED_CONTINUE` | **送餐**（一步） |
| 「不吃了」「够了」 | `STOP_FEED` | 停 + 撤回 |
| 「今天天气真好」「这是什么菜」 | （对话） | 不动，小瓜温柔回话 |

> `FEED_FIRST` vs `FEED_CONTINUE` 由**当前喂饭状态**（`meal_active`）+ 语义共同判断：没在喂饭时任何「喂」都先取再喂。

---

## 快速开始

### 1. 服务端（Mac / 同网段主机）

```bash
# 依赖（语音入口需要）
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 下载本地唤醒模型（进入已忽略的 models/ 目录）
python scripts/setup_kws.py

# 配置：复制 .env.example 为 .env 并填好密钥（.env 已被 gitignore）
cp .env.example .env
#   MEALMATE_API_KEY        —— LLM 密钥（OpenAI 兼容，示例用 DeepSeek）
#   DOUBAO_APP_ID / TOKEN   —— 火山豆包（语音识别 + 机器人 TTS 共用）
#   HEYRICE_ROBOT_TTS_URL   —— 机器人播放服务地址（要机器人语音时填）
```

**一键启动**（自动载入 `.env` + venv）：

```bash
./start_heyrice.sh
# 浏览器打开 http://127.0.0.1:8000
```

不需要语音输入时设 `MEALMATE_VOICE_ENABLED=0`；不需要机器人语音时不配 `HEYRICE_ROBOT_TTS_URL` 即可，文字对话不受影响。

### 2. 机器人端（Galbot，负责语音输出）

```bash
# 把 robot/ 下的文件放到机器人上（示例路径 ~/wei_fan/wei_audio/）
#   robot_player.py  doubao_say.py  run_player.sh
# 填豆包密钥
cp robot/robot.env.example robot.env   # 编辑填入 DOUBAO_APP_ID / DOUBAO_ACCESS_TOKEN

# 启动常驻播放服务（带崩溃自动重启；需 galbot_sdk 环境，见 run_player.sh）
setsid bash ./run_player.sh > /tmp/robot_player.log 2>&1 < /dev/null &
```

机器人需具备：`flask`、`websocket-client`、系统 `ffmpeg`，能联网到火山豆包。

---

## 三条链路

### ① 文字对话

浏览器 `POST /api/chat {"text": "..."}` → `server.process()` → 意图识别 / 对话 → 返回结构化结果（意图、机械臂步骤、回复、喂饭状态、偏好）。

### ② 语音输入（本地唤醒 + 豆包 ASR）

```text
浏览器麦克风 → 16kHz PCM（WebSocket /ws/voice）
  → 本地 sherpa-onnx 检测「小瓜小瓜」（唤醒前不上云）
  → 唤醒后音频进豆包流式 ASR
  → 最终文本 → /ws/agent → server.process()（与打字同一套逻辑）
```

**唤醒一次即进入持续对话**：说一次「小瓜小瓜」唤醒后，之后每句直接说、无需再喊唤醒词
（静音期间用本地 VAD 等待，不占用云端 ASR 连接）。会话结束条件:
- agent 判定为 `STOP_FEED`（用户说「不吃了」），或
- 连续 `MEALMATE_CONVERSATION_MS`（默认 10 分钟）无有效交互（说话过程不计入）。

浏览器连 `ws://<host>:8765/ws/voice`。服务默认内置一个 agent 客户端（`MEALMATE_EMBED_AGENT=1`），
通过 `/ws/agent` 调用 `server.process(text)`。接第三方独立 agent 时设 `MEALMATE_EMBED_AGENT=0`，对方按
`final_transcript` 收文本、按相同 `requestId` 回 `agent_result`。

### ③ 机器人语音输出（豆包逐字 TTS）

```text
agent 回复文本 → robot_tts.py POST /say
  → robot_player.py → doubao_say.py（豆包实时对话 SayHello 事件，逐字合成，不经大模型改写）
  → 豆包出原生 24kHz → ffmpeg 重采样到扬声器的 16kHz（清亮不闷）
  → galbot 扬声器播放
```

文字和语音两条输入链路的回复都会走到这里，所以**说的每句 agent 回复都会由机器人念出来**。
音色可切换（`ROBOT_TTS_SPEAKER` 或请求体 `speaker`）：`zh_female_xiaohe`（小何·甜美台湾腔，默认）、
`zh_female_vv`（薇薇·活泼女）、`zh_male_yunzhou`（云舟·沉稳男）、`zh_male_xiaotian`（小天·磁性男）。

---

## 配置（`.env`）

| 变量 | 说明 |
|---|---|
| `MEALMATE_API_BASE` / `MEALMATE_API_KEY` | LLM 接口（OpenAI 兼容）。示例：`https://api.deepseek.com` |
| `MEALMATE_INTENT_MODEL` / `MEALMATE_CHAT_MODEL` | 意图 / 对话模型（如 `deepseek-chat`） |
| `MEALMATE_SEND_TEMPERATURE` | 是否发送 temperature（DeepSeek 支持则设 `1`，意图拿到确定性 0） |
| `MEALMATE_VOICE_ENABLED` | 语音输入开关（`0` 关闭） |
| `MEALMATE_CONVERSATION_MS` | 语音会话空闲超时（毫秒，默认 600000＝10 分钟） |
| `DOUBAO_APP_ID` / `DOUBAO_ACCESS_TOKEN` | 火山豆包凭证（ASR + 机器人 TTS 共用） |
| `HEYRICE_ROBOT_TTS_URL` | 机器人播放服务地址，如 `http://172.16.20.160:5002/say`；不填则不启用机器人语音 |
| `HEYRICE_ROBOT_TTS_SPEAKER` | 可选，指定豆包音色（留空用机器人端默认小何） |

> LLM 是 OpenAI 兼容格式（`{API_BASE}/chat/completions`），换任意兼容供应商只改 `MEALMATE_API_BASE` + 模型名，代码不用动。

---

## 跑测试

离线单元测试，不联网、不需要真实密钥：

```bash
MEALMATE_API_KEY=dummy python -m unittest discover -s tests -v
```

覆盖：意图 JSON 抠取与收容校验、本地快路径规则、喂饭状态机（`FEED_FIRST` / `FEED_CONTINUE` / `STOP_FEED`）、
偏好去重落盘、语音协议 / 网关 / 唤醒 / 前端资源。需要网络的路径（豆包 ASR）需先 `pip install -r requirements.txt`。

---

## 文件结构

| 文件 | 作用 |
|---|---|
| `server.py` | HTTP 服务 + `process()` 主流程；启动语音网关；每条回复推给机器人语音 |
| `config.py` | 从环境变量读 API / 模型配置 |
| `llm.py` | 底层 LLM 调用（OpenAI 兼容，两个 LLM 共用） |
| `intent.py` | **意图识别 LLM**：命令/对话区分 + FEED_FIRST/CONTINUE，强制 JSON + 收容校验 |
| `chat_agent.py` | **对话 LLM**：小瓜人设回话 |
| `actions.py` | 动作层：取餐/送餐/停（**现为 mock**，留好接口） |
| `preferences.py` | 对话里学到的吃饭偏好，去重落盘 |
| `voice_gateway.py` | 浏览器语音、本地唤醒、豆包 ASR、agent WebSocket 状态机 |
| `doubao_asr.py` | 豆包流式 ASR 单句会话适配器 |
| `wakeword.py` | sherpa-onnx 本地「小瓜小瓜」检测器 |
| `agent_ws_client.py` | 最终文本 → 现有 agent 的 WebSocket 适配器 |
| `robot_tts.py` | 把回复文本 POST 给机器人语音服务（`/say`） |
| `static/voice-client.js` · `pcm-worklet.js` | 前端麦克风采集、PCM 重采样、语音状态机 |
| `scripts/setup_kws.py` | 下载并准备本地唤醒模型 |
| `start_heyrice.sh` | 一键载入 `.env` + venv 启动服务端 |
| `robot/robot_player.py` | **机器人**常驻语音服务（`/say` `/play` `/health`） |
| `robot/doubao_say.py` | **机器人**豆包 SayHello 逐字 TTS（24k→16k） |
| `robot/run_player.sh` · `robot.env.example` | 机器人启动脚本 + 密钥样例 |

---

## 接真实动作 API

动作 API 给了之后，只改 [`actions.py`](actions.py) 里的 `_call_arm()` 一个函数，保持返回 `{"ok": bool, ...}` 形状即可，
上层（意图识别、主循环）完全不用动。

---

## 安全要点（已内建）

- **意图识别用语义，不用关键词**：「别停」不会被误判成停，「不想吃了」正确识别为拒绝。
- **收容兜底**：模型返回非法/超时/非 JSON → 一律安全落到「当对话去追问」，**绝不误触发命令**。
- **命令确认话术用固定模板**，不交给自由对话 LLM（保证「说的」和「做的」一致）。
- **密钥只从环境变量读**：服务端 `.env`、机器人 `robot.env`，均 gitignore，不硬编码、不入库；前端永远拿不到。
- **唤醒前不上云**：等待唤醒期间的音频只进入本地 KWS，不创建豆包 ASR 会话。
- **最终文本才进 agent**：中间识别结果不会触发意图或机械臂动作。

---

## 参考

- [火山引擎流式语音识别大模型](https://www.volcengine.com/docs/6561/1354869?lang=zh)（语音输入 ASR）
- [火山引擎端到端实时语音大模型](https://www.volcengine.com/docs/6561/1594356?lang=zh)（机器人 TTS 用其 SayHello 事件）
- [sherpa-onnx Keyword Spotting](https://k2-fsa.github.io/sherpa/onnx/kws/index.html)（本地唤醒）
