"""Слой 1 — дешёвые эвристики без API. Размечает items.passed_filter / filter_reason."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from difflib import SequenceMatcher

from core import iso, log, now_utc


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _set(conn, item_id: str, passed: bool, reason: str) -> None:
    conn.execute("UPDATE items SET passed_filter=?, filter_reason=? WHERE id=?", (int(passed), reason, item_id))


# ---------------- Reddit ----------------

def reddit_verdict(meta: dict, title: str, text: str, sub_cfg: dict, rcfg: dict) -> tuple[bool, str]:
    if meta.get("stickied"):
        return False, "stickied"
    low_title = (title or "").lower()
    if any(p.lower() in low_title for p in rcfg.get("excluded_title_patterns", [])):
        return False, "service_post"
    flair = (meta.get("flair") or "").lower()
    for bad in rcfg["excluded_flairs"]:
        if bad.lower() in flair:
            return False, f"flair:{meta.get('flair')}"
    if meta.get("ups") is None:  # RSS-режим: апвотов нет, берём позицию в топе
        if meta.get("rank", 0) >= rcfg.get("rss_top_n", 10):
            return False, "low_rank"
    elif meta["ups"] < sub_cfg["min_upvotes"]:
        return False, "low_upvotes"
    link = meta.get("link", "")
    has_link = bool(link) and not any(d in link for d in rcfg["media_domains"])
    has_text = len((text or "").strip()) >= 40
    if not has_text and not has_link:
        return False, "no_text_no_link"
    return True, "ok"


def filter_reddit(conn, cfg: dict) -> None:
    rcfg = cfg["reddit"]
    subs = {s["name"].lower(): s for s in rcfg["subreddits"]}
    since = iso(now_utc() - timedelta(hours=cfg["scoring"]["lookback_hours"]))
    # перепроверяем всё свежее и ещё не скоренное: апвоты растут между сборами
    rows = conn.execute(
        "SELECT id,title,text,meta FROM items WHERE kind='reddit' AND scored_at IS NULL AND created_utc>=?",
        (since,),
    ).fetchall()
    passed = 0
    for r in rows:
        meta = json.loads(r["meta"])
        sub_cfg = subs.get(meta.get("sub", "").lower())
        if not sub_cfg:
            continue
        ok, reason = reddit_verdict(meta, r["title"], r["text"], sub_cfg, rcfg)
        _set(conn, r["id"], ok, reason)
        passed += ok
    conn.commit()
    log.info("фильтр reddit: %d из %d свежих прошли", passed, len(rows))


# ---------------- News ----------------

def norm_title(t: str) -> str:
    t = re.sub(r"\s+[-–|]\s+[^-–|]{2,40}$", "", t or "")  # хвост « - Publisher»
    return re.sub(r"[^a-z0-9 ]+", " ", t.lower()).strip()


def filter_news(conn, cfg: dict) -> None:
    ncfg = cfg["news"]
    stop = [s.lower() for s in ncfg["stop_words"]]
    window = iso(now_utc() - timedelta(days=ncfg["dedup_window_days"]))
    max_age = now_utc() - timedelta(hours=ncfg["max_age_hours"])

    new = conn.execute(
        "SELECT id,title,created_utc FROM items WHERE kind='news' AND passed_filter IS NULL ORDER BY first_seen, rowid"
    ).fetchall()
    # уже принятые новости в окне — эталон для дедупликации
    kept = [norm_title(r["title"]) for r in conn.execute(
        "SELECT title FROM items WHERE kind='news' AND passed_filter=1 AND first_seen>=?", (window,))]

    passed = 0
    for r in new:
        title = r["title"] or ""
        low = title.lower()
        if _parse(r["created_utc"]) < max_age:
            _set(conn, r["id"], False, "old")
            continue
        hit = next((s for s in stop if s in low), None)
        if hit:
            _set(conn, r["id"], False, f"stop:{hit}")
            continue
        nt = norm_title(title)
        if any(SequenceMatcher(None, nt, k).ratio() >= ncfg["title_similarity"] for k in kept):
            _set(conn, r["id"], False, "duplicate")
            continue
        kept.append(nt)
        _set(conn, r["id"], True, "ok")
        passed += 1
    conn.commit()
    log.info("фильтр news: %d из %d новых прошли", passed, len(new))


def run(conn, cfg: dict) -> None:
    filter_reddit(conn, cfg)
    filter_news(conn, cfg)
    # SEC не фильтруется: любой новый филинг из списка форм идёт в дайджест
    conn.execute("UPDATE items SET passed_filter=1, filter_reason='sec' WHERE kind='sec' AND passed_filter IS NULL")
    conn.commit()
