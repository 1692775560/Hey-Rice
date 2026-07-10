# 更新日志

本项目按「保留功能完整性、只增不删」的原则记录变更。
版本格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)。

## [Unreleased] · dev 分支持续完善

### Fixed
- 修复 `intent.py` 本地快路径引用了 `FeedingState` 不存在的 `feeding` 字段,
  导致新会话说「继续/再来/接着」时抛 `AttributeError`;改用真实字段 `meal_active`。
- 修复 `server.py` 的 `/api/reset` 写入幽灵字段 `STATE.feeding` 且从不重置 `meal_active`,
  reset 后下一顿饭会错误地「继续」而非重新取餐;现在正确归零 `meal_active` 与 `food_acquired`。
- 前端 `index.html` 的意图/动作卡片改为先转义模型返回文本(`reason` / `intent`)再插入 DOM,
  消除 LLM 输出被当作 HTML 执行的潜在 XSS 风险。

### Added
- `requirements.txt`:说明运行时仅需 Python 3.9+ 标准库,并列出可选增强依赖 certifi。
- `run.sh`:一键启动脚本,支持 Web 模式(`./run.sh`)与命令行模式(`./run.sh cli`)。
- `CHANGELOG.md`:本变更日志。
- 前端适老化与无障碍:大字体一键切换(记忆用户选择)、对话区 `role=log`/`aria-live` 播报、
  状态 `role=status`、按钮与输入框 `aria-label`、统一键盘聚焦轮廓。
- 前端动作时间线:命令卡把机械臂步骤渲染成「取餐 → 送餐」中文时间线;
  一顿饭结束时展示小瓜记住的吃饭偏好(复用后端 `prefSaved`)。

### Changed
- `.env.example` 与 `config.py` 默认值对齐(模型默认 Haiku、超时 12 秒、重试 1 次等),
  并补齐 `MEALMATE_PREF_MODEL`、`MEALMATE_SEND_TEMPERATURE`、`MEALMATE_HOST`、`MEALMATE_PORT` 等变量说明。
- `README.md` 补充 `run.sh` 用法,并将模型说明与实际默认值(快模型 Haiku,可切 Opus)对齐。
