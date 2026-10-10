"""Офлайн-тесты блока контекста (без сети и API): выбор показателей из XBRL, подтверждение чисел
цитатой, расчёт масштаба, запрещённые формулировки, повтор при невалидном ответе, рендер.

  python tests/test_context.py
"""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import anthropic  # noqa: E402

import context  # noqa: E402
import core  # noqa: E402
import fundamentals  # noqa: E402

CFG = core.load_config()
CCFG = CFG["context"]
DATA = json.loads((ROOT / "evals" / "context_cases.json").read_text(encoding="utf-8"))
FUNDS = DATA["fundamentals"]
ITEM = DATA["cases"][0]  # покупка 450 000 акций на $10.56 млн


def entry(**over) -> dict:
    base = {"id": ITEM["id"], "amount_usd": 10560000, "amount_shares": 450000,
            "amount_label_ru": "покупка акций", "amount_quote": "purchased 450,000 shares in two open-market "
            "transactions at $23.4753 and $23.4499, about $10.56 million total",
            "bull": {"has_argument": True, "text_ru": "Руководитель купил акции на открытом рынке за свои средства.",
                     "refs": ["news"]},
            "bear": {"has_argument": False, "text_ru": "", "refs": []}}
    base.update(over)
    return base


def fake_api(replies: list) -> list:
    calls = []

    class Client:
        def __init__(self):
            self.messages = self
            self.beta = types.SimpleNamespace(messages=self)

        def create(self, **kw):
            calls.append(kw)
            return types.SimpleNamespace(
                stop_reason="end_turn", usage=types.SimpleNamespace(input_tokens=100, output_tokens=50),
                content=[types.SimpleNamespace(type="text", text=json.dumps({"items": replies.pop(0)},
                                                                            ensure_ascii=False))])
    client = Client()
    anthropic.Anthropic = lambda **kw: client
    return calls


# ---------------- 5.1 отчётность ----------------

def test_pick_latest_fundamentals():
    facts = {"us-gaap": {
        "Revenues": {"units": {"USD": [
            {"start": "2026-02-01", "end": "2026-05-02", "val": 835, "form": "10-Q", "filed": "2026-06-11", "accn": "a1"},
            {"start": "2026-02-01", "end": "2026-08-01", "val": 1625, "form": "10-Q", "filed": "2026-09-09", "accn": "a2"},
            {"start": "2026-05-03", "end": "2026-08-01", "val": 790, "form": "10-Q", "filed": "2026-09-09", "accn": "a2"},
        ]}},
        "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [
            {"end": "2026-05-02", "val": 7397, "form": "10-Q", "filed": "2026-06-11", "accn": "a1"},
            {"end": "2026-08-01", "val": 4854, "form": "10-Q", "filed": "2026-09-09", "accn": "a2"},
        ]}}}}
    q = fundamentals.pick_latest(facts, {"kind": "quarter", "concepts": ["us-gaap:Revenues"]})
    assert q["val"] == 790  # квартал, а не полугодие с той же датой конца
    cash = fundamentals.pick_latest(facts, {"kind": "instant", "concepts": ["us-gaap:CashAndCashEquivalentsAtCarryingValue"]})
    assert cash["val"] == 4854 and cash["accn"] == "a2"
    assert fundamentals.pick_latest(facts, {"kind": "instant", "concepts": ["us-gaap:Nope"]}) is None


def test_store_marks_stale_and_keeps_source():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cfg = copy.deepcopy(CFG)
        cfg["paths"]["db"] = f"{tmp}/t.db"
        cfg["fundamentals"]["metrics"] = [
            {"key": "cash", "label": "кэш", "kind": "instant", "concepts": ["us-gaap:Cash"]},
            {"key": "buybacks", "label": "байбек", "kind": "period", "concepts": ["us-gaap:Buy"]}]
        conn = core.connect_db(cfg)
        from datetime import date, timedelta
        fresh = (date.today() - timedelta(days=60)).isoformat()
        facts = {"us-gaap": {
            "Cash": {"units": {"USD": [{"end": fresh, "val": 5e9, "form": "10-Q", "filed": fresh, "accn": "0001-26-000055"}]}},
            "Buy": {"units": {"USD": [{"start": "2021-01-31", "end": "2022-01-29", "val": 0, "form": "10-K",
                                       "filed": "2022-03-17", "accn": "0001-22-000021"}]}}}}
        assert fundamentals.store(conn, cfg, facts) == 2
        loaded = fundamentals.load(conn)
        assert set(loaded) == {"cash"}  # устаревший байбек 2022 года в контекст не идёт
        assert loaded["cash"]["url"].endswith("/000126000055/") and loaded["cash"]["form"] == "10-Q"
        conn.close()


def test_number_formatting():
    assert fundamentals.fmt_number(4854300000, "USD") == "$4,85 млрд"
    assert fundamentals.fmt_number(790200000, "USD") == "$790,2 млн"
    assert fundamentals.fmt_number(504500990, "shares") == "504,5 млн"
    assert fundamentals.fmt_number(450000, "shares") == "450 тыс."


# ---------------- масштаб: числа только из новости ----------------

