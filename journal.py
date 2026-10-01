"""Дневник: что важного произошло за день + цена. И простой поиск паттернов:
как вела себя цена после событий разного типа по сравнению с обычным днём.

Это описательная статистика по прошлому, а не прогноз и не рекомендация."""
from __future__ import annotations

import csv
import json
import statistics
from datetime import date, datetime, timedelta

from core import iso, log, now_utc, resolve_path


# ---------------- запись ----------------

def store_bars(conn, bars: list[dict]) -> None:
    conn.executemany(
        "INSERT INTO prices(date,open,high,low,close,volume) VALUES(:date,:open,:high,:low,:close,:volume) "
        "ON CONFLICT(date) DO UPDATE SET open=excluded.open, high=excluded.high, low=excluded.low, "
        "close=excluded.close, volume=excluded.volume",
        bars,
    )
    conn.commit()


def store_rall(conn, hits: list[dict], tracked: set[str]) -> list[dict]:
    """Сохраняет попадания отслеживаемых сабов в топ r/all. Возвращает их."""
    ts = iso()
    mine = [h for h in hits if h["sub"].lower() in tracked]
    conn.executemany(
        "INSERT OR IGNORE INTO rall_hits(post_id,ts,rank,sub,title,url) VALUES(?,?,?,?,?,?)",
        [(h["id"], ts, h["rank"], h["sub"], h["title"], h["url"]) for h in mine],
    )
    conn.commit()
    return mine


def price_summary(conn) -> dict | None:
    """Последний торговый день: закрытие, изменение, объём к среднему за 20 дней."""
    rows = conn.execute("SELECT * FROM prices ORDER BY date DESC LIMIT 21").fetchall()
    if len(rows) < 2:
        return None
    last, prev = rows[0], rows[1]
    vols = [r["volume"] for r in rows[1:] if r["volume"]]
    avg = sum(vols) / len(vols) if vols else None
    return {
        "market_day": last["date"],
        "close": last["close"],
        "change_pct": (last["close"] / prev["close"] - 1) * 100,
        "volume": last["volume"],
        "vol_ratio": (last["volume"] / avg) if avg and last["volume"] else None,
    }


def write_entry(conn, cfg: dict, digest_stamp: str) -> None:
    """Запись дня — вызывается после отправки дайджеста."""
    sent = conn.execute("SELECT * FROM items WHERE digest_sent_at=?", (digest_stamp,)).fetchall()
    sec = [{"form": "Form 4" if json.loads(r["meta"])["form"] == "4" else json.loads(r["meta"])["form"], "meaning": json.loads(r["meta"])["meaning"],
            "filing_date": json.loads(r["meta"])["filing_date"], "url": r["url"]}
           for r in sent if r["kind"] == "sec"]
    top = [{"score": r["score"], "category": r["category"], "source": r["source"], "title": r["title"],
            "summary": r["summary"], "url": r["url"]}
           for r in sent if r["kind"] != "sec" and r["score"] is not None]

    since = iso(now_utc() - timedelta(hours=24))
    rall = [dict(r) for r in conn.execute(
        "SELECT post_id, MIN(rank) best_rank, sub, title, url FROM rall_hits WHERE ts>=? "
        "GROUP BY post_id ORDER BY best_rank", (since,))]
    counts = {r["kind"]: r["n"] for r in conn.execute(
        "SELECT kind, COUNT(*) n FROM items WHERE passed_filter=1 AND first_seen>=? GROUP BY kind", (since,))}
    p = price_summary(conn) or {}

    conn.execute(
        "INSERT INTO journal(date,market_day,close,change_pct,volume,vol_ratio,sec,top,rall,reddit_n,news_n) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(date) DO UPDATE SET market_day=excluded.market_day, "
        "close=excluded.close, change_pct=excluded.change_pct, volume=excluded.volume, vol_ratio=excluded.vol_ratio, "
        "sec=excluded.sec, top=excluded.top, rall=excluded.rall, reddit_n=excluded.reddit_n, news_n=excluded.news_n",
        (date.today().isoformat(), p.get("market_day"), p.get("close"), p.get("change_pct"), p.get("volume"),
         p.get("vol_ratio"), json.dumps(sec, ensure_ascii=False), json.dumps(top, ensure_ascii=False),
         json.dumps(rall, ensure_ascii=False), counts.get("reddit", 0), counts.get("news", 0)),
    )
    conn.commit()
    export(conn, cfg)
    log.info("дневник: запись за %s", date.today().isoformat())


# ---------------- экспорт ----------------

