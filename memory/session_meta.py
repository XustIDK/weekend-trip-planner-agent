"""第 2 层：会话元数据。时区、设备、场景标签，用完即弃，不进长期库。"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Mapping

DEFAULT_TTL_HOURS = 24
_MOBILE = re.compile(r"Mobile|Android|iPhone|iPad|HarmonyOS", re.I)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def build(headers: Mapping[str, str], payload: dict[str, Any] | None = None, *,
          ttl_hours: int = DEFAULT_TTL_HOURS) -> dict[str, Any]:
    payload = payload or {}
    ua = str(headers.get("User-Agent", ""))
    timezone = str(payload.get("timezone") or headers.get("X-Timezone") or "Asia/Shanghai")
    locale = str(headers.get("Accept-Language", "zh-CN")).split(",")[0] or "zh-CN"
    now = datetime.now()
    tags = ["周末" if now.weekday() >= 5 else "工作日"]
    tags.append("手机端" if _MOBILE.search(ua) else "桌面端")
    return {
        "timezone": timezone,
        "device": "手机" if _MOBILE.search(ua) else "桌面",
        "locale": locale,
        "scene_tags": tags,
        "local_time": now.strftime("%Y-%m-%d %H:%M"),
        "first_seen": now_iso(),
        "last_seen": now_iso(),
        "ttl_hours": ttl_hours,
    }


def touch(meta: dict[str, Any]) -> dict[str, Any]:
    meta = dict(meta or {})
    meta["last_seen"] = now_iso()
    return meta


def is_expired(meta: dict[str, Any]) -> bool:
    """超过 TTL 未活动，视为会话结束，元数据作废（不进长期库）。"""
    last = (meta or {}).get("last_seen")
    if not last:
        return True
    try:
        seen = datetime.fromisoformat(last)
    except ValueError:
        return True
    hours = float(meta.get("ttl_hours") or DEFAULT_TTL_HOURS)
    return (datetime.now() - seen).total_seconds() > hours * 3600


def drop_volatile(meta: dict[str, Any]) -> dict[str, Any]:
    """过期清理：只清元数据，档案卡与账本不受影响。"""
    return {}


def compact(meta: dict[str, Any]) -> dict[str, Any]:
    """给模型看的元数据：只保留有用、且不敏感的几项。"""
    if not meta:
        return {}
    keys = ("timezone", "device", "scene_tags", "local_time")
    return {k: meta[k] for k in keys if meta.get(k)}
