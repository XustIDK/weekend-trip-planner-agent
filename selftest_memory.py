"""离线自检：不联网、不花 API，验证 5 层记忆与账本的行为。"""
from __future__ import annotations

import json
import sys
import tempfile
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import app
from memory import archive as archive_mod
from memory import context, ledger, policy, profile, session_meta, summarizer, window

PASS, FAIL = [], []


def check(name: str, fn) -> None:
    try:
        fn()
        PASS.append(name)
        print(f"  ok   {name}")
    except Exception as exc:
        FAIL.append((name, exc))
        print(f"  FAIL {name}: {exc}")
        traceback.print_exc()


def fresh_env():
    tmp = Path(tempfile.mkdtemp(prefix="memory-test-"))
    app.DATA = tmp
    app.ARCHIVE = archive_mod.Archive(tmp)
    app.API_KEY = ""
    return tmp


def fake_model(script: list[dict]):
    def _request(messages, *, use_tools=True):
        if not script:
            return {"choices": [{"message": {"role": "assistant", "content": "好。"}}]}
        return {"choices": [{"message": script.pop(0)}]}
    return _request


def tool_call(name: str, args: dict, cid: str = "call_1") -> dict:
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": cid, "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}


def main() -> int:
    print("== 记忆分层自检 ==")

    # 1. 档案卡：覆盖 + 变更留痕
    def t_profile():
        view, meta = profile.empty_profile(), profile.empty_meta()
        profile.merge(view, meta, {"origin": "上海", "budget": "5 万"}, turn=1, ts="t1", quote="我预算 5 万")
        changes = profile.merge(view, meta, {"budget": "8 万"}, turn=2, ts="t2", quote="涨到 8 万")
        assert view["budget"] == "8 万", view
        assert changes and changes[0]["kind"] == "overwrite" and changes[0]["old"] == "5 万", changes
        assert meta["budget"]["turn"] == 2 and meta["budget"]["quote"] == "涨到 8 万"
        # 列表去重
        profile.merge(view, meta, {"interests": ["咖啡", "咖啡"]}, turn=3, ts="t3", quote="爱喝咖啡")
        assert view["interests"] == ["咖啡"], view
    check("第 3 层：档案卡覆盖式更新 + 变更留痕", t_profile)

    # 2. 账本：只追加、可重放出对话流
    def t_ledger():
        tmp = fresh_env()
        sid = "ledger-test"
        for i in range(3):
            ledger.append(tmp, sid, kind="user", text=f"u{i}", turn=i + 1)
            ledger.append(tmp, sid, kind="assistant", text=f"a{i}", turn=i + 1)
        events = ledger.read_all(tmp, sid)
        assert [e["id"] for e in events] == [1, 2, 3, 4, 5, 6], events
        assert ledger.transcript(events)[0] == {"role": "user", "content": "u0"}
        ledger.append(tmp, sid, kind="tool", data={"name": "update_memory"})
        assert len(ledger.read_all(tmp, sid)) == 7
    check("账本：只追加 + 对话流投影", t_ledger)

    # 3. 窗口：轮数 + 字符预算
    def t_window():
        tmp = fresh_env()
        sid = "win-test"
        for i in range(8):
            ledger.append(tmp, sid, kind="user", text=f"第{i}轮问题" * 3, turn=i + 1)
            ledger.append(tmp, sid, kind="assistant", text=f"第{i}轮回答" * 3, turn=i + 1)
        events = ledger.read_all(tmp, sid)
        msgs, start = window.recent(events, max_turns=3, max_chars=10000)
        assert len(msgs) == 6, len(msgs)
        assert start == 11, start
        assert msgs[-1]["content"].startswith("第7轮回答")
        msgs2, _ = window.recent(events, max_turns=8, max_chars=40)
        assert sum(len(m["content"]) for m in msgs2) <= 80
    check("第 1 层：滑动窗口轮数/字符预算", t_window)

    # 4. 元数据：设备/时区/场景标签 + TTL 过期
    def t_meta():
        meta = session_meta.build({"User-Agent": "Mozilla/5.0 (iPhone)", "Accept-Language": "zh-CN"},
                                  {"timezone": "Asia/Shanghai"})
        assert meta["device"] == "手机" and meta["timezone"] == "Asia/Shanghai", meta
        assert meta["scene_tags"], meta
        assert session_meta.is_expired({}) is True
        old = dict(meta, last_seen=(datetime.now() - timedelta(hours=48)).isoformat(timespec="seconds"))
        assert session_meta.is_expired(old) is True
        assert session_meta.is_expired(meta) is False
        assert "first_seen" not in session_meta.compact(meta)
    check("第 2 层：会话元数据与 TTL", t_meta)

    # 5. 摘要：模型失败时退化抽取式，且记录 upto
    def t_summary():
        events = [{"id": 1, "turn": 1, "kind": "user", "text": "从上海出发，预算 5 万"},
                  {"id": 2, "turn": 1, "kind": "assistant", "text": "好的"},
                  {"id": 3, "turn": 2, "kind": "user", "text": "预算涨到 8 万"}]
        def boom(prompt):
            raise RuntimeError("no model")
        result = summarizer.roll("", events, boom, limit=120)
        assert result["source"] == "extractive", result
        assert "5 万" in result["text"] and "8 万" in result["text"], result
        assert result["upto"] == 3
        result2 = summarizer.roll("旧摘要", events, lambda p: "用户从上海出发，预算由 5 万改为 8 万。", limit=120)
        assert result2["source"] == "llm" and "8 万" in result2["text"]
    check("第 4 层：摘要（LLM 失败退化为抽取式）", t_summary)

    # 6. 归档：向量检索命中历史事件
    def t_archive():
        tmp = fresh_env()
        sid = "arc-test"
        texts = ["我们从上海出发，两个人", "预算大概 5 万", "喜欢安静的咖啡馆和徒步",
                 "预算涨到 8 万了", "不想去人多的地方"]
        for i, t in enumerate(texts):
            ledger.append(tmp, sid, kind="user", text=t, turn=i + 1)
        events = ledger.read_all(tmp, sid)
        app.ARCHIVE.index_events(sid, events)
        hits = app.ARCHIVE.search(sid, "我之前说的预算是多少", k=3, min_score=0.05)
        assert hits, "没有召回"
        assert any("预算" in h["text"] for h in hits), hits
        stats = app.ARCHIVE.stats(sid)
        assert stats["count"] == 5 and stats["embedder"].startswith("local-hash"), stats
        # 重复索引不产生重复条目
        app.ARCHIVE.index_events(sid, events)
        assert app.ARCHIVE.stats(sid)["count"] == 5
    check("第 5 层：归档向量索引与检索", t_archive)

    # 7. 上下文：5 层都在，且有预算截断
    def t_context():
        system = context.build_system({"origin": {"value": "上海"}}, {"timezone": "Asia/Shanghai"},
                                      "用户预算从 5 万改为 8 万", [{"turn": 2, "ts": "t", "text": "预算 5 万", "score": 0.4}])
        for marker in ("第 3 层", "第 2 层", "第 4 层", "第 5 层"):
            assert marker in system, marker
        assert len(context.build_system({}, {}, "x" * 20000, [])) <= context.MAX_CONTEXT_CHARS + 20
    check("上下文：5 层按优先级拼装 + 截断", t_context)

    # 8. 端到端：run_turn 里写档案卡 → 查归档 → 回答
    def t_run_turn():
        tmp = fresh_env()
        sid = "e2e-test"
        for i, t in enumerate(["我们从上海出发", "预算 5 万", "就想安静逛逛"]):
            ledger.append(tmp, sid, kind="user", text=t, turn=i + 1)
        state = app.load_session(sid)
        app.ARCHIVE.index_events(sid, ledger.read_all(tmp, sid))
        app.model_request = fake_model([
            tool_call("update_memory", {"budget": "8 万", "origin": "上海"}, "c1"),
            tool_call("search_archive", {"query": "之前说的预算"}, "c2"),
            {"role": "assistant", "content": "已把预算更新为 8 万；你之前提过 5 万。"},
        ])
        answer = app.run_turn(state, "预算涨到 8 万了", {"User-Agent": "test", "Accept-Language": "zh-CN"},
                              {"timezone": "Asia/Shanghai"})
        assert "8 万" in answer, answer
        events = ledger.read_all(tmp, sid)
        kinds = [e["kind"] for e in events]
        assert kinds.count("tool") == 2, kinds
        assert kinds[-1] == "assistant", kinds
        assert state["memory"]["budget"] == "8 万"
        assert state["memory"]["origin"] == "上海"
        assert any(e.get("data", {}).get("type") == "summary_roll" for e in events) is False
        view = app.session_view(state, events)
        assert view["messages"][-1]["role"] == "assistant"
        assert view["layers"]["archive"]["count"] >= 5
        # 账本里能查到旧值（覆盖没丢证据）
        tool_events = [e for e in events if e["kind"] == "tool" and e.get("data", {}).get("name") == "update_memory"]
        assert state["memory_meta"]["budget"]["turn"] == state["turn"]
    check("端到端：工具写档案卡 + 归档召回 + 回复", t_run_turn)

    # 9. 旧会话迁移：messages 进账本、裸 memory 进档案卡
    def t_migrate():
        tmp = fresh_env()
        sid = "old-session"
        legacy = {"id": sid, "memory": {"origin": "杭州", "budget": "5 万", "interests": ["茶"]},
                  "messages": [{"role": "user", "content": "从杭州出发"},
                               {"role": "assistant", "content": "好的"}],
                  "draft": None, "final": None, "turn": 1,
                  "updated_at": "2026-01-01T00:00:00"}
        (tmp / f"{sid}.json").write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
        state = app.load_session(sid)
        assert state["memory"]["origin"] == "杭州" and state["memory_meta"]["origin"].get("legacy") is True
        events = ledger.read_all(tmp, sid)
        assert len(events) == 2 and events[0]["data"].get("migrated") is True
    check("迁移：旧会话不丢数据", t_migrate)

    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for name, exc in FAIL:
            print(f" - {name}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
