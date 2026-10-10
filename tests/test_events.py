"""Офлайн-тесты: типы событий и исторические аналоги (5.3), воскресная сводка (5.5), разбиение сообщений.

  python tests/test_events.py
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import types
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import anthropic  # noqa: E402

import core  # noqa: E402
import events  # noqa: E402
import weekly  # noqa: E402

CFG = core.load_config()
ACFG = CFG["analogs"]

FORM4_BUY = """<?xml version="1.0"?><ownershipDocument>
<reportingOwner><reportingOwnerId><rptOwnerName>Cohen Ryan</rptOwnerName></reportingOwnerId></reportingOwner>
<nonDerivativeTable>
 <nonDerivativeTransaction><transactionCoding><transactionCode>P</transactionCode></transactionCoding>
  <transactionAmounts><transactionShares><value>250000</value></transactionShares>
  <transactionPricePerShare><value>23.4753</value></transactionPricePerShare></transactionAmounts></nonDerivativeTransaction>
 <nonDerivativeTransaction><transactionCoding><transactionCode>P</transactionCode></transactionCoding>
  <transactionAmounts><transactionShares><value>200000</value></transactionShares>
  <transactionPricePerShare><value>23.4499</value></transactionPricePerShare></transactionAmounts></nonDerivativeTransaction>
