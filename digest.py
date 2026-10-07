"""Ежедневный дайджест: шапка (цена, SEC-филинги, служебные строки) + по сообщению на материал.
Материалы идут отдельными сообщениями, чтобы на любой можно было ответить /trash."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import journal
from core import esc, iso, kv_get, kv_set, log, now_utc, send_telegram

BIAS_ICON = {"бычий сабреддит": "🐂", "нейтральный": "⚖️", "официальный документ": "🏛"}


def _line_bias(bias: str) -> str:
    return f"{BIAS_ICON.get(bias, '•')} {esc(bias)}" if bias else ""


def service_notes(conn) -> list[str]:
    """Служебные строки суточной сводки: качество ответов модели и недоступные источники."""
    notes = []
    s = conn.execute(
        "SELECT COALESCE(SUM(n_invalid),0) inv, COALESCE(SUM(n_unscored),0) uns, COALESCE(SUM(n_injection),0) inj "
        "FROM scoring_runs WHERE ts>=?", (iso(now_utc() - timedelta(hours=24)),)).fetchone()
    if s["inv"] or s["uns"] or s["inj"]:
        notes.append(f"🧪 Скоринг за сутки: невалидных ответов модели {s['inv']}, не оценено {s['uns']}, "
                     f"попыток инъекции {s['inj']}")
    down = conn.execute("SELECT source, fail_since FROM source_health WHERE fail_since IS NOT NULL "
                        "ORDER BY source").fetchall()
    if down:
        notes.append("🔧 Источники недоступны: " + ", ".join(
            f"{esc(r['source'])} (с {esc(r['fail_since'][5:16].replace('T', ' '))} UTC)" for r in down))
    return notes


def day_stats(conn) -> dict:
    """Счётчики за последние 24 часа — для строки сводки и трейса."""
    since = iso(now_utc() - timedelta(hours=24))
    one = lambda sql: conn.execute(sql, (since,)).fetchone()[0]  # noqa: E731
    return {
        "collected": one("SELECT COUNT(*) FROM items WHERE kind IN ('reddit','news') AND first_seen>=?"),
        "passed_filter": one("SELECT COUNT(*) FROM items WHERE kind IN ('reddit','news') AND passed_filter=1 "
                             "AND first_seen>=?"),
        "scored": one("SELECT COUNT(*) FROM items WHERE score_status='ok' AND scored_at>=?"),
        "unscored": one("SELECT COUNT(*) FROM items WHERE score_status='unscored' AND scored_at>=?"),
        # встретились при сборе снова, но уже были оценены — токены не тратились
        "cache_skipped": one("SELECT COUNT(*) FROM items WHERE scored_at IS NOT NULL AND last_seen>=? "
                             "AND last_seen>scored_at"),
        # отсеяны как дубль уже принятого материала — в скоринг не пошли
        "duplicates": one("SELECT COUNT(*) FROM items WHERE filter_reason='duplicate' AND first_seen>=?"),
    }


def weekly_cost_line(conn, cfg: dict) -> str | None:
    """Раз в неделю — суммарные расходы на Claude за 7 дней."""
    if datetime.now().weekday() != cfg["budget"]["weekly_cost_weekday"]:
        return None
    since = (datetime.now().date() - timedelta(days=7)).isoformat()
    r = conn.execute("SELECT COALESCE(SUM(cost_usd),0) c, COALESCE(SUM(input_tokens),0) i, "
                     "COALESCE(SUM(output_tokens),0) o FROM usage WHERE day>=?", (since,)).fetchone()
    ev = conn.execute("SELECT COALESCE(SUM(cost_usd),0) c FROM usage WHERE day>=? AND purpose='eval'",
                      (since,)).fetchone()["c"]
    extra = f", из них evals ${ev:.2f}" if ev else ""
    return f"💰 Claude за 7 дней: ${r['c']:.2f} (вход {r['i'] // 1000}k / выход {r['o'] // 1000}k токенов{extra})"


def build(conn, cfg: dict, scoring_error: str | None) -> tuple[str, list[tuple[str, str]], list[str]]:
    """Возвращает (шапка, [(item_id, текст сообщения)], id всех материалов дайджеста)."""
    dcfg, scfg = cfg["digest"], cfg["scoring"]
    since = iso(now_utc() - timedelta(hours=scfg["lookback_hours"]))

    top = conn.execute(
        "SELECT * FROM items WHERE kind IN ('reddit','news') AND digest_sent_at IS NULL AND score>=? "
        "AND user_label IS NULL AND created_utc>=? ORDER BY score DESC, created_utc DESC LIMIT ?",
        (scfg["min_score"], since, dcfg["top_n"]),
    ).fetchall()

    fallback = []
    if scoring_error and not top:
        # Claude недоступен — показываем самое заметное по эвристикам, без саммари
        fallback = conn.execute(
            "SELECT * FROM items WHERE kind IN ('reddit','news') AND digest_sent_at IS NULL AND passed_filter=1 "
            "AND scored_at IS NULL AND created_utc>=? ORDER BY COALESCE(json_extract(meta,'$.ups'), 0) DESC, "
            "COALESCE(json_extract(meta,'$.rank'), 99) LIMIT ?",
            (since, dcfg["top_n"]),
        ).fetchall()

    filings = conn.execute(
        "SELECT * FROM items WHERE kind='sec' AND digest_sent_at IS NULL ORDER BY created_utc DESC"
    ).fetchall()

    today = datetime.now().strftime("%d.%m.%Y")
    quiet = not top and not fallback and not filings
    head = [f"🤫 <b>GME: тишина — {today}</b>\n0 материалов прошло порог за сутки." if quiet
            else f"📰 <b>GME дайджест — {today}</b>"]
    p = journal.price_summary(conn)
    if p:
        vol = f", объём ×{p['vol_ratio']:.1f} к среднему" if p["vol_ratio"] else ""
        head.append(f"💵 GME {p['close']:.2f} ({p['change_pct']:+.1f}%) на закрытии {p['market_day']}{vol}")
    rall = conn.execute("SELECT COUNT(DISTINCT post_id) n, MIN(rank) best FROM rall_hits WHERE ts>=?",
                        (iso(now_utc() - timedelta(hours=24)),)).fetchone()
    if rall["n"]:
        head.append(f"🔥 Внимание Reddit: {rall['n']} пост(ов) GME-сабов в топ-100 r/all "
                    f"(лучшее место #{rall['best'] + 1})")
    if scoring_error:
        head.append(f"⚠ Скоринг Claude не сработал: <code>{esc(scoring_error[:300])}</code>")
    head.extend(service_notes(conn))

    if filings:
        lines = ["🏛 <b>Новые SEC-филинги</b>"]
        for f in filings:
            m = json.loads(f["meta"])
            form = "Form 4" if m["form"] == "4" else m["form"]
            lines.append(f"• <b>{esc(form)}</b> ({esc(m['filing_date'])}) — {esc(m['meaning'])}\n"
                         f"  {esc(f['url'])}")
        head.append("\n".join(lines))

    messages: list[tuple[str, str]] = []
    for i, r in enumerate(top, 1):
        messages.append((r["id"],
                         f"<b>{i}. [{esc(r['category'])}] {r['score']}/10</b> — {esc(r['title'])}\n"
                         f"{esc(r['summary'])}\n"
                         f"{esc(r['source'])} · {_line_bias(r['bias'])}\n{esc(r['url'])}"))
    for i, r in enumerate(fallback, 1):
        m = json.loads(r["meta"])
        extra = f" · {m['ups']} апв." if m.get("ups") is not None else ""
        messages.append((r["id"],
                         f"<b>{i}.</b> {esc(r['title'])}\n{esc(r['source'])}{extra} · {_line_bias(r['bias'])}\n"
                         f"{esc(r['url'])}"))

    if not quiet and not messages:
        head.append("Из Reddit/новостей 0 материалов прошло порог.")

    s = day_stats(conn)
    head.append(f"<i>За сутки: собрано {s['collected']} · прошли фильтр {s['passed_filter']} · оценено {s['scored']} · "
                f"пропущено по кэшу {s['cache_skipped']} · дублей {s['duplicates']} · в дайджесте {len(messages)}</i>")
    cost = weekly_cost_line(conn, cfg)
    if cost:
        head.append(cost)
    if messages:
        head.append("<i>Мусор в дайджесте? Ответь на сообщение с постом командой /trash.</i>")

    sent_ids = [r["id"] for r in (*top, *fallback, *filings)]
    return "\n\n".join(head), messages, sent_ids


def run(conn, cfg: dict, scoring_error: str | None) -> dict | None:
    """Отправляет дайджест. Возвращает счётчики для трейса или None, если не отправлен."""
    head, messages, ids = build(conn, cfg, scoring_error)
    if not send_telegram(cfg, head):
        log.error("дайджест не отправлен")
        return None
    now = iso()
    for item_id, text in messages:
        for mid in send_telegram(cfg, text, silent=True):  # без звука: уведомление уже было от шапки
            if mid:
                conn.execute("INSERT OR REPLACE INTO tg_messages(message_id,item_id,sent_at) VALUES(?,?,?)",
                             (mid, item_id, now))
    conn.executemany("UPDATE items SET digest_sent_at=? WHERE id=?", [(now, i) for i in ids])
    conn.commit()
    kv_set(conn, "last_digest_date", datetime.now().date().isoformat())
    log.info("дайджест отправлен: %d материалов", len(ids))
    try:
        journal.write_entry(conn, cfg, now)
    except Exception:  # noqa: BLE001 — дневник не должен ронять дайджест
        log.exception("дневник: не удалось записать")
    return {**day_stats(conn), "in_digest": len(messages), "sec_filings": len(ids) - len(messages)}


def already_sent_today(conn) -> bool:
    return kv_get(conn, "last_digest_date") == datetime.now().date().isoformat()