def export(conn, cfg: dict) -> None:
    """data/journal.md (читать глазами) и data/journal.csv (для таблиц)."""
    out_dir = resolve_path(cfg["paths"]["db"]).parent
    rows = conn.execute("SELECT * FROM journal ORDER BY date DESC").fetchall()

    md = ["# GME — дневник\n", "Свежие записи сверху. Цена — последний торговый день перед дайджестом.\n"]
    for r in rows:
        line = f"\n## {r['date']}\n"
        if r["close"] is not None:
            vol = f", объём ×{r['vol_ratio']:.1f} к среднему" if r["vol_ratio"] else ""
            line += f"\n**Цена** ({r['market_day']}): {r['close']:.2f} ({r['change_pct']:+.1f}%){vol}\n"
        for s in json.loads(r["sec"] or "[]"):
            line += f"\n- 🏛 **{s['form']}** — {s['meaning']} ([документ]({s['url']}))"
        for t in json.loads(r["top"] or "[]"):
            line += f"\n- **{t['score']}/10 [{t['category']}]** {t['title']} — {t['summary']} ([ссылка]({t['url']}))"
        rall = json.loads(r["rall"] or "[]")
        if rall:
            best = ", ".join(f"r/{h['sub']} #{h['best_rank'] + 1}" for h in rall[:3])
            line += f"\n- 🔥 В топ-100 r/all: {len(rall)} пост(ов) ({best})"
        line += f"\n\n_Прошло фильтр за сутки: Reddit {r['reddit_n']}, новости {r['news_n']}._\n"
        md.append(line)
    (out_dir / "journal.md").write_text("".join(md), encoding="utf-8")

    with open(out_dir / "journal.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["date", "market_day", "close", "change_pct", "volume", "vol_ratio", "sec_forms",
                    "top_score", "top_titles", "rall_posts", "rall_best_rank", "reddit_n", "news_n"])
        for r in rows:
            sec = json.loads(r["sec"] or "[]")
            top = json.loads(r["top"] or "[]")
            rall = json.loads(r["rall"] or "[]")
            w.writerow([r["date"], r["market_day"], r["close"], r["change_pct"], r["volume"], r["vol_ratio"],
                        "; ".join(s["form"] for s in sec), max((t["score"] for t in top), default=""),
                        " | ".join(t["title"] for t in top), len(rall),
                        (min(h["best_rank"] for h in rall) + 1) if rall else "", r["reddit_n"], r["news_n"]])


# ---------------- паттерны ----------------

HORIZONS = (1, 5, 20)  # торговых дней


def _event_days(conn, cfg: dict) -> dict[str, set[str]]:
    ev: dict[str, set[str]] = {}

    def add(kind: str, d: str) -> None:
        ev.setdefault(kind, set()).add(d[:10])

    for r in conn.execute("SELECT meta FROM items WHERE kind='sec'"):
        m = json.loads(r["meta"])
        form, d = m["form"].replace("SCHEDULE ", "SC "), m["filing_date"]
        if form.startswith("8-K"):
            add("SEC 8-K (отчётность, item 2.02)" if "2.02" in (m.get("items") or "") else "SEC 8-K (прочее)", d)
        elif form == "4":
            add("SEC Form 4 (инсайдеры)", d)
        elif form.startswith("SC 13D"):
            add("SEC 13D/13D-A (активист)", d)
        elif form.startswith("SC 13G"):
            add("SEC 13G/13G-A (пассивный >5%)", d)
        elif form.startswith(("10-Q", "10-K")):
            add("SEC 10-Q/10-K", d)
    for r in conn.execute("SELECT DISTINCT substr(ts,1,10) d FROM rall_hits"):
        add("Пост в топ-100 r/all", r["d"])
    for r in conn.execute("SELECT date, top FROM journal"):
        if any(t["score"] >= 8 for t in json.loads(r["top"] or "[]")):
            add("Материал с оценкой ≥8", r["date"])
    big = cfg["price"]["alert_move_pct"]
    rows = conn.execute("SELECT date, close FROM prices ORDER BY date").fetchall()
    for prev, cur in zip(rows, rows[1:]):
        if abs(cur["close"] / prev["close"] - 1) * 100 >= big:
            add(f"День с движением ≥{big}%", cur["date"])
    return ev


def _fwd_returns(dates: list[str], closes: list[float], event_day: str) -> dict[int, float] | None:
    """База — закрытие торгового дня ДО события; доходность к закрытию через k торговых дней,
    считая день события (или первый торговый день после него) первым."""
    import bisect
    i = bisect.bisect_left(dates, event_day)  # первый торговый день >= события
    if i == 0 or i >= len(dates):
        return None
    base = closes[i - 1]
    return {k: closes[i + k - 1] / base - 1 for k in HORIZONS if i + k - 1 < len(dates)}


def _stats(values: list[float]) -> str:
    if not values:
        return f"{'—':>22}"
    pos = sum(v > 0 for v in values) / len(values) * 100
    return f"{statistics.mean(values) * 100:+6.1f}% / {statistics.median(values) * 100:+6.1f}% {pos:4.0f}%↑"


def patterns(conn, cfg: dict, since: str | None = None) -> str:
    rows = conn.execute("SELECT date, close FROM prices WHERE date>=? ORDER BY date",
                        (since or "2021-01-01",)).fetchall()
    if len(rows) < 30:
        return "Мало ценовых данных — запустите сбор (история цен подтянется автоматически)."
    dates = [r["date"] for r in rows]
    closes = [r["close"] for r in rows]

    lines = [f"Период: {dates[0]} … {dates[-1]} ({len(dates)} торговых дней)",
             "Доходность от закрытия ДО события: среднее / медиана, доля положительных; n — число дней-событий",
             "",
             f"{'событие':<34}{'n':>5}   " + "   ".join(f"{'через ' + str(k) + ' дн.':^22}" for k in HORIZONS)]

    def row(name: str, days) -> str:
        per = {k: [] for k in HORIZONS}
        n = 0
        for d in sorted(days):
            if d < dates[0]:
                continue
            fr = _fwd_returns(dates, closes, d)
            if not fr:
                continue
            n += 1
            for k, v in fr.items():
                per[k].append(v)
        return f"{name:<34}{n:>5}   " + "   ".join(_stats(per[k]) for k in HORIZONS)

    lines.append(row("любой день (база для сравнения)", dates[1:]))
    lines.append("")
    for name, days in sorted(_event_days(conn, cfg).items()):
        lines.append(row(name, days))
    lines += ["",
              "Как читать: смотрите на отличие от строки «любой день». При n < 20 различия почти всегда шум;",
              "много строк = много сравнений, часть «паттернов» найдётся случайно. Это описание прошлого,",
              "а не прогноз и не повод для сделок."]
    return "\n".join(lines)
