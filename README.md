# 周末去哪儿：美团规划助手（Windows / PowerShell 版）

一个轻量的多轮对话 Agent 演示：模型读取分层记忆，调用本地工具保存用户约束、更新行程草案，
并在用户明确要求后输出唯一最终方案。

记忆按学习笔记的 **「4 层核心 + 1 层向量兜底」** 实现，外加一层只追加的**账本**：

| 层 | 存什么 | 实现 | 特点 |
| --- | --- | --- | --- |
| 账本 Ledger | 全部原始事件（对话、工具调用、摘要滚动） | `data/<sid>.events.jsonl` | 只追加，其它几层都可从它重算 |
| ① 滑动窗口 | 最近 6 轮（可配）原文 | `memory/window.py` | 零检索；超轮数/字符预算丢最早的 |
| ② 会话元数据 | 时区、设备、场景标签、本地时间 | `memory/session_meta.py` | 24h 未活动即作废，不进长期库 |
| ③ 用户档案卡 | 出发地/日期/预算/同行人/兴趣/饮食/交通/节奏/限制/已确认 | `memory/profile.py` | 结构化、精确读取、覆盖式更新；每字段带更新时间与出处 |
| ④ 对话摘要 | 滚出窗口的原文压成要点 | `memory/summarizer.py` | 省 token、有损；模型失败自动退化为抽取式 |
| ⑤ 长期归档 | 全量历史 + 向量检索 | `memory/archive.py` | 模糊召回；本地哈希向量零依赖，可切远端 embedding |

策略层 `memory/policy.py` 决定何时检索、何时摘要、何时遗忘；上下文按可信度优先级拼装见 `memory/context.py`。

## 环境要求

- Windows 10 / 11
- Python 3.10+（安装时勾选 **Add python.exe to PATH**）
- 无需第三方 Python 包（向量检索是纯标准库实现）

## 启动

### 方式一：一键脚本（推荐）

```powershell
.\start.ps1
```

或直接双击 `start.bat`。

`start.ps1` 会自动完成：控制台切到 UTF-8、检测 Python 版本、缺失时用 `.env.example` 生成 `.env`，并打开浏览器。

可选参数：

```powershell
.\start.ps1 -Port 8766 -NoBrowser   # 换端口 / 不自动开浏览器
```

如果提示"禁止运行脚本"，用下面的方式启动，或先执行一次
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

### 方式二：手动启动

```powershell
Copy-Item .env.example .env
notepad .env          # 把 OPENAI_API_KEY 改成可用的 DeepSeek API Key
python app.py         # 或 py -3 app.py
```

浏览器打开 <http://127.0.0.1:8765>。`app.py` 会自己读取项目根目录的 `.env`。
停止服务：在运行窗口按 `Ctrl+C`。

## 配置项（`.env`）

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `OPENAI_API_KEY` | 模型 API Key，占位值会被当作未配置 | — |
| `OPENAI_BASE_URL` | Chat Completions 兼容地址 | `https://api.deepseek.com` |
| `MODEL_NAME` | 模型名 | `deepseek-chat` |
| `HOST` / `PORT` | 监听地址与端口 | `127.0.0.1` / `8765` |
| `OPEN_BROWSER` | 启动后自动开浏览器 | `1` |
| `MEMORY_WINDOW_TURNS` | ① 窗口保留几轮原文 | `6` |
| `MEMORY_WINDOW_CHARS` | ① 窗口字符预算 | `3000` |
| `MEMORY_SUMMARY_MAX_CHARS` | ④ 摘要目标长度 | `400` |
| `MEMORY_SUMMARY_SPILL_TRIGGER` | 滚出窗口多少条事件后触发摘要 | `4` |
| `MEMORY_ARCHIVE_TOP_K` / `MEMORY_ARCHIVE_MIN_SCORE` | ⑤ 召回条数 / 相似度阈值 | `3` / `0.15` |
| `MEMORY_META_TTL_HOURS` | ② 元数据保留小时（超时作废） | `24` |
| `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` / `EMBEDDING_MODEL` | 可选：改用远端 embedding（OpenAI 兼容 /v1/embeddings） | 未配置则用本地哈希向量 |

