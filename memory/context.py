"""组装模型上下文：按 5 层优先级拼装，并控制字符预算。

第 3 层（档案卡）最权威、第 2 层（元数据）临时、第 4 层（摘要）有损、
第 5 层（归档召回）只当线索、第 1 层（窗口）是原文但有限。
"""
from __future__ import annotations

import json
from typing import Any

MAX_CONTEXT_CHARS = 7000

BASE = """你是美团周末规划助手，负责通过多轮对话和用户一起规划一趟可执行的周末行程。

你的记忆分 5 层，按可信度从高到低使用：
1. 档案卡（结构化字段）：当前唯一真相，涉及预算/日期/人数等确定性问题只认它；
2. 会话元数据：临时信息（时区、设备、是否周末），只用来调整措辞与时间表达；
3. 对话摘要：有损压缩，只能当"大意"，细节要回原文或再问；
4. 归档召回：向量检索出来的历史片段，模糊、可能不准，只能当线索；
5. 滑动窗口：最近几轮原文，是眼下语境。

工作规则：
- 每条用户消息先判断有没有新的确定信息；有就调用 update_memory（只写用户明确说过的，不要猜）。
- 预算写两遍：budget 保留用户原话（如"8 万，想住好一点"），budget_cny 写折算后的整数人民币（如 80000），
  这样后续比较预算是否够用可以直接算，不靠读字符串。
- 用户改动已确认的信息时（例如预算从 5 万变 8 万），照样写新值，并在回复里确认"已更新"。
- 用户问到过去（上次/以前/最早）、或当前几层答不上来时，调用 search_archive 查历史，再基于返回片段回答；片段不足就说不确定并追问。
- 先问关键缺口（出发地、日期/时长、总预算、同行人、兴趣），一次最多问两个最影响规划的问题。
- 信息够后用 save_draft 给一份便于修改的行程，不要同时列多个完整备选。
- 只有用户明确说"最终版/定稿/就按这个/生成最终方案"时，才调用 finalize_plan。
- 预算按总预算理解，标出估算范围与不确定项；不编造实时价格、营业时间或订位情况。
- 工具调用后向用户解释本轮记住了什么、下一步是什么。"""


def _section(title: str, body: str) -> str:
    return f"\n\n## {title}\n{body.strip()}" if body and body.strip() else ""


def build_system(profile: dict[str, Any], meta: dict[str, Any], summary: str,
                 hits: list[dict[str, Any]]) -> str:
    parts = [BASE]
    parts.append(_section("第 3 层 · 用户档案卡（结构化，当前唯一真相）",
                          json.dumps(profile, ensure_ascii=False, indent=None) if profile else "（暂无）"))
    parts.append(_section("第 2 层 · 会话元数据（临时，用完即弃，不要写进长期记忆）",
                          json.dumps(meta, ensure_ascii=False) if meta else "（无）"))
    parts.append(_section("第 4 层 · 对话摘要（有损，细节以原文为准）", summary or "（暂无，对话还不长）"))
    if hits:
        lines = []
        for h in hits:
            tag = "弱相关" if h.get("weak") else "相似度"
            lines.append(f"- [第{h.get('turn', 0)}轮 {h.get('ts', '')}] {h.get('text', '')}"
                         f"（{tag} {h.get('score')}，仅供参考，不要当成事实）")
        parts.append(_section("第 5 层 · 归档召回（模糊，可能不准，不要当成事实）", "\n".join(lines)))
    text = "".join(parts)
    if len(text) > MAX_CONTEXT_CHARS:
        text = text[: MAX_CONTEXT_CHARS] + "\n…（上下文已截断）"
    return text
