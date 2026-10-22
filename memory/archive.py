"""第 5 层：长期归档 + 向量兜底检索。

默认是零依赖的本地向量：句子级片段 → 字符 n-gram 哈希到 4096 维（停用词过滤 + 同义词扩展）
→ L2 归一化 → 余弦相似度。配置了 OpenAI 兼容的 embedding 接口后自动改用远端向量（失败回退本地）。

为什么分句：整段对话做向量会被长文本稀释（同一句话里既有偏好又有寒暄），
切成句子级片段后，短查询能精确命中「那一句」，召回分数明显更稳。
向量库只负责「模糊召回」，精确字段仍然读第 3 层档案卡。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import urllib.request
from pathlib import Path
from typing import Any, Iterable

DIM = 4096
_WORD = re.compile(r"[a-z0-9]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")

# 高频虚词：出现在 n-gram 里会把相似度冲淡
_STOP = set("的了是我你他她它吗呢吧啊嘛呀哦嗯怎什么多少有没不也就都还和跟与在到从要会能可以这个那个"
            "那这我们你们他们一个一下一些点吧啦哈很太真现在开始然后但是所以因为如果就是其实大概左右")

# 轻量同义词：让「忌口」也能召回「海鲜过敏」（换成真 embedding 后就不需要了）
_SYNONYMS = {
    "预算": ("预算", "费用", "花销", "花费", "多少钱", "价格", "开销"),
    "饮食": ("吃", "忌口", "过敏", "不吃", "饮食", "口味", "海鲜", "素食", "辣"),
    "住宿": ("住", "酒店", "民宿", "住宿", "过夜"),
    "交通": ("开车", "自驾", "高铁", "火车", "飞机", "交通", "出行"),
    "出发地": ("出发", "从哪", "起点", "城市", "哪里出发"),
    "偏好": ("偏好", "喜欢", "爱好", "兴趣", "想要"),
    "时间": ("日期", "哪天", "几天", "时长", "周末"),
}


def _tokens(text: str) -> list[tuple[str, float]]:
    low = (text or "").lower()
    out: list[tuple[str, float]] = [(w, 0.8) for w in _WORD.findall(low)]
    joined = "".join(_CJK.findall(low))
    for i in range(len(joined) - 1):
        gram = joined[i:i + 2]
        if gram[0] in _STOP and gram[1] in _STOP:
            continue
        out.append((gram, 0.55))
    for i in range(len(joined) - 2):
        gram = joined[i:i + 3]
        if all(ch in _STOP for ch in gram):
            continue
        out.append((gram, 1.0))
    for canonical, words in _SYNONYMS.items():
        if any(w in low for w in words):
            out.append((f"SYN:{canonical}", 1.3))
    return out


class HashingEmbedder:
    """零依赖向量：稳定、离线，适合 demo 与没有 embedding 接口的环境。"""
    name = "local-hash-" + str(DIM)
    dim = DIM

    def embed(self, text: str) -> dict[str, float]:
        vec: dict[int, float] = {}
        for token, weight in _tokens(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            idx = int.from_bytes(digest[:4], "little") % DIM
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[idx] = vec.get(idx, 0.0) + sign * weight
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {str(k): round(v / norm, 5) for k, v in vec.items() if abs(v) > 1e-6}


class ApiEmbedder:
    """可选的远端 embedding（OpenAI 兼容 /v1/embeddings）。"""

    def __init__(self, base: str, key: str, model: str) -> None:
        self.base, self.key, self.model = base.rstrip("/"), key, model
        self.name = f"api:{model}"
        self.dim = 0

    def embed(self, text: str) -> dict[str, float]:
        payload = json.dumps({"model": self.model, "input": text[:2000]}).encode()
        req = urllib.request.Request(
            f"{self.base}/v1/embeddings", data=payload, method="POST",
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        vec = data["data"][0]["embedding"]
        self.dim = len(vec)
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return {str(i): round(v / norm, 6) for i, v in enumerate(vec) if abs(v) > 1e-6}


def make_embedder():
    key = os.getenv("EMBEDDING_API_KEY", "").strip()
    model = os.getenv("EMBEDDING_MODEL", "").strip()
    base = (os.getenv("EMBEDDING_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "").strip()
    if key and model and base:
        return ApiEmbedder(base, key, model)
    return HashingEmbedder()


def segments(text: str, limit: int = 120) -> list[str]:
    """把一条对话切成句子级片段，避免长文本稀释向量。"""
    parts = re.split(r"[。！？!?\n；;]+", text or "")
    out: list[str] = []
    for part in parts:
        part = part.strip()
        while len(part) > limit:
            out.append(part[:limit])
            part = part[limit:]
        if part:
            out.append(part)
    return out or [(text or "").strip()]


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(k, 0.0) for k, v in a.items())


class Archive:
    def __init__(self, data_dir: Path, embedder=None) -> None:
        self.dir = data_dir
        self.embedder = embedder or make_embedder()

    def path(self, sid: str) -> Path:
        return self.dir / f"{sid}.archive.jsonl"

    def load(self, sid: str) -> list[dict[str, Any]]:
        p = self.path(sid)
        if not p.exists():
            return []
        out = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and "vec" in item:
                out.append(item)
        return out

    def _write(self, sid: str, entries: Iterable[dict[str, Any]]) -> None:
        with self.path(sid).open("w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")

    def index_events(self, sid: str, events: Iterable[dict[str, Any]]) -> int:
        """把对话事件按句子切片补进索引；向量实现/维度变化时整体重建。"""
        conv = [e for e in events if e.get("kind") in ("user", "assistant") and e.get("text")]
        existing = self.load(sid)
        rebuild = bool(existing) and (
            existing[0].get("embedder") != self.embedder.name
            or (self.embedder.dim and existing[0].get("dim") != self.embedder.dim))
        entries = [] if rebuild else existing
        seen = {e.get("event_id") for e in entries}
        added = 0
        max_entries = int(os.getenv("MEMORY_ARCHIVE_MAX_ENTRIES", "4000"))
        for event in conv:
            if event["id"] in seen or len(entries) >= max_entries:
                continue
            for seg_index, seg in enumerate(segments(event.get("text", ""))):
                if len(entries) >= max_entries:
                    break
                try:
                    vec = self.embedder.embed(seg)
                except Exception:
                    vec = HashingEmbedder().embed(seg)
                entries.append({"event_id": event["id"], "seg": seg_index,
                                "turn": event.get("turn", 0), "ts": event.get("ts"),
                                "role": event["kind"], "text": seg, "vec": vec,
                                "dim": self.embedder.dim, "embedder": self.embedder.name})
                added += 1
        if added or rebuild:
            self._write(sid, entries)
        return added

    def search(self, sid: str, query: str, *, k: int = 3, min_score: float = 0.15,
               weak_min_score: float = 0.09) -> list[dict[str, Any]]:
        entries = self.load(sid)
        if not entries:
            return []
        qvec = self.embedder.embed(query)
        best_by_event: dict[int, tuple[float, dict[str, Any]]] = {}
        for e in entries:
            if e.get("dim") != self.embedder.dim:
                continue
            score = cosine(qvec, e.get("vec") or {})
            score *= 1.08 if e.get("role") == "user" else 0.98  # 用户原话信息密度更高
            if score < weak_min_score:
                continue
            prev = best_by_event.get(e["event_id"])
            if prev is None or score > prev[0]:
                best_by_event[e["event_id"]] = (score, e)
        scored = sorted(best_by_event.values(), key=lambda x: x[0], reverse=True)
        if not scored:
            return []
        best = scored[0][0]
        strong = [(s, e) for s, e in scored if s >= max(min_score, best * 0.55)]
        weak = False
        if not strong:
            strong, weak = [(best, scored[0][1])], True
        return [{"event_id": e["event_id"], "turn": e.get("turn"), "ts": e.get("ts"),
                 "role": e.get("role"), "text": e.get("text"), "score": round(s, 3),
                 "weak": weak}
                for s, e in strong[:k]]

    def stats(self, sid: str) -> dict[str, Any]:
        entries = self.load(sid)
        return {"count": len({e["event_id"] for e in entries}), "segments": len(entries),
                "embedder": self.embedder.name, "dim": self.embedder.dim}