## Agent 是怎么工作的

```text
用户消息
  → ② 刷新会话元数据（时区/设备/场景标签，临时）
  → ① 取最近 6 轮原文作为窗口
  → ⑤ 问到"过去/以前/最早"时先自动向量召回一次（只当线索）
  → 组装上下文：③ 档案卡（最权威）> ② 元数据 > ④ 摘要 > ⑤ 召回 > ① 原文
  → 模型：update_memory 写档案卡 / search_archive 查历史 / save_draft / finalize_plan
  → 回答写入账本 → ⑤ 归档索引补新事件 → 滚出窗口的对话压成 ④ 摘要
```

几条关键行为：

- **确定性只认档案卡**：预算、日期、人数这类问题，模型被要求只依据 ③ 的结构化字段回答，不靠"猜"。
- **覆盖不丢证据**：档案卡覆盖旧值时，旧值、时间、依据的原话都写进账本；`memory_meta` 记录每个字段最后更新于第几轮。
- **摘要只是视图**：摘要失真可以从账本重算；原文永远在账本里。
- **召回要标注不确定**：⑤ 返回的片段带相似度分数，提示词要求模型把它们当线索而非事实。
- **对话按 Markdown 渲染**：表格、列表、代码块、引用、链接直接在气泡里显示（前端内置极简渲染器，仍不引入第三方依赖）。

## 数据文件

```text
data/<session-id>.events.jsonl    账本：每轮对话、工具调用、摘要滚动，只追加
data/<session-id>.archive.jsonl   ⑤ 归档索引：文本 + 稀疏向量（4096 维）
data/<session-id>.json            会话视图：档案卡、字段元数据、摘要、草案、定稿
```

清空记忆：删掉 `data/` 下对应会话的三个文件，或在页面上点"＋ 新建行程"。

## 离线自检（不联网、不消耗 API）

```powershell
python selftest_memory.py
```

覆盖：账本只追加、窗口轮数/字符预算、元数据 TTL、档案卡覆盖与留痕、摘要退化、
归档向量检索、上下文拼装与截断、端到端工具链、旧会话迁移。

## 常见问题

- **页面提示"尚未配置模型 API Key"**：`.env` 里的 `OPENAI_API_KEY` 还是占位值，填好真实 Key 后重启服务。
- **摘要一直没触发？** 只有对话滚出窗口、且溢出事件达到 `MEMORY_SUMMARY_SPILL_TRIGGER` 才触发；短对话不需要摘要。
- **归档召回不准？** 本地哈希向量只做字面相似度（中文按 2/3-gram + 少量同义词），同义改写会漏召回。
  配置 `EMBEDDING_*` 换成真 embedding 即可显著改善；索引会在向量实现变化时自动重建。
- **端口被占用**：改 `.env` 的 `PORT`，或用 `.\start.ps1 -Port 8766`。
- **控制台中文乱码**：用 `start.ps1` / `start.bat` 启动；手动启动时先 `chcp 65001`。
- **TLS / 证书错误**：Windows 默认使用系统证书存储；公司代理环境可 `pip install certifi` 后重启。

## 已知边界

- 记忆是**单会话**的：归档按会话隔离，换设备/清 localStorage 即开新会话，没有跨会话用户档案。
- ⑤ 归档是**兜底**，不是主路径：字段级事实永远走 ③ 档案卡；检索只用于"翻旧账"。
- 遗忘只做了 ② 元数据的 TTL；③ 档案卡与 ⑤ 归档目前只增不减（上限 `MEMORY_ARCHIVE_MAX_ENTRIES` 条，超出停止索引）。
- 界面只展示记忆状态，不提供手动纠错入口；要改某个字段，直接对话说明即可。
