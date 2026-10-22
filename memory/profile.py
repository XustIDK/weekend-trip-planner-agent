"""第 3 层：用户档案卡（结构化、可精确读取、覆盖式更新）。

两条铁律：
1. 对外的 memory 只保留「当前唯一真相」（覆盖式更新），前端直接渲染；
2. 每次覆盖都在账本里留痕（谁在什么时间、根据哪句话改的），并写入 memory_meta，
   这样「覆盖」不会丢证据。
"""
from __future__ import annotations

from typing import Any

SCHEMA_VERSION = 2

SCALAR_FIELDS = ("origin", "dates", "duration", "budget", "budget_cny", "party",
                 "transport", "pace")
LIST_FIELDS = ("interests", "food_preferences", "constraints", "confirmed", "open_questions")
FIELDS = SCALAR_FIELDS + LIST_FIELDS


def empty_profile() -> dict[str, Any]:
    view: dict[str, Any] = {k: None for k in SCALAR_FIELDS}
    view.update({k: [] for k in LIST_FIELDS})
    return view


def empty_meta() -> dict[str, Any]:
    return {}


def migrate(memory: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """兼容旧会话（memory 是裸值、没有 meta）。"""
    view, meta = empty_profile(), empty_meta()
    if not isinstance(memory, dict):
        return view, meta
    for key, value in memory.items():
        if key not in FIELDS or value in (None, "", []):
            continue
        if key in LIST_FIELDS:
            view[key] = [str(x) for x in value] if isinstance(value, list) else []
        else:
            view[key] = str(value)
        meta[key] = {"updated_at": None, "turn": None, "quote": None, "legacy": True}
    return view, meta


def quote_of(text: str, limit: int = 80) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def merge(view: dict[str, Any], meta: dict[str, Any], update: dict[str, Any], *,
          turn: int, ts: str, quote: str) -> list[dict[str, Any]]:
    """把工具参数合并进档案卡，返回变更列表（供账本留痕与回复模型）。"""
    changes: list[dict[str, Any]] = []
    if not isinstance(update, dict):
        return changes
    for key, value in update.items():
        if key not in FIELDS or value is None:
            continue
        if key in LIST_FIELDS:
            if not isinstance(value, list):
                continue
            items = list(dict.fromkeys(str(x).strip() for x in value if str(x).strip()))
            added = [x for x in items if x not in view[key]]
            if not added:
                continue
            view[key].extend(added)
            item_meta = meta.setdefault(key, {}).get("items", {})
            for item in added:
                item_meta[item] = {"turn": turn, "at": ts, "quote": quote}
            meta[key] = {"updated_at": ts, "turn": turn, "quote": quote, "items": item_meta}
            changes.append({"field": key, "kind": "append", "added": added, "turn": turn, "at": ts})
        else:
            new = str(value).strip()
            if not new:
                continue
            old = view.get(key)
            if old == new:
                continue
            view[key] = new
            meta[key] = {"updated_at": ts, "turn": turn, "quote": quote}
            changes.append({"field": key, "old": old, "new": new, "turn": turn, "at": ts,
                            "kind": "set" if old in (None, "") else "overwrite"})
    return changes


def compact(view: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    """给模型看的紧凑档案卡：只带有值的字段 + 最近一次更新时间。"""
    out: dict[str, Any] = {}
    for key in FIELDS:
        value = view.get(key)
        if value in (None, "", []):
            continue
        entry: dict[str, Any] = {"value": value}
        m = meta.get(key) or {}
        if m.get("updated_at"):
            entry["updated_at"] = m["updated_at"]
        out[key] = entry
    return out
