"""Ежедневный дайджест: топ-N материалов + блок SEC-филингов."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from core import esc, iso, kv_get, kv_set, log, now_utc, send_telegram

BIAS_ICON = {"бычий сабреддит": "🐂", "нейтральный": "⚖️", "официальный документ": "🏛"}


def _line_bias(bias: str) -> str:
    return f"{BIAS_ICON.get(bias, '•')} {esc(bias)}" if bias else ""


def build(conn, cfg: dict, scoring_error: str | None) -> tuple[str, list[str]]:
    dcfg, scfg = cfg["digest"], cfg["scoring"]
    since = iso(now_utc() - timedelta(hours=scfg["lookback_hours"]))

    top = conn.execute(
        "SELECT * FROM items WHERE kind IN ('reddit','news') AND digest_sent_at IS NULL AND score>=? "
        "AND created_utc>=? ORDER BY score DESC, created_utc DESC LIMIT ?",
        (scfg["min_score"], since, dcfg["top_n"]),
    ).fetchall()

    fallback = []
    if scoring_error and not top:
        # Claude недоступен — показываем самое заметное по эвристикам, без саммари
        fallback = conn.execute(
            "SELECT * FROM items WHERE kind IN ('reddit','news') AND digest_sent_at IS NULL AND passed_filter=1 "
            "AND created_utc>=? ORDER BY COALESCE(json_extract(meta,'$.ups'), 0) DESC, COALESCE(json_extract(meta,'$.rank'), 99) LIMIT ?",
            (since, dcfg["top_n"]),
        ).fetchall()

    filings = conn.execute(
        "SELECT * FROM items WHERE kind='sec' AND digest_sent_at IS NULL ORDER BY created_utc DESC"
    ).fetchall()

    today = datetime.now().strftime("%d.%m.%Y")
    parts = [f"📰 <b>GME дайджест — {today}</b>"]
    if scoring_error:
        parts.append(f"⚠ Скоринг Claude не сработал: <code>{esc(scoring_error[:300])}</code>")

    if filings:
        lines = ["🏛 <b>Новые SEC-филинги</b>"]
        for f in filings:
            m = json.loads(f["meta"])
            form = "Form 4" if m["form"] == "4" else m["form"]
            lines.append(f"• <b>{esc(form)}</b> ({esc(m['filing_date'])}) — {esc(m['meaning'])}\n"
                         f"  {esc(f['url'])}")
        parts.append("\n".join(lines))

    for i, r in enumerate(top, 1):
        parts.append(
            f"<b>{i}. [{esc(r['category'])}] {r['score']}/10</b> — {esc(r['title'])}\n"
            f"{esc(r['summary'])}\n"
            f"{esc(r['source'])} · {_line_bias(r['bias'])}\n{esc(r['url'])}"
        )
    for i, r in enumerate(fallback, 1):
        m = json.loads(r["meta"])
        extra = f" · {m['ups']} апв." if m.get("ups") is not None else ""
        parts.append(f"<b>{i}.</b> {esc(r['title'])}\n{esc(r['source'])}{extra} · {_line_bias(r['bias'])}\n"
                     f"{esc(r['url'])}")

    if not top and not fallback and not filings:
        return "🤫 <b>GME: тишина</b> — 0 материалов прошло порог за сутки.", []
    if not top and not fallback:
        parts.append("Из Reddit/новостей 0 материалов прошло порог.")

    sent_ids = [r["id"] for r in (*top, *fallback, *filings)]
    return "\n\n".join(parts), sent_ids


def run(conn, cfg: dict, scoring_error: str | None) -> bool:
    text, ids = build(conn, cfg, scoring_error)
    if not send_telegram(cfg, text):
        log.error("дайджест не отправлен")
        return False
    now = iso()
    conn.executemany("UPDATE items SET digest_sent_at=? WHERE id=?", [(now, i) for i in ids])
    conn.commit()
    kv_set(conn, "last_digest_date", datetime.now().date().isoformat())
    log.info("дайджест отправлен: %d материалов", len(ids))
    return True


def already_sent_today(conn) -> bool:
    return kv_get(conn, "last_digest_date") == datetime.now().date().isoformat()
