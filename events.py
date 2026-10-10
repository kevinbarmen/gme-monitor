"""Исторические аналоги: у каждого события есть тип из фиксированного списка (config.yaml →
analogs.event_types), для каждого события хранится цена закрытия в день события и через
1, 5, 20 торговых дней. В дайджесте по типу события показывается, как цена вела себя раньше.

Это описательная статистика по прошлому, а не прогноз: при малом числе случаев (N < min_n)
проценты не выводятся вовсе.

Откуда берётся тип:
  - SEC-филинги — детерминированно: форма, items у 8-K, код операции в XML у Form 4;
  - новости и посты — модель при скоринге выбирает тип из того же списка.
Для типов, у которых есть SEC-источник, статистика считается только по филингам (объективная дата),
для остальных (пресса, операционные новости, байбек) — по новостям с оценкой не ниже порога.
"""
from __future__ import annotations

import bisect
import json
import os
import re
import statistics
import xml.etree.ElementTree as ET
from datetime import datetime

from core import iso, log

NONE = "-"  # тип не определён / не отслеживается

# коды операций Form 4: P — покупка на рынке, S — продажа на рынке; остальное — награды, налоги и т.п.
FORM4_OTHER = {"A": "награда акциями", "F": "удержание акций в счёт налога", "M": "исполнение опциона/конвертация",
               "G": "дарение", "D": "возврат эмитенту", "C": "конвертация", "X": "исполнение дериватива"}


# ---------------- тип события для SEC-филингов ----------------

def classify_filing(form: str, items: str, acfg: dict) -> str | None:
    """Тип события по форме и items. None — нужен разбор XML (Form 4)."""
    form = form.replace("SCHEDULE ", "SC ")
    if form == "4":
        return None
    if form.startswith("8-K"):
        codes = {c.strip() for c in (items or "").split(",")}
        for item, event_type in acfg["sec_8k_items"].items():  # порядок в конфиге = приоритет
            if item in codes:
                return event_type
        return NONE
    if form in acfg["major_holder_forms"]:
        return "major_holder"
    if form.startswith(("10-Q", "10-K")):
        return "periodic_report"
    return NONE


def parse_form4(xml_text: str) -> dict:
    """Суть Form 4 из XML: кто, покупка/продажа/прочее, сколько акций и на какую сумму."""
    root = ET.fromstring(xml_text)
    owner = (root.findtext(".//reportingOwner/reportingOwnerId/rptOwnerName") or "").strip()
    totals: dict[str, dict] = {}
    for tx in root.findall(".//nonDerivativeTable/nonDerivativeTransaction"):
        code = (tx.findtext("transactionCoding/transactionCode") or "").strip()
        shares = float(tx.findtext("transactionAmounts/transactionShares/value") or 0)
        price = float(tx.findtext("transactionAmounts/transactionPricePerShare/value") or 0)
        t = totals.setdefault(code, {"shares": 0.0, "value_usd": 0.0})
        t["shares"] += shares
        t["value_usd"] += shares * price
    for code, event_type in (("P", "insider_buy"), ("S", "insider_sell")):
        if code in totals:
            return {"owner": owner, "event_type": event_type, "code": code, **totals[code]}
    codes = sorted(totals) or sorted({(e.text or "").strip() for e in root.iter("transactionCode")})
    return {"owner": owner, "event_type": "insider_other", "code": ",".join(codes), "shares": 0.0, "value_usd": 0.0}


def describe_form4(d: dict) -> str:
    """Одна строка «что это значит» для дайджеста — только факты из филинга."""
    from fundamentals import fmt_number
    who = d.get("owner") or "инсайдер"
    if d["event_type"] in ("insider_buy", "insider_sell"):
        verb = "покупка на рынке" if d["event_type"] == "insider_buy" else "продажа на рынке"
        return f"{verb}: {who}, {fmt_number(d['shares'], 'shares')} акций на {fmt_number(d['value_usd'], 'USD')}"
    what = ", ".join(FORM4_OTHER.get(c, f"код {c}") for c in d["code"].split(",") if c) or "операция без сделки на рынке"
    return f"операция инсайдера без сделки на рынке ({what}): {who}"


