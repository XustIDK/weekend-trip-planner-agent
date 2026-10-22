"""账本（Ledger）：只追加的事件日志，是排障溯源的唯一依据。

每条事件一行 JSON，写入后不可修改；所有视图（窗口 / 摘要 / 归档索引 / 前端对话流）
都从账本重算，因此「视图覆盖」不会丢证据。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

_LOCK = threading.Lock()

CONVERSATION_KINDS = ("user", "assistant")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def path_for(data_dir: Path, sid: str) -> Path:
    return data_dir / f"{sid}.events.jsonl"


def read_all(data_dir: Path, sid: str) -> list[dict[str, Any]]:
    p = path_for(data_dir, sid)
    if not p.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and "id" in item:
            events.append(item)
    return events


def append(data_dir: Path, sid: str, *, kind: str, text: str = "", turn: int = 0,
           data: dict[str, Any] | None = None) -> dict[str, Any]:
    """追加一条事件。kind: user / assistant / tool / note / session。"""
    with _LOCK:
        events = read_all(data_dir, sid)
        event = {
            "id": (events[-1]["id"] if events else 0) + 1,
            "ts": now_iso(),
            "turn": turn,
            "kind": kind,
            "text": text,
            "data": data or {},
        }
        with path_for(data_dir, sid).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event


def conversation(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """只保留对话事件（user / assistant），用于窗口、摘要与归档。"""
    return [e for e in events if e.get("kind") in CONVERSATION_KINDS]


def transcript(events: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    """给前端渲染的完整对话流（视图，不是模型上下文）。"""
    return [{"role": e["kind"], "content": e.get("text", "")}
            for e in events if e.get("kind") in CONVERSATION_KINDS]
