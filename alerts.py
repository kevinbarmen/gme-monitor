"""Срочные алерты. Ровно три триггера: SEC 8-K/13D/G, движение цены, аномальный апвот-темп.
Никаких алертов по сентименту и «горячим обсуждениям»."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from core import alert_already_sent, esc, iso, log, mark_alert, now_utc, send_telegram


def _send(conn, cfg: dict, key: str, text: str) -> None:
    if alert_already_sent(conn, key):
        return
    if send_telegram(cfg, text):
        mark_alert(conn, key, text)
        log.info("алерт отправлен: %s", key)


# 1. SEC
def check_sec(conn, cfg: dict, new_ids: list[str]) -> None:
    alert_forms = set(cfg["sec"]["alert_forms"])
    for item_id in new_ids:
        r = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        meta = json.loads(r["meta"])
        if meta["form"] not in alert_forms:
            continue
        text = (f"🚨 <b>SEC: новый {esc(meta['form'])}</b> (GameStop)\n"
                f"{esc(meta['meaning'])}\n"
                f"Подан: {esc(meta['filing_date'])}\n{esc(r['url'])}")
        _send(conn, cfg, f"sec:{item_id}", text)


# 2. Цена
def check_price(conn, cfg: dict, move: dict) -> None:
    threshold = cfg["price"]["alert_move_pct"]
    pct = move["change_pct"]
    if abs(pct) < threshold:
        return
    direction = "up" if pct > 0 else "down"
    arrow = "📈" if pct > 0 else "📉"
    text = (f"{arrow} <b>{esc(move['ticker'])} {pct:+.1f}% за день</b> (порог ±{threshold}%)\n"
            f"Цена {move['price']:.2f} vs закрытие {move['prev_close']:.2f} "
            f"(торговый день {move['market_day']})")
    # не чаще одного алерта на направление за торговый день
    _send(conn, cfg, f"price:{move['market_day']}:{direction}", text)


# 3. Апвот-темп
def _ups_per_hour(conn, post_id: str, ups: int, created_ts: float) -> float:
    """Темп по двум последним снимкам; если снимок один — средний с момента публикации."""
    snaps = conn.execute(
        "SELECT ts, ups FROM reddit_snapshots WHERE post_id=? ORDER BY ts DESC LIMIT 2", (post_id,)
    ).fetchall()
    if len(snaps) == 2:
        t1 = datetime.fromisoformat(snaps[0]["ts"])
        t0 = datetime.fromisoformat(snaps[1]["ts"])
        hours = (t1 - t0).total_seconds() / 3600
        if hours >= 0.25:
            return (snaps[0]["ups"] - snaps[1]["ups"]) / hours
    age_h = max((now_utc() - datetime.fromtimestamp(created_ts, timezone.utc)).total_seconds() / 3600, 0.25)
    return ups / age_h


def check_velocity(conn, cfg: dict) -> None:
    from filter import reddit_verdict  # те же флеар-исключения, что и в фильтре

    rcfg = cfg["reddit"]
    vcfg = rcfg["velocity"]
    subs = {s["name"].lower(): s for s in rcfg["subreddits"]}
    since = iso(now_utc() - timedelta(hours=vcfg["max_post_age_hours"]))
    rows = conn.execute("SELECT * FROM items WHERE kind='reddit' AND created_utc>=?", (since,)).fetchall()
    for r in rows:
        meta = json.loads(r["meta"])
        sub_cfg = subs.get(meta.get("sub", "").lower())
        if not sub_cfg or meta.get("ups") is None or meta["ups"] < vcfg["min_upvotes"]:
            continue
        ok, reason = reddit_verdict(meta, r["title"], r["text"], {**sub_cfg, "min_upvotes": 0}, rcfg)
        if not ok:
            continue
        rate = _ups_per_hour(conn, r["id"], meta["ups"], meta["created_ts"])
        if rate < sub_cfg["velocity_ups_per_hour"]:
            continue
        text = (f"⚡ <b>Аномальный апвот-темп в {esc(r['source'])}</b> ({esc(r['bias'])})\n"
                f"{esc(r['title'])}\n"
                f"~{rate:.0f} апв/час (порог {sub_cfg['velocity_ups_per_hour']}), всего {meta['ups']}, "
                f"комм. {meta['comments']}\n{esc(r['url'])}\n"
                f"<i>Это сигнал внимания, не факт — содержание не проверено.</i>")
        _send(conn, cfg, f"velocity:{r['id']}", text)


# 3б. Внимание всего Reddit: пост отслеживаемого саба в топе r/all
#     (замена апвот-темпа, когда Reddit доступен только через RSS)
def check_rall(conn, cfg: dict, hits: list[dict]) -> None:
    acfg = cfg["reddit"]["r_all"]
    max_age = now_utc() - timedelta(hours=acfg["max_post_age_hours"])
    for h in hits:
        if h["rank"] >= acfg["alert_top_n"] or datetime.fromtimestamp(h["created_ts"], timezone.utc) < max_age:
            continue
        text = (f"🔥 <b>Пост из r/{esc(h['sub'])} на #{h['rank'] + 1} в r/all</b>\n"
                f"{esc(h['title'])}\n{esc(h['url'])}\n"
                f"<i>Сигнал внимания всего Reddit, не факт — содержание не проверено.</i>")
        _send(conn, cfg, f"rall:{h['id']}", text)


# Здоровье источников (не «алерт по данным», а сообщение о поломке)
def check_source_health(conn, cfg: dict) -> None:
    limit = now_utc() - timedelta(hours=cfg["health"]["source_down_alert_hours"])
    for r in conn.execute("SELECT * FROM source_health WHERE fail_since IS NOT NULL AND down_alert_sent=0"):
        if datetime.fromisoformat(r["fail_since"]) > limit:
            continue
        text = (f"🔧 <b>Источник {esc(r['source'])} недоступен</b> с {esc(r['fail_since'][:16])} UTC\n"
                f"Последняя ошибка: <code>{esc((r['last_error'] or '')[:300])}</code>\n"
                f"Тишина от него — это поломка, а не отсутствие новостей.")
        if send_telegram(cfg, text):
            conn.execute("UPDATE source_health SET down_alert_sent=1 WHERE source=?", (r["source"],))
    conn.commit()
