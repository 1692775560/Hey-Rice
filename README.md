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
# 1. 安装语音依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. 下载本地唤醒模型(模型进入已忽略的 models/ 目录)
python scripts/setup_kws.py

# 3. 设置密钥(从环境变量读,不要写进代码)
export MEALMATE_API_KEY=你的密钥
export DOUBAO_APP_ID=你的豆包应用ID
export DOUBAO_ACCESS_TOKEN=你的豆包AccessToken
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
页面上点击麦克风按钮授权后,麦克风持续监听。说“小瓜小瓜”,再说“我想吃饭”,最终文本会通过 WebSocket 进入 Hey-Rice agent。手动文本输入仍然可用。

**方式 B · 命令行**
```bash
python agent.py               # 交互模式,逐句输入
python agent.py "喂我吃饭吧"    # 单句模式
```

不需要语音时可设置 `MEALMATE_VOICE_ENABLED=0`,此时文本聊天仍只依赖原有服务。所有密钥留在后端,前端页面永远拿不到。

## 语音链路

```text
浏览器麦克风
  -> 16 kHz PCM WebSocket
  -> 本地 sherpa-onnx 检测“小瓜小瓜”
  -> 唤醒后的音频进入豆包流式 ASR
  -> 最终文本发送到 /ws/agent
  -> Hey-Rice agent 处理并返回结果
```

浏览器连接 `ws://127.0.0.1:8765/ws/voice`;agent 连接 `ws://127.0.0.1:8765/ws/agent`。服务默认启动一个内置 agent 客户端,它通过这个 WebSocket 调用现有 `server.process(text)`。接入对方独立 agent 时设置:

```bash
export MEALMATE_EMBED_AGENT=0
```

对方 agent 接收:

```json
{
  "type": "final_transcript",
  "requestId": "uuid",
  "sessionId": "uuid",
  "text": "我想吃饭",
  "timestamp": 1783670400000
}
```

处理完成后按相同 `requestId` 返回:

```json
{
  "type": "agent_result",
  "requestId": "uuid",
  "result": {"reply": "好嘞", "intent": "FEED_FIRST"}
}
```

豆包接入使用优化双向流式端点,音频格式为 PCM/raw、16 kHz、16 bit、单声道。协议参考[火山引擎流式语音识别大模型文档](https://docs.volcengine.com/docs/6561/1354869?lang=zh)。本地唤醒使用[sherpa-onnx Keyword Spotting](https://k2-fsa.github.io/sherpa/onnx/kws/index.html)。

## 跑测试

离线单元测试,不联网、不需要真实密钥(用 dummy 占位即可):

```bash
MEALMATE_API_KEY=dummy python -m unittest discover -s tests -v
```

覆盖:意图 JSON 抠取与收容校验、本地快路径规则、喂饭状态机(FEED_FIRST / FEED_CONTINUE / STOP_FEED)、偏好去重落盘。需要 LLM 的路径用打桩替换,验证异常/非 JSON 一律安全兜底为对话,绝不误触发命令。

## 文件结构

| 文件 | 作用 |
|---|---|
| `config.py` | 从环境变量读 API / 模型配置 |
| `llm.py` | 底层 LLM 调用(OpenAI 兼容,两个 LLM 共用) |
| `intent.py` | **意图识别 LLM**:命令/对话区分 + FEED_FIRST/CONTINUE 判断,强制 JSON + 收容校验 |
| `chat_agent.py` | **对话 LLM**:小瓜人设(温柔家人感)回话 |
| `actions.py` | 动作层:取餐/送餐/停(**现在 mock**,留好接口) |
| `agent.py` | 主循环:输入 → 意图识别 → 命令调动作 / 对话调小瓜 |
| `voice_gateway.py` | 浏览器语音、本地唤醒、豆包 ASR 和 agent WebSocket 状态机 |
| `doubao_asr.py` | 豆包流式 ASR 单句会话适配器 |
| `wakeword.py` | sherpa-onnx 本地“小瓜小瓜”检测器 |
| `agent_ws_client.py` | 最终文本到现有 Hey-Rice agent 的 WebSocket 适配器 |
| `scripts/setup_kws.py` | 下载并准备本地关键词模型 |

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
- **唤醒前不上云**:等待唤醒期间的音频只进入本地 KWS,不创建豆包 ASR 会话。
- **最终文本才进 agent**:中间识别结果不会触发意图或机械臂动作。