def raw_form4_url(url: str) -> str:
    return re.sub(r"/xslF345X\d+/", "/", url)


def classify_pending(conn, http, cfg: dict, limit: int | None = None) -> int:
    """Проставляет event_type SEC-филингам, у которых его ещё нет. Form 4 — через XML
    (только начиная с analogs.since; более старые в статистике не участвуют)."""
    acfg = cfg["analogs"]
    limit = limit or acfg["form4_fetch_per_cycle"]
    rows = conn.execute("SELECT id, url, meta FROM items WHERE kind='sec' AND event_type IS NULL "
                        "ORDER BY created_utc DESC").fetchall()
    done = fetched = 0
    for r in rows:
        m = json.loads(r["meta"])
        event_type = classify_filing(m["form"], m.get("items", ""), acfg)
        details = None
        if event_type is None:  # Form 4
            if m["filing_date"] < acfg["since"]:
                event_type = NONE
            elif fetched >= limit:
                continue  # остальные — в следующих циклах
            else:
                fetched += 1
                try:
                    body, _ = http.get(raw_form4_url(r["url"]), headers={"User-Agent": os.getenv("SEC_USER_AGENT", "")})
                    details = parse_form4(body)
                    event_type = details["event_type"]
                except Exception as e:  # noqa: BLE001 — один нечитаемый филинг не должен ломать сбор
                    log.warning("events: Form 4 %s не разобран: %s", r["id"], e)
                    event_type = "insider_other"
                    details = {"owner": "", "event_type": event_type, "code": "", "shares": 0.0, "value_usd": 0.0,
                               "unparsed": True}
        conn.execute("UPDATE items SET event_type=?, details=? WHERE id=?",
                     (event_type, json.dumps(details, ensure_ascii=False) if details else None, r["id"]))
        done += 1
    conn.commit()
    return done


# ---------------- таблица событий с ценами ----------------

def rebuild(conn, cfg: dict) -> int:
    """Пересобирает таблицу events: одно событие на (тип, торговый день) с ценами закрытия
    в день события и через 1/5/20 торговых дней (пусто, пока день не наступил)."""
    acfg = cfg["analogs"]
    prices = conn.execute("SELECT date, close FROM prices ORDER BY date").fetchall()
    dates, closes = [p["date"] for p in prices], [p["close"] for p in prices]
    types = acfg["event_types"]
    sec_types = set(acfg["sec_8k_items"].values()) | {"major_holder", "periodic_report", "insider_buy",
                                                      "insider_sell", "insider_other"}

    found: dict[tuple[str, str], tuple[str, str]] = {}  # (type, trading_day) -> (source, item_id)

    def add(event_type: str, day: str, source: str, item_id: str) -> None:
        i = bisect.bisect_left(dates, day[:10])
        if event_type in types and i < len(dates):
            found.setdefault((event_type, dates[i]), (source, item_id))

    for r in conn.execute("SELECT id, meta, event_type FROM items WHERE kind='sec' AND event_type IS NOT NULL"):
        day = json.loads(r["meta"])["filing_date"]
        if day >= acfg["since"]:
            add(r["event_type"], day, "sec", r["id"])
    for r in conn.execute(
            "SELECT id, created_utc, event_type FROM items WHERE kind IN ('reddit','news') AND event_type IS NOT NULL "
            "AND score>=? AND COALESCE(injection,0)=0 ORDER BY created_utc", (cfg["scoring"]["min_score"],)):
        if r["event_type"] not in sec_types and r["created_utc"][:10] >= acfg["since"]:
            add(r["event_type"], r["created_utc"], "news", r["id"])

    conn.execute("DELETE FROM events")
    for (event_type, day), (source, item_id) in found.items():
        i = dates.index(day)
        after = {h: (closes[i + h] if i + h < len(closes) else None) for h in acfg["horizons"]}
        conn.execute(
            "INSERT INTO events(type,trading_day,source,item_id,close_0,closes_after,updated_at) VALUES(?,?,?,?,?,?,?)",
            (event_type, day, source, item_id, closes[i], json.dumps(after), iso()))
    conn.commit()
    return len(found)


