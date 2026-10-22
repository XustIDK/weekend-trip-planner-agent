"""第 4 层：对话摘要。把滚出窗口的原文压成要点，省 token 但有损。

摘要永远只是「视图」：原文仍在账本里，摘要失真时可以回账本重算。
"""
from __future__ import annotations

from typing import Any, Callable, Iterable

PROMPT = """你在为一个旅行规划 Agent 压缩对话记忆。把下面的历史对话压缩成不超过 {limit} 字的中文要点。

必须保留：出发地、日期/时长、总预算、同行人、兴趣与饮食偏好、交通方式、节奏偏好；
用户改过口的地方（例如预算从 5 万涨到 8 万）要写成「新值（原为旧值）」；
已确认事项、尚未确认的问题也要保留。

要求：只做压缩，不要提建议，不要补全没说过的事实，不要编造数字。只输出摘要正文，不要任何前缀。

已有摘要（可能为空）：
{previous}

新增对话：
{history}
"""


def _fmt(events: Iterable[dict[str, Any]]) -> str:
    lines = []
    for e in events:
        role = "用户" if e.get("kind") == "user" else "助手"
        lines.append(f"[第{e.get('turn', 0)}轮 {role}] {e.get('text', '')}")
    return "\n".join(lines)


def fallback(previous: str, events: list[dict[str, Any]], limit: int) -> str:
    """离线/模型失败时的保底摘要：抽取式。"""
    parts = [previous] if previous else []
    for e in events:
        if e.get("kind") != "user":
            continue
        text = " ".join(e.get("text", "").split())
        if text:
            parts.append(f"第{e.get('turn', 0)}轮：{text[:40]}")
    text = "；".join(p for p in parts if p)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def roll(previous: str, events: list[dict[str, Any]], complete: Callable[[str], str], *,
         limit: int = 400) -> dict[str, Any]:
    """返回 {text, source, upto}；complete 是「无工具的一次模型调用」。"""
    if not events:
        return {"text": previous, "source": "unchanged", "upto": None}
    history = _fmt(events)
    try:
        text = complete(PROMPT.format(limit=limit, previous=previous or "（无）", history=history))
        text = " ".join((text or "").split())
        if len(text) > limit:
            text = text[: limit - 1] + "…"
        if text:
            return {"text": text, "source": "llm", "upto": events[-1]["id"]}
    except Exception:
        pass
    return {"text": fallback(previous, events, limit), "source": "extractive",
            "upto": events[-1]["id"]}
