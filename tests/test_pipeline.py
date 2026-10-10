"""Офлайн-тесты (без сети и API): /trash, кэш скоринга и дедупликация, бюджет токенов,
таймаут цикла, трейс.

  python tests/test_pipeline.py
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import anthropic  # noqa: E402

import core  # noqa: E402
import digest  # noqa: E402
import filter as heuristics  # noqa: E402
import run  # noqa: E402
import scorer  # noqa: E402
import tg_commands  # noqa: E402

OWNER = "1000"


def make_cfg(tmp: str) -> dict:
    cfg = copy.deepcopy(core.load_config())
    cfg["paths"] = {"db": f"{tmp}/t.db", "logs": f"{tmp}/logs", "beacon": f"{tmp}/gme.ts", "lock": f"{tmp}/run.lock"}
    return cfg


def add_item(conn, item_id: str, title: str = "Insider buys shares, Form 4 filed", kind: str = "reddit",
             sub: str = "GME", text: str = "Some text with facts and a link to sec.gov, long enough.", **cols) -> None:
    core.upsert_item(conn, {"id": item_id, "kind": kind, "source": f"r/{sub}", "bias": "бычий сабреддит",
                            "title": title, "url": f"https://reddit.com/{item_id}", "text": text,
                            "created_utc": core.iso(), "meta": {"sub": sub, "rank": 0, "ups": None, "link": ""}})
    cols.setdefault("passed_filter", 1)
    for col, value in cols.items():
        conn.execute(f"UPDATE items SET {col}=? WHERE id=?", (value, item_id))
    conn.commit()


class FakeClient:
    def __init__(self):
        self.calls = 0
        self.messages = self
        self.beta = types.SimpleNamespace(messages=self)

    def create(self, **kw):
        self.calls += 1
        ids = [line.split('"')[1] for line in kw["messages"][0]["content"].splitlines() if line.startswith("<item id=")]
        items = [{"id": i, "injection_suspected": False, "score": 7, "category": "событие", "keep": True, "event_type": "other",
                  "summary_ru": "Саммари."} for i in ids]
        return types.SimpleNamespace(
            stop_reason="end_turn", usage=types.SimpleNamespace(input_tokens=1000, output_tokens=200),
            content=[types.SimpleNamespace(type="text", text=json.dumps({"items": items}, ensure_ascii=False))])


def fake_api() -> FakeClient:
    client = FakeClient()
    anthropic.Anthropic = lambda **kw: client
    return client


def capture_telegram(module) -> list[dict]:
    """Подменяет send_telegram в модуле: ничего не шлёт, выдаёт id 101, 102, …"""
    sent: list[dict] = []

    def send(cfg, text, silent=False, reply_to=None):
        sent.append({"id": 101 + len(sent), "text": text, "silent": silent, "reply_to": reply_to})
        return [sent[-1]["id"]]
    module.send_telegram = send
    return sent


def update(text: str, message_id: int, reply_to: int | None = None, chat: str = OWNER) -> dict:
    msg = {"message_id": message_id, "chat": {"id": int(chat)}, "text": text}
    if reply_to:
        msg["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": message_id, "message": msg}


# ---------------- 2.2 /trash ----------------

def test_trash_flow():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cfg = make_cfg(tmp)
        conn = core.connect_db(cfg)
        add_item(conn, "reddit:good", scored_at=core.iso(), score=8, category="событие", summary="s", score_status="ok")
        add_item(conn, "reddit:meh", title="Borderline post that slipped through", scored_at=core.iso(), score=5,
                 category="анализ", summary="s", score_status="ok")
        sent = capture_telegram(digest)
        assert digest.run(conn, cfg, None) is not None
        # шапка + по сообщению на пост; посты без звука
        assert len(sent) == 3 and not sent[0]["silent"] and sent[1]["silent"] and sent[2]["silent"]
        mapping = {r["message_id"]: r["item_id"] for r in conn.execute("SELECT * FROM tg_messages")}
        assert mapping == {102: "reddit:good", 103: "reddit:meh"}

        os.environ["GME_TG_CHAT_ID"] = OWNER
        tg_commands.CANDIDATES = Path(tmp) / "candidates"
        replies = capture_telegram(tg_commands)
        tg_commands.fetch_updates = lambda conn: [
            update("/trash", 201, reply_to=103, chat="666"),   # чужой чат — игнор
            update("/trash", 202),                             # без ответа — подсказка
            update("/trash", 203, reply_to=101),               # ответ на шапку — нельзя
            update("/trash@gme_bot", 204, reply_to=103),       # корректная команда
            update("просто текст", 205),                       # не команда
        ]
        assert tg_commands.run(conn, cfg) == 3
        labels = {r["id"]: r["user_label"] for r in conn.execute("SELECT id, user_label FROM items")}
        assert labels == {"reddit:good": None, "reddit:meh": "trash"}
        files = list((Path(tmp) / "candidates").glob("*.json"))
        assert len(files) == 1
        cand = json.loads(files[0].read_text(encoding="utf-8"))
        assert cand["label"] == "junk" and cand["id"] == "eval:reddit:meh" and cand["model_verdict"]["score"] == 5
        assert "ответом" in replies[0]["text"] and "не пост" in replies[1]["text"] and "🗑" in replies[2]["text"]
        conn.close()


# ---------------- 2.3 кэш и дедупликация ----------------

def test_scored_once_and_cache_counter():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cfg = make_cfg(tmp)
        conn = core.connect_db(cfg)
        add_item(conn, "reddit:a", title="Ryan Cohen files Form 4 for 450,000 shares")
        add_item(conn, "reddit:b", title="Completely different post about warrants expiry")
        client = fake_api()
        assert scorer.run(conn, cfg) == (2, None) and client.calls == 1
        assert scorer.run(conn, cfg) == (0, None) and client.calls == 1  # второй прогон токены не тратит

        # как в жизни: оба оценены вчера, с тех пор не встречались
        conn.execute("UPDATE items SET scored_at=?, last_seen=?",
                     ("2020-01-02T00:00:00+00:00", "2020-01-01T00:00:00+00:00"))
        add_item(conn, "reddit:a", title="Ryan Cohen files Form 4 for 450,000 shares")  # встретился при сборе снова
        assert scorer.run(conn, cfg) == (0, None) and client.calls == 1
        assert digest.day_stats(conn)["cache_skipped"] == 1
        conn.close()


def test_reddit_crosspost_is_duplicate():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cfg = make_cfg(tmp)
        conn = core.connect_db(cfg)
        title = "GameStop director buys 10,462 shares — Form 4"
        add_item(conn, "reddit:first", title=title, sub="GME", passed_filter=None)
        add_item(conn, "reddit:xpost", title=title, sub="Superstonk", passed_filter=None)
        add_item(conn, "reddit:other", title="Unrelated analysis of convertible notes", sub="GME", passed_filter=None)
        counts = heuristics.filter_reddit(conn, cfg)
        rows = {r["id"]: (r["passed_filter"], r["filter_reason"]) for r in conn.execute("SELECT * FROM items")}
        assert rows["reddit:first"] == (1, "ok") and rows["reddit:other"] == (1, "ok")
        assert rows["reddit:xpost"] == (0, "duplicate") and counts["duplicates"] == 1
        client = fake_api()
        assert scorer.run(conn, cfg)[0] == 2 and client.calls == 1  # дубль в скоринг не пошёл
        conn.close()


# ---------------- 3.1 бюджет ----------------

def test_token_budget_stops_scoring():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cfg = make_cfg(tmp)
        cfg["budget"]["max_tokens_per_day"] = 1500
        conn = core.connect_db(cfg)
        add_item(conn, "reddit:a")
        client = fake_api()
        assert scorer.run(conn, cfg) == (1, None)  # 1200 токенов — в пределах бюджета
        assert core.tokens_today(conn) == 1200
        add_item(conn, "reddit:b", title="Another distinct post about earnings date")
        core.record_usage(conn, cfg, "eval", "claude-sonnet-5-5", "x", 400, 0)  # evals тоже в бюджете
        n, err = scorer.run(conn, cfg)
        assert n == 0 and err.startswith(scorer.BUDGET_PREFIX) and client.calls == 1  # запроса к API не было
        row = conn.execute("SELECT scored_at FROM items WHERE id='reddit:b'").fetchone()
        assert row["scored_at"] is None  # материал ждёт завтрашнего батча, а не помечен «не оценён»

        alerts_sent: list[str] = []
        run.alerts.notify_once = lambda conn, cfg, key, text: alerts_sent.append(key)
        capture_telegram(digest)
        run.run_digest(cfg, force=True)
        assert len(alerts_sent) == 1 and alerts_sent[0].startswith("budget:")
        trace = json.loads(conn.execute("SELECT data FROM cycles WHERE kind='digest'").fetchone()["data"])
        assert trace["scoring_error"].startswith(scorer.BUDGET_PREFIX) and trace["sent"] is True
        assert trace["tokens_today"] == 1600
        conn.close()


def test_batch_limit():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cfg = make_cfg(tmp)
        cfg["scoring"]["max_items_per_batch"] = 2
        conn = core.connect_db(cfg)
        for i in range(5):
            add_item(conn, f"reddit:{i}", title=f"Distinct factual post number {i} about filings")
        fake_api()
        assert scorer.run(conn, cfg)[0] == 2
        assert len(scorer.pending(conn, cfg)) == 2  # остальные — в следующие батчи (по 2 за раз)
        conn.close()


# ---------------- 3.1 таймаут цикла + 3.2 трейс ----------------

def test_cycle_timeout_and_trace():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cfg = make_cfg(tmp)
        cfg["budget"]["cycle_timeout_sec"] = 500
        clock = [0.0]
        done: list[str] = []

        def task(name: str, seconds: float):
            def fn():
                clock[0] += seconds
                done.append(name)
                return {"summary": "ok", "fetched": 10, "new": 3}
            return name, fn

        run._source_tasks = lambda cfg, conn, http, ctx: [task("fast", 5), task("slow", 600), task("late", 5)]
        run._filter_and_alert = lambda conn, cfg, ctx: {"news": {"passed": 2, "checked": 10, "duplicates": 1}}
        run.alerts.check_source_health = lambda conn, cfg: None
        notices: list[str] = []
        run.alerts.notify_once = lambda conn, cfg, key, text: notices.append(key)
        real_monotonic, run.time.monotonic = run.time.monotonic, lambda: clock[0]
        try:
            run.collect(cfg)
        finally:
            run.time.monotonic = real_monotonic

        assert done == ["fast", "slow"]  # «late» не запускался: время цикла вышло
        assert len(notices) == 1 and notices[0].startswith("timeout:")
        conn = core.connect_db(cfg)
        health = {r["source"]: r["fail_since"] for r in conn.execute("SELECT * FROM source_health")}
        assert health["fast"] is None and health["slow"] is None and health["late"] is not None
        assert Path(cfg["paths"]["beacon"]).exists()
        trace = json.loads(conn.execute("SELECT data FROM cycles WHERE kind='collect'").fetchone()["data"])
        assert trace["timed_out"] is True and trace["unavailable"] == 1
        assert trace["sources"]["fast"] == {"ok": True, "attempt": 1, "fetched": 10, "new": 3}
        assert trace["sources"]["late"]["ok"] is False and trace["filter"]["news"]["passed"] == 2
        conn.close()


def test_cost_estimate():
    cfg = core.load_config()
    assert abs(core.cost_usd(cfg, "claude-sonnet-5-5", 1_000_000, 100_000) - 3.0) < 1e-9
    assert core.cost_usd(cfg, "unknown-model", 1000, 1000) == 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if stream:
            stream.reconfigure(encoding="utf-8", errors="replace")
    os.environ["GME_TG_BOT_TOKEN"] = ""  # тесты ничего не шлют в Telegram
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"OK  {t.__name__}")
    print(f"\n{len(tests)} тестов пройдено")
