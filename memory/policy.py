"""策略（Policy）：决定何时检索、何时摘要、何时遗忘。没有它，记忆库会变成垃圾堆。"""
from __future__ import annotations

import os
import re

WINDOW_TURNS = int(os.getenv("MEMORY_WINDOW_TURNS", "6"))
WINDOW_CHARS = int(os.getenv("MEMORY_WINDOW_CHARS", "3000"))
SUMMARY_MAX_CHARS = int(os.getenv("MEMORY_SUMMARY_MAX_CHARS", "400"))
SUMMARY_SPILL_TRIGGER = int(os.getenv("MEMORY_SUMMARY_SPILL_TRIGGER", "4"))
ARCHIVE_TOP_K = int(os.getenv("MEMORY_ARCHIVE_TOP_K", "3"))
ARCHIVE_MIN_SCORE = float(os.getenv("MEMORY_ARCHIVE_MIN_SCORE", "0.15"))
ARCHIVE_WEAK_MIN_SCORE = float(os.getenv("MEMORY_ARCHIVE_WEAK_MIN_SCORE", "0.09"))
ARCHIVE_MAX_ENTRIES = int(os.getenv("MEMORY_ARCHIVE_MAX_ENTRIES", "4000"))
META_TTL_HOURS = int(os.getenv("MEMORY_META_TTL_HOURS", "24"))

# 问到「过去」时自动去第 5 层兜底检索；否则不打扰
_HISTORY_CUES = re.compile(
    r"上次|上回|之前|以前|原来|最早|最初|去年|前一天|那次|那次说|说过|聊过|还记得|"
    r"最开始|当时|历史|记录里|以前说"
)


def should_retrieve(user_text: str) -> bool:
    return bool(_HISTORY_CUES.search(user_text or ""))


def retrieval_query(user_text: str) -> str:
    """检索词去掉寒暄与标点，保留实体词。"""
    text = re.sub(r"[，。！？、；：,.!?;:~（）()\[\]【】\s]+", " ", user_text or "")
    return text.strip()[:120]


def should_roll_summary(spilled_count: int) -> bool:
    return spilled_count >= SUMMARY_SPILL_TRIGGER
