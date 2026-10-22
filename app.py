"""Meituan-style weekend planner: a small tool-using conversational agent.

记忆按笔记的「4 层核心 + 1 层向量兜底」实现：
  账本 Ledger   data/<sid>.events.jsonl  只追加，是其它所有视图的底稿
  第 1 层 窗口   memory/window.py        最近几轮原文
  第 2 层 元数据 memory/session_meta.py  时区/设备/场景标签，用完即弃
  第 3 层 档案卡 memory/profile.py       结构化字段 + 字段级时间与出处
  第 4 层 摘要   memory/summarizer.py    滚动摘要（有损，原文仍在账本）
  第 5 层 归档   memory/archive.py       向量兜底检索（本地哈希向量，零依赖）
策略 Policy     memory/policy.py        何时检索 / 何时摘要 / 何时遗忘
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import threading
import urllib.error
import urllib.request
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from memory import archive as archive_mod
from memory import context as context_mod
from memory import ledger, policy, profile, session_meta, summarizer, window

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(parents=True, exist_ok=True)
LOCK = threading.Lock()
STATE_VERSION = 3


def setup_console() -> None:
    """让 Windows 控制台按 UTF-8 输出中文，避免 GBK 编码报错。"""
    os.environ.setdefault("PYTHONUTF8", "1")
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def load_local_config() -> None:
    """Load project .env values without overriding variables set by the shell."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    # utf-8-sig：兼容记事本/PowerShell 生成的带 BOM 的 .env
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


load_local_config()
API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
if API_KEY in {"your_api_key_here", "sk-xxx", "sk-your-key"}:
    API_KEY = ""
API_BASE = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com").rstrip("/")
MODEL = os.getenv("MODEL_NAME", "deepseek-chat")
ARCHIVE = archive_mod.Archive(DATA)


def make_ssl_context() -> ssl.SSLContext:
    """创建 TLS 上下文：Windows 默认读取系统证书存储，失败时回退到 certifi（若已安装）。"""
    context = ssl.create_default_context()
    if sys.platform == "win32":
        try:
            import certifi  # 可选依赖，未安装时跳过
            context.load_verify_locations(cafile=certifi.where())
        except Exception:
            pass
    return context


SSL_CONTEXT = make_ssl_context()

TOOLS = [
    {"type": "function", "function": {"name": "update_memory", "description": "把用户明确说出的信息写入本会话档案卡。只更新已知字段，不要猜测。", "parameters": {
        "type": "object", "properties": {
            "origin": {"type": ["string", "null"]}, "dates": {"type": ["string", "null"]},
            "duration": {"type": ["string", "null"]}, "budget": {"type": ["string", "null"]},
            "budget_cny": {"type": ["integer", "null"], "description": "总预算折算成整数人民币，例如 80000"},
            "party": {"type": ["string", "null"]}, "interests": {"type": "array", "items": {"type": "string"}},
            "food_preferences": {"type": "array", "items": {"type": "string"}}, "transport": {"type": ["string", "null"]},
            "pace": {"type": ["string", "null"]}, "constraints": {"type": "array", "items": {"type": "string"}},
            "confirmed": {"type": "array", "items": {"type": "string"}}, "open_questions": {"type": "array", "items": {"type": "string"}}
        }, "additionalProperties": False}},},
    {"type": "function", "function": {"name": "save_draft", "description": "保存本轮行程草案，供用户逐项调整。", "parameters": {
        "type": "object", "properties": {"title": {"type": "string"}, "summary": {"type": "string"},
            "days": {"type": "array", "items": {"type": "object", "properties": {
                "date": {"type": "string"}, "items": {"type": "array", "items": {"type": "object", "properties": {
                    "time": {"type": "string"}, "name": {"type": "string"}, "kind": {"type": "string"},
                    "detail": {"type": "string"}, "cost": {"type": "string"}}, "required": ["time", "name", "kind", "detail", "cost"], "additionalProperties": False}}},
                "required": ["date", "items"], "additionalProperties": False}}, "estimated_total": {"type": "string"},
                "budget_note": {"type": "string"}}, "required": ["title", "summary", "days", "estimated_total", "budget_note"], "additionalProperties": False}},},
    {"type": "function", "function": {"name": "finalize_plan", "description": "用户明确要求收敛/最终版后，保存唯一最终行程。", "parameters": {
        "type": "object", "properties": {"title": {"type": "string"}, "summary": {"type": "string"},
            "days": {"type": "array", "items": {"type": "object", "properties": {
                "date": {"type": "string"}, "items": {"type": "array", "items": {"type": "object", "properties": {
                    "time": {"type": "string"}, "name": {"type": "string"}, "kind": {"type": "string"},
                    "detail": {"type": "string"}, "cost": {"type": "string"}}, "required": ["time", "name", "kind", "detail", "cost"], "additionalProperties": False}}},
                "required": ["date", "items"], "additionalProperties": False}}, "estimated_total": {"type": "string"},
                "budget_note": {"type": "string"}}, "required": ["title", "summary", "days", "estimated_total", "budget_note"], "additionalProperties": False}},},
    {"type": "function", "function": {"name": "search_archive", "description": "在长期归档（全量历史）里做向量检索。当用户问到过去（上次/以前/最早）、或当前档案卡与摘要答不上来时使用；返回的片段只是线索。", "parameters": {
        "type": "object", "properties": {"query": {"type": "string"}, "k": {"type": "integer"}},
        "required": ["query"], "additionalProperties": False}},},
]