def test_scale_computed_by_code():
    sc = context.scale(entry(), ITEM, FUNDS, CCFG)
    assert sc["available"] and len(sc["parts"]) == 2
    usd = sc["parts"][0]
    assert usd["amount"] == "$10,56 млн"
    pcts = {c["metric"]: round(c["pct"], 2) for c in usd["comparisons"]}
    assert pcts == {"выручка за квартал": 1.34, "денежные средства и эквиваленты": 0.22}
    assert all(c["url"].startswith("https://www.sec.gov/") and c["source"] == "10-Q от 09.09.2026"
               for c in usd["comparisons"])
    assert sc["parts"][1]["amount"] == "450 тыс. акций"


def test_ungrounded_numbers_rejected():
    # цитаты нет в тексте новости
    assert not context.scale(entry(amount_quote="bought $10.56 million of stock yesterday"), ITEM, FUNDS, CCFG)["available"]
    # цитата настоящая, но число не из неё (подставная сумма из инъекции)
    sc = context.scale(entry(amount_usd=5000000000, amount_shares=None), ITEM, FUNDS, CCFG)
    assert not sc["available"]
    # чисел нет вовсе
    assert not context.scale(entry(amount_usd=None, amount_shares=None, amount_quote=""), ITEM, FUNDS, CCFG)["available"]


def test_validate_rejects_bad_answers():
    known, refs = {ITEM["id"]}, {"fund:cash"}
    ok = lambda e: context.validate(e, known, refs, CCFG)  # noqa: E731
    assert ok(entry()) is None
    bull = lambda **kw: entry(bull={"has_argument": True, "text_ru": "Достаточно длинный аргумент по делу.",  # noqa: E731
                                    "refs": ["news"], **kw})
    assert "без источника" in ok(bull(refs=[]))
    assert "неизвестные источники" in ok(bull(refs=["fund:made_up"]))
    assert "запрещённая" in ok(bull(text_ru="Сделка крупная, поэтому акции стоит покупать уже сейчас."))
    assert "запрещённая" in ok(bull(text_ru="Аналитики называют целевую цену в сто долларов за акцию."))
    assert "запрещённая" in ok(bull(text_ru="После этой сделки акция вырастет до $40 в ближайший месяц."))
    assert "длиннее" in ok(bull(text_ru="я" * (CCFG["argument_max_chars"] + 1)))
    assert "диапазона" in ok(entry(amount_usd=-5))
    # описание факта покупки — не рекомендация
    assert ok(bull(text_ru="Директор продолжает покупать акции на открытом рынке третью неделю подряд.")) is None


def test_retry_then_no_context():
    bad = entry(bull={"has_argument": True, "text_ru": "Акции следует покупать, пока не поздно, это очевидно.",
                      "refs": ["news"]})
    calls = fake_api([[bad], [bad]])
    valid, stats, err = context.build_for(CFG, [ITEM], FUNDS, [], [])
    assert err is None and len(calls) == 2  # один повтор, не больше
    assert valid == {} and stats["n_invalid"] == 1 and stats["n_failed"] == 1  # контекст не выдуман

    calls = fake_api([[bad], [entry()]])
    valid, stats, _ = context.build_for(CFG, [ITEM], FUNDS, [], [])
    assert len(calls) == 2 and ITEM["id"] in valid and stats["n_failed"] == 0


def test_untrusted_wrapping_and_ref_enum():
    content, refs = context.build_message([ITEM], FUNDS, [], [], CFG)
    assert content.count("<untrusted_content_") == 2  # шапка + один блок материала
    assert "fund:cash" in refs and refs["fund:cash"]["url"].startswith("https://www.sec.gov/")
    enum = context.schema(list(refs))["properties"]["items"]["items"]["properties"]["bull"]["properties"]["refs"]
    assert enum["items"]["enum"][0] == "news" and "fund:revenue_q" in enum["items"]["enum"]


# ---------------- рендер ----------------

def test_render():
    refs = {"fund:cash": {"label": "денежные средства: $4,85 млрд на 01.08.2026, 10-Q от 09.09.2026",
                          "url": "https://www.sec.gov/x/"}}
    e = entry(bear={"has_argument": True, "text_ru": "Сумма мала на фоне денежных средств компании & <прочего>.",
                    "refs": ["fund:cash", "news", "fund:cash"]})
    html = context.render(json.dumps(context.assemble(e, ITEM, FUNDS, refs, CCFG), ensure_ascii=False))
    assert "📐 <b>Масштаб</b> (покупка акций): $10,56 млн ≈ 1,3% от показателя «выручка за квартал»" in html
    assert '<a href="https://www.sec.gov/Archives/edgar/data/1326380/000132638026000055/">10-Q от 09.09.2026</a>' in html
    assert "&amp; &lt;прочего&gt;" in html  # текст модели экранирован
    assert html.count("https://www.sec.gov/x/") == 1  # повтор источника схлопнут
    assert "🐂 <b>За</b>" in html and "🐻 <b>Против</b>" in html

    nothing = entry(amount_usd=None, amount_shares=None, amount_quote="",
                    bull={"has_argument": False, "text_ru": "", "refs": []})
    html = context.render(json.dumps(context.assemble(nothing, ITEM, FUNDS, {}, CCFG), ensure_ascii=False))
    assert "масштаб оценить нельзя" in html
    assert "существенных аргументов «за» не найдено" in html and "существенных аргументов «против» не найдено" in html
    assert context.render(None) == "" and context.render('{"failed": true}') == ""
    assert context._pct(0.04) == "менее 0,1%" and "<" not in context._pct(0.04)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if stream:
            stream.reconfigure(encoding="utf-8", errors="replace")
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"OK  {t.__name__}")
    print(f"\n{len(tests)} тестов пройдено")