</nonDerivativeTable></ownershipDocument>"""

FORM4_TAX = """<?xml version="1.0"?><ownershipDocument>
<reportingOwner><reportingOwnerId><rptOwnerName>Moore Daniel</rptOwnerName></reportingOwnerId></reportingOwner>
<nonDerivativeTable><nonDerivativeTransaction><transactionCoding><transactionCode>F</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>7297</value></transactionShares>
<transactionPricePerShare><value>24.26</value></transactionPricePerShare></transactionAmounts></nonDerivativeTransaction>
</nonDerivativeTable></ownershipDocument>"""


def make_cfg(tmp: str) -> dict:
    cfg = copy.deepcopy(CFG)
    cfg["paths"] = {"db": f"{tmp}/t.db", "logs": f"{tmp}/logs", "beacon": f"{tmp}/gme.ts", "lock": f"{tmp}/run.lock"}
    cfg["analogs"].update(since="2024-01-01", min_n=5, digest_horizon=5, horizons=[1, 5, 20])
    return cfg


def add_filing(conn, acc: str, form: str, day: str, items: str = "") -> None:
    core.upsert_item(conn, {"id": f"sec:{acc}", "kind": "sec", "source": "SEC EDGAR", "bias": "официальный документ",
                            "title": form, "url": f"https://www.sec.gov/Archives/edgar/data/1/{acc}/xslF345X06/f.xml",
                            "text": "", "created_utc": day + "T20:00:00+00:00",
                            "meta": {"form": form, "items": items, "filing_date": day, "meaning": form}})
    conn.commit()


def fill_prices(conn, start: date, n: int, step: float = 1.0) -> list[str]:
    """n торговых дней подряд (без выходных), цена растёт на step в день."""
    days, d, price = [], start, 100.0
    while len(days) < n:
        if d.weekday() < 5:
            conn.execute("INSERT OR REPLACE INTO prices(date,open,high,low,close,volume) VALUES(?,?,?,?,?,?)",
                         (d.isoformat(), price, price + 1, price - 1, price, 1000))
            days.append(d.isoformat())
            price += step
        d += timedelta(days=1)
    conn.commit()
    return days


# ---------------- типы событий ----------------

def test_classify_filings():
    assert events.classify_filing("8-K", "2.02,9.01", ACFG) == "earnings"
    assert events.classify_filing("8-K", "5.02,2.02", ACFG) == "earnings"       # приоритет по порядку в конфиге
    assert events.classify_filing("8-K", "5.02", ACFG) == "management_change"
    assert events.classify_filing("8-K", "7.01,9.01", ACFG) == events.NONE     # не отслеживается
    assert events.classify_filing("SCHEDULE 13D/A", "", ACFG) == "major_holder"
    assert events.classify_filing("SC 13G/A", "", ACFG) == events.NONE          # рутинные поправки 13G
    assert events.classify_filing("10-Q", "", ACFG) == "periodic_report"
    assert events.classify_filing("4", "", ACFG) is None                        # нужен разбор XML


def test_parse_form4():
    d = events.parse_form4(FORM4_BUY)
    assert d["event_type"] == "insider_buy" and d["owner"] == "Cohen Ryan" and d["shares"] == 450000
    assert abs(d["value_usd"] - (250000 * 23.4753 + 200000 * 23.4499)) < 0.01
    assert events.describe_form4(d) == "покупка на рынке: Cohen Ryan, 450 тыс. акций на $10,56 млн"
    t = events.parse_form4(FORM4_TAX)
    assert t["event_type"] == "insider_other" and "налог" in events.describe_form4(t)
    assert events.raw_form4_url("https://x/1/xslF345X03/edgar.xml") == "https://x/1/edgar.xml"


def test_classify_pending_fetches_form4():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cfg = make_cfg(tmp)
        conn = core.connect_db(cfg)
        add_filing(conn, "a1", "4", "2025-03-03")
        add_filing(conn, "a2", "4", "2020-03-03")      # старше analogs.since — XML не запрашиваем
        add_filing(conn, "a3", "8-K", "2025-03-04", "2.02,9.01")
        fetched: list[str] = []

        class Http:
            def get(self, url, headers=None, cache=True):
                fetched.append(url)
                return FORM4_BUY, True
        assert events.classify_pending(conn, Http(), cfg) == 3
        rows = {r["id"]: r for r in conn.execute("SELECT * FROM items")}
        assert fetched == ["https://www.sec.gov/Archives/edgar/data/1/a1/f.xml"]  # сырой XML, без xsl-папки
        assert rows["sec:a1"]["event_type"] == "insider_buy" and json.loads(rows["sec:a1"]["details"])["shares"] == 450000
        assert rows["sec:a2"]["event_type"] == events.NONE and rows["sec:a3"]["event_type"] == "earnings"
        assert events.classify_pending(conn, Http(), cfg) == 0 and len(fetched) == 1  # повторно не запрашивает
        conn.close()


# ---------------- аналоги ----------------

def test_analogs_stats_and_min_n():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cfg = make_cfg(tmp)
        conn = core.connect_db(cfg)
        days = fill_prices(conn, date(2025, 1, 6), 60)
        # 6 событий earnings (одно — в конце, цены через 5 дней ещё нет), 2 события mna
        for i, k in enumerate([0, 10, 20, 30, 40, 58]):
            add_filing(conn, f"e{i}", "8-K", days[k], "2.02")
        add_filing(conn, "dup", "8-K", days[10], "2.02")          # второй филинг того же типа в тот же день
        add_filing(conn, "m0", "8-K", days[5], "2.01")
        add_filing(conn, "m1", "8-K", days[15], "2.01")
        add_filing(conn, "wk", "8-K", "2025-01-11", "5.02")       # суббота → относится к понедельнику
        events.classify_pending(conn, None, cfg)
        events.rebuild(conn, cfg)

        ev = {(r["type"], r["trading_day"]): r for r in conn.execute("SELECT * FROM events")}
        assert len([k for k in ev if k[0] == "earnings"]) == 6    # дубль в один день не считается дважды
        assert ("management_change", "2025-01-13") in ev
        first = ev[("earnings", days[0])]
        assert first["close_0"] == 100.0 and json.loads(first["closes_after"]) == {"1": 101.0, "5": 105.0, "20": 120.0}
        assert json.loads(ev[("earnings", days[58])]["closes_after"]) == {"1": 159.0, "5": None, "20": None}

        s = events.stats(conn, cfg, "earnings")
        assert s["n"] == 5 and abs(s["median"] - (125 / 120 - 1) * 100) < 1e-9   # событие без цены +5 не в счёт
        assert abs(s["max"] - 5.0) < 1e-9 and abs(s["min"] - (145 / 140 - 1) * 100) < 1e-9
        line = events.analog_line(conn, cfg, "earnings")
        assert "с 2024 г. 5 случаев" in line and "медиана через 5 торг. дн. +4,2%" in line and "разброс [+4%; +5%]" in line
        # текущее событие не входит в собственную статистику → становится меньше min_n
        assert "недостаточно данных (N=4)" in events.analog_line(conn, cfg, "earnings", exclude_day=days[0])
        assert events.analog_line(conn, cfg, "mna") .endswith("недостаточно данных (N=2)")   # без процентов
        assert "%" not in events.analog_line(conn, cfg, "mna")
        assert events.analog_line(conn, cfg, "other") == "" and events.analog_line(conn, cfg, None) == ""
        conn.close()


def test_news_events_only_for_non_sec_types():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cfg = make_cfg(tmp)
        conn = core.connect_db(cfg)
        days = fill_prices(conn, date(2025, 1, 6), 30)
        for i, (etype, score) in enumerate([("press", 7), ("press", 3), ("insider_buy", 8)]):
            core.upsert_item(conn, {"id": f"news:{i}", "kind": "news", "source": "n", "title": "t", "url": "u",
                                    "text": "", "created_utc": days[i * 3] + "T10:00:00+00:00", "meta": {}})
            conn.execute("UPDATE items SET score=?, event_type=?, score_status='ok' WHERE id=?", (score, etype, f"news:{i}"))
        conn.commit()
        events.rebuild(conn, cfg)
        got = [(r["type"], r["source"]) for r in conn.execute("SELECT * FROM events")]
        # пресса с оценкой ниже порога не событие; покупка инсайдера считается только по Form 4
        assert got == [("press", "news")]
        conn.close()


# ---------------- воскресная сводка ----------------

def test_weekly_due_and_calendar():
    cfg = copy.deepcopy(CFG)
    cfg["weekly"].update(enabled=True, weekday=6, time="09:50")
    assert weekly.is_due(cfg, datetime(2026, 10, 11, 9, 50))        # воскресенье
    assert not weekly.is_due(cfg, datetime(2026, 10, 11, 9, 49))
    assert not weekly.is_due(cfg, datetime(2026, 10, 12, 12, 0))    # понедельник

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cfg = make_cfg(tmp)
        cfg["calendar"] = {"horizon_days": 60, "manual": [
            {"date": "2026-10-30", "title": "истечение варрантов", "source": "компания"},
            {"date": "2026-09-01", "title": "прошедшее", "source": "x"},
            {"date": "2027-06-01", "title": "слишком далеко", "source": "x"}],
            "estimates": [{"event_type": "earnings", "title": "квартальная отчётность"}]}
        conn = core.connect_db(cfg)
        for day in ("2025-12-09", "2025-09-09", "2026-09-09"):
            conn.execute("INSERT INTO events(type,trading_day,source,item_id,close_0,closes_after) VALUES(?,?,?,?,?,?)",
                         ("earnings", day, "sec", "x", 1.0, "{}"))
        confirmed, estimates = weekly.calendar(conn, cfg, date(2026, 10, 11))
        assert len(confirmed) == 1 and "30.10.2026 (через 19 дн.) — истечение варрантов" in confirmed[0]
        assert len(estimates) == 1 and "около 09.12" in estimates[0] and "дата не подтверждена" in estimates[0]
        conn.close()


def test_weekly_model_points_validated():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cfg = make_cfg(tmp)
        conn = core.connect_db(cfg)
        fill_prices(conn, date.today() - timedelta(days=20), 12)
        now, old = core.iso(), core.iso(core.now_utc() - timedelta(days=10))
        for item_id, created in (("news:new", now), ("news:old", old)):
            core.upsert_item(conn, {"id": item_id, "kind": "news", "source": "Wire", "bias": "нейтральный",
                                    "title": "Insider bought shares", "url": f"https://e.com/{item_id}", "text": "",
                                    "created_utc": created, "meta": {}})
            conn.execute("UPDATE items SET score=8, score_status='ok', summary='Факт.', passed_filter=1 WHERE id=?",
                         (item_id,))
        conn.commit()
        reply = {"changes": [
            {"text_ru": "Директор купил акции на открытом рынке, подан Form 4.", "refs": ["news:new"]},
            {"text_ru": "Аналитики повысили целевую цену акции до сорока долларов.", "refs": ["news:new"]},
            {"text_ru": "Пункт без источника из входа, такого быть не должно.", "refs": ["news:ghost"]}],
            "continuations": [
            {"text_ru": "Раньше сообщалось о покупке, на этой неделе подан новый Form 4.", "refs": ["news:old", "news:new"]},
            {"text_ru": "Связь только с прошлым материалом, без события этой недели.", "refs": ["news:old"]}]}
        calls = []

        class Client:
            def __init__(self):
                self.messages = self
                self.beta = types.SimpleNamespace(messages=self)

            def create(self, **kw):
                calls.append(kw)
                return types.SimpleNamespace(
                    stop_reason="end_turn", usage=types.SimpleNamespace(input_tokens=10, output_tokens=5),
                    content=[types.SimpleNamespace(type="text", text=json.dumps(reply, ensure_ascii=False))])
        anthropic.Anthropic = lambda **kw: Client()

        data = weekly.gather(conn, cfg)
        assert [r["id"] for r in data["week_items"]] == ["news:new"] and [r["id"] for r in data["earlier"]] == ["news:old"]
        sections, err = weekly.model_sections(conn, cfg, data)
        assert err is None and len(calls) == 2                       # невалидные пункты → один повтор
        assert len(sections["changes"]) == 1 and len(sections["continuations"]) == 1
        assert sections["continuations"][0]["links"][0]["url"] == "https://e.com/news:old"  # ссылки подставил код

        text = weekly.build(conn, cfg, data, sections, None, None)
        assert "целев" not in text and "ghost" not in text
        assert "📌 <b>Что изменилось за неделю</b>" in text and "🔁 <b>Продолжения прошлых событий</b>" in text
        assert "🆕 <b>Новое за сутки</b>" in text and "📅 <b>Ближайшие события</b>" in text
        # Claude недоступен — сводка всё равно собирается, с пометкой
        degraded = weekly.build(conn, cfg, data, None, "Claude API: timeout", None)
        assert "не построены" in degraded and "💵 <b>Цена</b>" in degraded
        conn.close()


# ---------------- разбиение длинных сообщений ----------------

def test_split_never_breaks_tags():
    link = '<a href="https://e.com/' + "u" * 3000 + '">источник</a>'
    block = "\n".join(f"• пункт {i} [{link}]" for i in range(6))
    parts = core._split("шапка\n\n" + block + "\n\n" + "я" * 9000)
    assert all(core._visible_len(p) <= core.TG_LIMIT for p in parts)
    assert all(p.count("<a ") == p.count("</a>") for p in parts)   # ни один тег не разорван
    assert "".join(parts).count("источник") == 6
    assert core._split("короткое") == ["короткое"]


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if stream:
            stream.reconfigure(encoding="utf-8", errors="replace")
    os.environ["GME_TG_BOT_TOKEN"] = ""
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"OK  {t.__name__}")
    print(f"\n{len(tests)} тестов пройдено")