def stats(conn, cfg: dict, event_type: str, exclude_day: str | None = None) -> dict:
    """{'n', 'median', 'min', 'max'} движения цены через analogs.digest_horizon торговых дней
    после закрытия дня события. Текущее событие (exclude_day) в свою же статистику не входит."""
    h = str(cfg["analogs"]["digest_horizon"])
    moves = []
    for r in conn.execute("SELECT trading_day, close_0, closes_after FROM events WHERE type=?", (event_type,)):
        later = json.loads(r["closes_after"]).get(h)
        if later is not None and r["trading_day"] != exclude_day:
            moves.append((later / r["close_0"] - 1) * 100)
    if not moves:
        return {"n": 0}
    return {"n": len(moves), "median": statistics.median(moves), "min": min(moves), "max": max(moves)}


def analog_line(conn, cfg: dict, event_type: str | None, exclude_day: str | None = None) -> str:
    """Строка для дайджеста ('' — если тип не отслеживается)."""
    acfg = cfg["analogs"]
    if not acfg.get("enabled") or not event_type or event_type not in acfg["event_types"] \
            or event_type in acfg.get("no_stats_types", []):
        return ""
    label = acfg["event_types"][event_type]
    s = stats(conn, cfg, event_type, exclude_day)
    if s["n"] < acfg["min_n"]:
        return f"📊 Аналоги — {label}: недостаточно данных (N={s['n']})"
    year = acfg["since"][:4]
    pct = lambda v, d=1: f"{v:+.{d}f}".replace(".", ",") + "%"  # noqa: E731
    return (f"📊 Аналоги — {label}: с {year} г. {s['n']} случаев, медиана через {acfg['digest_horizon']} торг. дн. "
            f"{pct(s['median'])}, разброс [{pct(s['min'], 0)}; {pct(s['max'], 0)}]")


def trading_day_of(conn, day: str) -> str | None:
    row = conn.execute("SELECT MIN(date) d FROM prices WHERE date>=?", (day[:10],)).fetchone()
    return row["d"]


def report(conn, cfg: dict) -> str:
    """Таблица для `run.py analogs`."""
    acfg = cfg["analogs"]
    lines = [f"События с {acfg['since']}; движение от закрытия дня события через {acfg['digest_horizon']} торг. дн.", "",
             f"{'тип события':<52}{'N':>5}{'медиана':>10}{'мин':>9}{'макс':>9}"]
    for key, label in acfg["event_types"].items():
        s = stats(conn, cfg, key)
        total = conn.execute("SELECT COUNT(*) FROM events WHERE type=?", (key,)).fetchone()[0]
        if s["n"] >= acfg["min_n"]:
            lines.append(f"{label:<52}{s['n']:>5}{s['median']:>+9.1f}%{s['min']:>+8.0f}%{s['max']:>+8.0f}%")
        else:
            lines.append(f"{label:<52}{s['n']:>5}   недостаточно данных (всего событий {total})")
    left = conn.execute("SELECT COUNT(*) FROM items WHERE kind='sec' AND event_type IS NULL").fetchone()[0]
    if left:
        lines.append(f"\nещё не классифицировано филингов: {left} (Form 4 разбираются по "
                     f"{acfg['form4_fetch_per_cycle']} за цикл сбора)")
    lines.append("\nОписательная статистика по прошлому, не прогноз.")
    return "\n".join(lines)


def last_dates(conn, event_type: str, n: int = 4) -> list[str]:
    """Даты последних событий типа (для календаря ориентиров)."""
    return [r["trading_day"] for r in conn.execute(
        "SELECT trading_day FROM events WHERE type=? ORDER BY trading_day DESC LIMIT ?", (event_type, n))]


def fmt_day(d: str) -> str:
    return datetime.fromisoformat(d).strftime("%d.%m.%Y")
