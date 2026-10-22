"""第 1 层：滑动窗口。眼前几轮原始对话，零检索开销，超出预算丢最早的。"""
from __future__ import annotations

from typing import Any, Iterable


def recent(events: Iterable[dict[str, Any]], *, max_turns: int = 6,
           max_chars: int = 3000) -> tuple[list[dict[str, str]], int]:
    """从账本尾部取最近若干轮原文。返回 (messages, 窗口起点事件 id)。"""
    conv = [e for e in events if e.get("kind") in ("user", "assistant")]
    if not conv:
        return [], 0
    kept: list[dict[str, Any]] = []
    turns_seen: set[int] = set()
    chars = 0
    for event in reversed(conv):
        turn = int(event.get("turn") or 0)
        new_turn = turn not in turns_seen
        if new_turn and len(turns_seen) >= max_turns:
            break
        text = event.get("text", "")
        if kept and chars + len(text) > max_chars:
            break
        kept.append(event)
        turns_seen.add(turn)
        chars += len(text)
    kept.reverse()
    messages = [{"role": e["kind"], "content": e.get("text", "")} for e in kept]
    return messages, (kept[0]["id"] if kept else 0)
