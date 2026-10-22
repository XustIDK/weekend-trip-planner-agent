# 周末去哪儿：美团规划助手

一个轻量的多轮对话 Agent 演示：模型读取当前会话记忆，调用本地工具保存用户约束、更新一份行程草案，并在用户明确要求后输出唯一最终方案。

## 启动

需要 Python 3.10+，不依赖第三方 Python 包。

```bash
cp .env.example .env
```

将 `.env` 中 `OPENAI_API_KEY` 改成可用的 DeepSeek API Key。再在启动 shell 中加载环境变量并启动：

```bash
set -a
source .env
set +a
python app.py
```

浏览器打开 <http://127.0.0.1:8765>。也可以直接导出 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`MODEL_NAME` 后启动。

## Agent 是怎么工作的

```text
用户消息 → 模型抽取意图/缺口 → update_memory 写入会话记忆
         → 信息不足时追问 / 信息足够时 save_draft
         → 用户反馈修改 → 模型读取旧记忆与草案并更新
         → 用户明确要求定稿 → finalize_plan 保存唯一最终方案
```

- **多轮对话**：近期消息作为上下文发送给模型；结构化约束独立保存在 `data/<session-id>.json`。
- **记忆**：记录出发地、日期、预算、同行人、兴趣、饮食、交通、节奏、限制与确认项；每个浏览器本地保存一个会话 ID。
- **逐步微调**：草案与最终方案分开存储；草案阶段可继续修改，定稿时才标成唯一方案。
- **工具执行边界**：模型只发出 tool call；Python 的 `run_turn` 校验并执行本地状态更新，随后将结果以 `role=tool` 回传模型。
- **预算和事实**：以模型估算为准，不连接美团商户实时数据，不承诺现价、营业或可订情况。UI 提醒出发前核验。

## 配置说明

后端使用 OpenAI Chat Completions 兼容接口的 tool calling。DeepSeek 默认地址为 `https://api.deepseek.com`，模型默认 `deepseek-chat`。本地会话 JSON 被 `.gitignore` 排除，API Key 只从环境变量读取。