# ---------- 会话状态（视图） ----------

def new_session(sid: str | None = None) -> dict[str, Any]:
    return {
        "id": sid or str(uuid.uuid4()),
        "version": STATE_VERSION,
        "memory": profile.empty_profile(),
        "memory_meta": profile.empty_meta(),
        "meta": {},
        "summary": {"text": "", "upto": 0, "source": "none", "updated_at": None},
        "last_retrieval": {},
        "draft": None,
        "final": None,
        "turn": 0,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def load_session(sid: str) -> dict[str, Any]:
    path = DATA / f"{sid}.json"
    state = new_session(sid)
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = None
        if isinstance(raw, dict):
            state["id"] = raw.get("id") or sid
            state["memory"], state["memory_meta"] = (
                (raw["memory"], raw.get("memory_meta") or {})
                if raw.get("memory_meta") is not None or raw.get("version") == STATE_VERSION
                else profile.migrate(raw.get("memory")))
            state["meta"] = raw.get("meta") or {}
            state["summary"] = raw.get("summary") or state["summary"]
            state["last_retrieval"] = raw.get("last_retrieval") or {}
            state["draft"], state["final"] = raw.get("draft"), raw.get("final")
            state["turn"] = int(raw.get("turn") or 0)
            # 旧会话：把 messages 迁进账本，一条不丢
            if raw.get("messages") and not ledger.read_all(DATA, sid):
                for i, m in enumerate(raw["messages"]):
                    ledger.append(DATA, sid, kind=m.get("role", "user"),
                                  text=str(m.get("content", "")), turn=i // 2 + 1,
                                  data={"migrated": True})
    if session_meta.is_expired(state.get("meta") or {}):
        state["meta"] = {}  # 第 2 层：会话结束即作废
    return state


def save_session(state: dict[str, Any]) -> None:
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    state["version"] = STATE_VERSION
    payload = {k: v for k, v in state.items() if k != "messages"}
    (DATA / f"{state['id']}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def session_view(state: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """给前端的视图：对话流从账本投影，记忆分层信息一并返回。"""
    view = {k: v for k, v in state.items() if k != "messages"}
    view["messages"] = ledger.transcript(events)
    view["layers"] = {
        "window_turns": policy.WINDOW_TURNS,
        "summary_chars": len((state.get("summary") or {}).get("text") or ""),
        "archive": ARCHIVE.stats(state["id"]),
        "ledger_events": len(events),
        "last_retrieval": state.get("last_retrieval") or {},
    }
    return view


# ---------- 模型调用 ----------

def model_request(messages: list[dict[str, Any]], *, use_tools: bool = True) -> dict[str, Any]:
    if not API_KEY:
        raise RuntimeError("尚未配置模型 API Key。请复制 .env.example 的配置并设置 OPENAI_API_KEY。")
    body: dict[str, Any] = {"model": MODEL, "messages": messages, "temperature": 0.4}
    if use_tools:
        body.update({"tools": TOOLS, "tool_choice": "auto"})
    payload = json.dumps(body).encode()
    req = urllib.request.Request(f"{API_BASE}/v1/chat/completions", data=payload,
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90, context=SSL_CONTEXT) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:800]
        raise RuntimeError(f"模型服务返回 HTTP {exc.code}: {detail}") from exc


def plain_complete(prompt: str) -> str:
    """无工具的一次模型调用，用于第 4 层摘要。"""
    data = model_request([{"role": "user", "content": prompt}], use_tools=False)
    return data["choices"][0]["message"].get("content") or ""


# ---------- 一轮对话 ----------

def _execute_tool(state: dict[str, Any], name: str, args: dict[str, Any], *,
                  turn: int, quote: str) -> Any:
    ts = ledger.now_iso()
    if name == "update_memory":
        changes = profile.merge(state["memory"], state["memory_meta"], args,
                                turn=turn, ts=ts, quote=quote)
        ledger.append(DATA, state["id"], kind="tool", turn=turn,
                      data={"name": name, "args": args, "changes": changes})
        return {"saved": True, "changes": changes, "memory": state["memory"]}
    if name == "save_draft":
        state["draft"] = args
        state["final"] = None  # 新草案取代旧定稿（覆盖只发生在视图，账本里仍有定稿记录）
        ledger.append(DATA, state["id"], kind="tool", turn=turn, data={"name": name, "title": args.get("title")})
        return {"saved": True, "draft": args}
    if name == "finalize_plan":
        state["final"] = args
        state["draft"] = args
        ledger.append(DATA, state["id"], kind="tool", turn=turn, data={"name": name, "title": args.get("title")})
        return {"finalized": True, "plan": args}
    if name == "search_archive":
        query = str(args.get("query") or "").strip()
        k = int(args.get("k") or policy.ARCHIVE_TOP_K)
        hits = ARCHIVE.search(state["id"], query, k=min(max(k, 1), 8),
                              min_score=policy.ARCHIVE_MIN_SCORE,
                              weak_min_score=policy.ARCHIVE_WEAK_MIN_SCORE)
        state["last_retrieval"] = {"query": query, "hits": len(hits), "source": "tool",
                                   "at": ledger.now_iso(),
                                   "top": (hits[0]["text"][:40] if hits else None)}
        ledger.append(DATA, state["id"], kind="tool", turn=turn,
                      data={"name": name, "query": query, "hits": [h["event_id"] for h in hits]})
        return {"query": query, "hits": hits, "note": "向量召回结果，可能不准，只能当线索"}
    return {"error": "unknown tool"}


def _auto_hits(state: dict[str, Any], user_text: str) -> list[dict[str, Any]]:
    """第 5 层兜底：用户问到过去时，先自动召回想一遍（结果只标为线索）。"""
    if not policy.should_retrieve(user_text):
        return []
    query = policy.retrieval_query(user_text)
    hits = ARCHIVE.search(state["id"], query, k=policy.ARCHIVE_TOP_K,
                          min_score=policy.ARCHIVE_MIN_SCORE,
                          weak_min_score=policy.ARCHIVE_WEAK_MIN_SCORE)
    state["last_retrieval"] = {"query": query, "hits": len(hits), "source": "auto",
                               "at": ledger.now_iso(),
                               "top": (hits[0]["text"][:40] if hits else None)}
    return hits


def _maybe_roll_summary(state: dict[str, Any]) -> None:
    events = ledger.read_all(DATA, state["id"])
    _, start_id = window.recent(events, max_turns=policy.WINDOW_TURNS, max_chars=policy.WINDOW_CHARS)
    if not start_id:
        return
    upto = int((state.get("summary") or {}).get("upto") or 0)
    spilled = [e for e in ledger.conversation(events) if upto < e["id"] < start_id]
    if not policy.should_roll_summary(len(spilled)):
        return
    if not API_KEY:  # 没有模型时退化为抽取式摘要
        result = {"text": summarizer.fallback((state.get("summary") or {}).get("text", ""), spilled,
                                              policy.SUMMARY_MAX_CHARS),
                  "source": "extractive", "upto": spilled[-1]["id"]}
    else:
        result = summarizer.roll((state.get("summary") or {}).get("text", ""), spilled,
                                 plain_complete, limit=policy.SUMMARY_MAX_CHARS)
    state["summary"] = {"text": result["text"], "upto": result["upto"],
                        "source": result["source"], "updated_at": ledger.now_iso()}
    ledger.append(DATA, state["id"], kind="note", turn=state["turn"],
                  data={"type": "summary_roll", "source": result["source"], "upto": result["upto"]})


def run_turn(state: dict[str, Any], user_text: str, headers: dict[str, str],
             payload: dict[str, Any] | None = None) -> str:
    sid = state["id"]
    state["turn"] += 1
    turn = state["turn"]

    # 第 2 层：元数据每轮刷新，但不覆盖首次进入时间
    prev_meta = state.get("meta") or {}
    meta = session_meta.build(headers, payload or {}, ttl_hours=policy.META_TTL_HOURS)
    if prev_meta.get("first_seen"):
        meta["first_seen"] = prev_meta["first_seen"]
    state["meta"] = meta

    events = ledger.read_all(DATA, sid)
    ledger.append(DATA, sid, kind="user", text=user_text, turn=turn,
                  data={"tz": meta.get("timezone"), "device": meta.get("device")})

    # 第 1 层：窗口 = 本轮之前的最近几轮原文
    window_msgs, _ = window.recent(events, max_turns=policy.WINDOW_TURNS,
                                  max_chars=policy.WINDOW_CHARS)
    # 第 5 层：问到「过去」时先兜底召回
    hits = _auto_hits(state, user_text)
    system = context_mod.build_system(
        profile.compact(state["memory"], state["memory_meta"]),
        session_meta.compact(meta),
        (state.get("summary") or {}).get("text", ""),
        hits,
    )
    msgs: list[dict[str, Any]] = [{"role": "system", "content": system}]
    msgs.extend(window_msgs)
    msgs.append({"role": "user", "content": user_text})

    for _ in range(6):
        response = model_request(msgs)
        choice = response["choices"][0]["message"]
        msgs.append(choice)
        calls = choice.get("tool_calls") or []
        if not calls:
            answer = choice.get("content") or "我已记录这轮信息。你希望先调整哪一部分？"
            ledger.append(DATA, sid, kind="assistant", text=answer, turn=turn)
            ARCHIVE.index_events(sid, ledger.read_all(DATA, sid))
            _maybe_roll_summary(state)
            save_session(state)
            return answer
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = _execute_tool(state, name, args, turn=turn,
                                   quote=profile.quote_of(user_text))
            msgs.append({"role": "tool", "tool_call_id": call["id"],
                         "content": json.dumps(result, ensure_ascii=False)})
    raise RuntimeError("本轮工具调用次数过多，请把修改要求拆成一条消息再试。")


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, data: Any) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def _headers(self) -> dict[str, str]:
        return {k: v for k, v in self.headers.items()}

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            raw = (ROOT / "static" / "index.html").read_bytes()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
        elif path.startswith("/api/session/"):
            sid = path.rsplit("/", 1)[-1]
            if not sid.replace("-", "").isalnum(): return self._json(400, {"error": "invalid session id"})
            with LOCK:
                state = load_session(sid)
                events = ledger.read_all(DATA, sid)
            self._json(200, session_view(state, events))
        elif path == "/api/health":
            self._json(200, {"ok": True, "model_configured": bool(API_KEY), "model": MODEL,
                             "memory": {"window_turns": policy.WINDOW_TURNS,
                                        "summary_max_chars": policy.SUMMARY_MAX_CHARS,
                                        "archive": ARCHIVE.stats("__probe__")}})
        else: self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        try:
            size = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(size) or b"{}")
            if self.path == "/api/session":
                state = new_session()
                with LOCK:
                    save_session(state)
                    ledger.append(DATA, state["id"], kind="session",
                                  data={"created": state["updated_at"]})
                return self._json(200, session_view(state, []))
            if self.path != "/api/chat": return self._json(404, {"error": "not found"})
            sid, message = str(data.get("session_id", "")), str(data.get("message", "")).strip()
            if not sid or not message: return self._json(400, {"error": "session_id and message are required"})
            if len(message) > 3000: return self._json(400, {"error": "message is too long"})
            with LOCK:
                state = load_session(sid)
                answer = run_turn(state, message, self._headers(), data)
                events = ledger.read_all(DATA, sid)
            self._json(200, {"session": session_view(state, events), "reply": answer})
        except RuntimeError as exc: self._json(503, {"error": str(exc)})
        except (ValueError, KeyError, json.JSONDecodeError) as exc: self._json(400, {"error": f"请求格式错误：{exc}"})
        except Exception as exc: self._json(500, {"error": f"服务处理失败：{exc}"})

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")


def main() -> int:
    host = os.getenv("HOST", "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(os.getenv("PORT", "8765"))
    except ValueError:
        print("配置错误：PORT 必须是数字，请在 .env 中修正后重试。")
        return 2

    try:
        server = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        print(f"启动失败：无法绑定 {host}:{port}（{exc}）。")
        print("端口可能已被占用，请在 .env 中修改 PORT（例如 8766）后重试。")
        return 1

    url = f"http://{host}:{port}"
    print("=" * 56)
    print("周末去哪儿 · 行程规划助手")
    print(f"服务地址: {url}")
    print(f"模型配置: {'已就绪（' + MODEL + '）' if API_KEY else '未配置，请编辑 .env 中的 OPENAI_API_KEY'}")
    print(f"记忆分层: 窗口{policy.WINDOW_TURNS}轮 / 档案卡 / 摘要 / 归档向量({ARCHIVE.embedder.name})")
    print("按 Ctrl+C 停止服务")
    print("=" * 56)

    if os.getenv("OPEN_BROWSER", "1") not in {"0", "false", "False"}:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止服务……")
    finally:
        server.server_close()
    print("服务已停止。")
    return 0


if __name__ == "__main__":
    setup_console()
    sys.exit(main())
