"""Meituan-style weekend planner: a small tool-using conversational agent."""
from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
LOCK = threading.Lock()


def load_local_config() -> None:
    """Load project .env values without overriding variables set by the shell."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


load_local_config()
API_KEY = os.getenv("OPENAI_API_KEY", "")
API_BASE = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com").rstrip("/")
MODEL = os.getenv("MODEL_NAME", "deepseek-chat")


def make_ssl_context() -> ssl.SSLContext:
    """Use platform roots, including macOS Keychain roots, while keeping TLS verification on."""
    context = ssl.create_default_context()
    if sys.platform == "darwin" and not os.getenv("SSL_CERT_FILE"):
        try:
            roots = subprocess.run(
                ["/usr/bin/security", "find-certificate", "-a", "-p",
                 "/System/Library/Keychains/SystemRootCertificates.keychain",
                 "/Library/Keychains/System.keychain"],
                check=True, capture_output=True, text=True, timeout=10,
            ).stdout
            if roots.strip():
                context.load_verify_locations(cadata=roots)
        except (OSError, subprocess.SubprocessError, ssl.SSLError):
            pass
    return context


SSL_CONTEXT = make_ssl_context()

EMPTY_MEMORY = {
    "origin": None, "dates": None, "duration": None, "budget": None,
    "party": None, "interests": [], "food_preferences": [], "transport": None,
    "pace": None, "constraints": [], "confirmed": [], "open_questions": [],
}

TOOLS = [
    {"type": "function", "function": {"name": "update_memory", "description": "把用户明确说出的信息写入本会话记忆。只更新已知字段，不要猜测。", "parameters": {
        "type": "object", "properties": {
            "origin": {"type": ["string", "null"]}, "dates": {"type": ["string", "null"]},
            "duration": {"type": ["string", "null"]}, "budget": {"type": ["string", "null"]},
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
]

SYSTEM = """你是美团周末规划助手，负责通过多轮对话和用户一起规划一趟可执行的周末行程。
每条用户消息都要先抽取明确的新信息并调用 update_memory；不要丢掉已有记忆。先问关键缺口（出发地、日期/时长、总预算、同行人、兴趣），一次最多问两个最影响规划的问题。信息够后用 save_draft 提供一份便于修改的行程，不要同时列多个完整备选方案。用户提出修改时，保留未要求改变的已确认内容并生成更新草案。只有用户明确说“最终版/定稿/就按这个/生成最终方案”等，才调用 finalize_plan；如果信息明显不足或草案不存在，先追问/补草案。预算按总预算理解，标出估算范围和不确定项，不编造实时价格、营业时间或订位情况；提醒核验具体商户信息。对话语气简洁、具体、温和。
工具调用后向用户解释本轮记住了什么、下一步是什么。"""


def load_session(sid: str) -> dict[str, Any]:
    path = DATA / f"{sid}.json"
    if path.exists():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else new_session(sid)
        except (OSError, json.JSONDecodeError):
            pass
    return new_session(sid)


def new_session(sid: str | None = None) -> dict[str, Any]:
    return {"id": sid or str(uuid.uuid4()), "memory": dict(EMPTY_MEMORY), "messages": [], "draft": None,
            "final": None, "turn": 0, "updated_at": datetime.now().isoformat(timespec="seconds")}


def save_session(state: dict[str, Any]) -> None:
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    (DATA / f"{state['id']}.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def model_request(messages: list[dict[str, Any]]) -> dict[str, Any]:
    if not API_KEY:
        raise RuntimeError("尚未配置模型 API Key。请复制 .env.example 的配置并设置 OPENAI_API_KEY。")
    payload = json.dumps({"model": MODEL, "messages": messages, "tools": TOOLS, "tool_choice": "auto", "temperature": 0.4}).encode()
    req = urllib.request.Request(f"{API_BASE}/v1/chat/completions", data=payload,
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90, context=SSL_CONTEXT) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:800]
        raise RuntimeError(f"模型服务返回 HTTP {exc.code}: {detail}") from exc


def merge_memory(memory: dict[str, Any], update: dict[str, Any]) -> None:
    for key, value in update.items():
        if key not in EMPTY_MEMORY or value is None:
            continue
        if isinstance(value, list):
            existing = memory.get(key, [])
            memory[key] = list(dict.fromkeys(existing + value))
        else:
            memory[key] = value


def run_turn(state: dict[str, Any], user_text: str) -> str:
    state["turn"] += 1
    state["final"] = None
    state["messages"].append({"role": "user", "content": user_text})
    msgs = [{"role": "system", "content": SYSTEM + "\n\n当前会话记忆：" + json.dumps(state["memory"], ensure_ascii=False)
             + "\n\n当前草案：" + json.dumps(state["draft"], ensure_ascii=False)
             + "\n当前是第 " + str(state["turn"]) + " 轮。"}] + state["messages"][-24:]
    for _ in range(6):
        response = model_request(msgs)
        choice = response["choices"][0]["message"]
        msgs.append(choice)
        calls = choice.get("tool_calls") or []
        if not calls:
            answer = choice.get("content") or "我已记录这轮信息。你希望先调整哪一部分？"
            state["messages"].append({"role": "assistant", "content": answer})
            return answer
        for call in calls:
            name = call["function"]["name"]
            args = json.loads(call["function"].get("arguments") or "{}")
            if name == "update_memory":
                merge_memory(state["memory"], args)
                result: Any = {"saved": True, "memory": state["memory"]}
            elif name == "save_draft":
                state["draft"] = args
                result = {"saved": True, "draft": args}
            elif name == "finalize_plan":
                state["final"] = args
                state["draft"] = args
                result = {"finalized": True, "plan": args}
            else:
                result = {"error": "unknown tool"}
            msgs.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result, ensure_ascii=False)})
    raise RuntimeError("本轮工具调用次数过多，请把修改要求拆成一条消息再试。")


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

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            raw = (ROOT / "static" / "index.html").read_bytes()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
        elif path.startswith("/api/session/"):
            sid = path.rsplit("/", 1)[-1]
            if not sid.replace("-", "").isalnum(): return self._json(400, {"error": "invalid session id"})
            with LOCK: state = load_session(sid)
            self._json(200, state)
        elif path == "/api/health": self._json(200, {"ok": True, "model_configured": bool(API_KEY), "model": MODEL})
        else: self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        try:
            size = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(size) or b"{}")
            if self.path == "/api/session":
                state = new_session()
                with LOCK: save_session(state)
                return self._json(200, state)
            if self.path != "/api/chat": return self._json(404, {"error": "not found"})
            sid, message = str(data.get("session_id", "")), str(data.get("message", "")).strip()
            if not sid or not message: return self._json(400, {"error": "session_id and message are required"})
            if len(message) > 3000: return self._json(400, {"error": "message is too long"})
            with LOCK:
                state = load_session(sid)
                answer = run_turn(state, message)
                save_session(state)
            self._json(200, {"session": state, "reply": answer})
        except RuntimeError as exc: self._json(503, {"error": str(exc)})
        except (ValueError, KeyError, json.JSONDecodeError) as exc: self._json(400, {"error": f"请求格式错误：{exc}"})
        except Exception as exc: self._json(500, {"error": f"服务处理失败：{exc}"})

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")


if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8765"))
    print(f"Weekend planner: http://{host}:{port} (model configured: {bool(API_KEY)})")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
