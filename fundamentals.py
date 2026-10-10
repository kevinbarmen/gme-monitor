"""Фактический контекст из отчётности: последние значения ключевых показателей GameStop
из SEC EDGAR (XBRL company facts API, бесплатно). Кэш в SQLite, обновление раз в день
и сразу при новом 10-Q/10-K. Каждая цифра хранится со ссылкой на филинг-источник и датой.

Какие показатели и из каких XBRL-концептов брать — в config.yaml → fundamentals.metrics.
"""
from __future__ import annotations

import os
from datetime import date, datetime

from core import PermanentError, iso, kv_get, kv_set, log

API = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"


def _duration_days(fact: dict) -> int | None:
    if not fact.get("start"):
        return None
    return (date.fromisoformat(fact["end"]) - date.fromisoformat(fact["start"])).days


def _matches(fact: dict, kind: str) -> bool:
    days = _duration_days(fact)
    if kind == "instant":      # значение на дату (баланс)
        return days is None
    if kind == "quarter":      # значение за квартал (отчёт о прибылях)
        return days is not None and 80 <= days <= 100
    return days is not None    # period: любой период (например, с начала года)


def pick_latest(facts: dict, metric: dict) -> dict | None:
    """Самое свежее значение показателя среди перечисленных концептов.
    Свежесть — по дате конца периода, затем по дате подачи филинга."""
    best = None
    for concept in metric["concepts"]:
        ns, name = concept.split(":")
        entry = facts.get(ns, {}).get(name)
        if not entry:
            continue
        for unit, values in entry["units"].items():
            if unit != metric.get("unit", "USD"):
                continue
            for f in values:
                if not _matches(f, metric["kind"]):
                    continue
                key = (f["end"], f["filed"])
                if best is None or key > (best["end"], best["filed"]):
                    best = {**f, "unit": unit, "concept": concept}
    return best


def filing_url(cik: str, accession: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/"


def store(conn, cfg: dict, facts: dict) -> int:
    fcfg = cfg["fundamentals"]
    cik = cfg["sec"]["cik"]
    today = date.today()
    n = 0
    for metric in fcfg["metrics"]:
        f = pick_latest(facts, metric)
        if f is None:
            log.warning("fundamentals: по показателю %s нет данных в XBRL", metric["key"])
            conn.execute("DELETE FROM fundamentals WHERE key=?", (metric["key"],))
            continue
        stale = (today - date.fromisoformat(f["end"])).days > fcfg["max_age_days"]
        conn.execute(
            "INSERT INTO fundamentals(key,label,value,unit,period_start,period_end,form,filed,accession,url,"
            "concept,stale,fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
            "label=excluded.label, value=excluded.value, unit=excluded.unit, period_start=excluded.period_start, "
            "period_end=excluded.period_end, form=excluded.form, filed=excluded.filed, accession=excluded.accession, "
            "url=excluded.url, concept=excluded.concept, stale=excluded.stale, fetched_at=excluded.fetched_at",
            (metric["key"], metric["label"], f["val"], f["unit"], f.get("start"), f["end"], f["form"], f["filed"],
             f["accn"], filing_url(cik, f["accn"]), f["concept"], int(stale), iso()))
        n += 1
    conn.commit()
    return n


def refresh(conn, http, cfg: dict, force: bool = False) -> str:
    """Обновляет кэш, если сегодня ещё не обновляли (или force). Возвращает строку для лога."""
    today = date.today().isoformat()
    if not force and kv_get(conn, "fundamentals_day") == today:
        return "кэш свежий"
    ua = os.getenv("SEC_USER_AGENT")
    if not ua:
        raise PermanentError("SEC_USER_AGENT не задан в .env")
    data = http.get_json(API.format(cik=cfg["sec"]["cik"].zfill(10)), headers={"User-Agent": ua}, cache=False)
    n = store(conn, cfg, data["facts"])
    kv_set(conn, "fundamentals_day", today)
    return f"обновлено показателей: {n}"


def load(conn) -> dict[str, dict]:
    """Актуальные (не устаревшие) показатели по ключу."""
    return {r["key"]: dict(r) for r in conn.execute("SELECT * FROM fundamentals WHERE stale=0 ORDER BY key")}


# ---------------- форматирование ----------------

def fmt_number(value: float, unit: str) -> str:
    sign = "-" if value < 0 else ""
    v = abs(value)
    for limit, suffix in ((1e9, " млрд"), (1e6, " млн"), (1e3, " тыс.")):
        if v >= limit:
            body = f"{v / limit:.2f}".rstrip("0").rstrip(".").replace(".", ",") + suffix
            break
    else:
        body = f"{v:.0f}"
    return f"{sign}${body}" if unit == "USD" else f"{sign}{body}"


def fmt_date(d: str) -> str:
    return datetime.fromisoformat(d).strftime("%d.%m.%Y")


def describe_period(f: dict) -> str:
    if f["period_start"]:
        return f"за {fmt_date(f['period_start'])}–{fmt_date(f['period_end'])}"
    return f"на {fmt_date(f['period_end'])}"


def source_label(f: dict) -> str:
    return f"{f['form']} от {fmt_date(f['filed'])}"


def report(conn) -> str:
    """Текстовая таблица для `run.py fundamentals`."""
    rows = conn.execute("SELECT * FROM fundamentals ORDER BY key").fetchall()
    if not rows:
        return "Показателей нет — запустите сбор (run.py collect) или run.py fundamentals --force."
    lines = []
    for r in rows:
        r = dict(r)
        mark = "  (устарело — в контекст не идёт)" if r["stale"] else ""
        lines.append(f"{r['label']:<42} {fmt_number(r['value'], r['unit']):>14}  {describe_period(r):<26} "
                     f"{source_label(r)}{mark}\n{'':<42} {r['url']}")
    return "\n".join(lines)
