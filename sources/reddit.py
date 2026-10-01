"""Reddit. Три режима, выбираются автоматически:

1. OAuth (если в .env есть REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET) — oauth.reddit.com, полный JSON.
2. Публичный JSON (/r/<sub>/top.json) — полный JSON, но Reddit часто отвечает 403 без авторизации.
3. RSS (/r/<sub>/top/.rss) — работает без авторизации, но БЕЗ апвотов и флеаров:
   порог апвотов заменяется позицией в топе, алерт по апвот-темпу недоступен.
"""
from __future__ import annotations

import html
import os
import re
from urllib.parse import quote_plus

import feedparser
import requests

from core import iso, log

WWW = "https://www.reddit.com"
OAUTH = "https://oauth.reddit.com"

ENDPOINTS = {  # (json-путь, rss-путь)
    "top_day": ("/r/{sub}/top.json?t=day&limit={limit}", "/r/{sub}/top/.rss?t=day&limit={limit}"),
    "hot": ("/r/{sub}/hot.json?limit={limit}", "/r/{sub}/hot/.rss?limit={limit}"),
}

_token: str | None = None
_public_json_blocked = False  # после первого 403 в этом запуске сразу идём в RSS


def _oauth_token(http) -> str | None:
    global _token
    cid, secret = os.getenv("REDDIT_CLIENT_ID"), os.getenv("REDDIT_CLIENT_SECRET")
    if not cid or not secret:
        return None
    if _token is None:
        r = http.s.post(f"{WWW}/api/v1/access_token", auth=(cid, secret),
                        data={"grant_type": "client_credentials"}, timeout=20)
        r.raise_for_status()
        _token = r.json()["access_token"]
    return _token


def _paths(sub_cfg: dict, cfg: dict, rss: bool) -> list[str]:
    sub, limit = sub_cfg["name"], cfg["limit"]
    if sub_cfg.get("search"):
        q = quote_plus(sub_cfg["search"])
        ext = ".rss" if rss else ".json"
        return [f"/r/{sub}/search{ext}?q={q}&restrict_sr=1&sort={s}&t=day&limit={limit}" for s in ("top", "new")]
    return [ENDPOINTS[e][1 if rss else 0].format(sub=sub, limit=limit) for e in cfg["endpoints"]]


# ---------------- JSON ----------------

def _post_from_json(d: dict, sub_cfg: dict, rank: int) -> dict:
    return {
        "id": f"reddit:{d['id']}",
        "kind": "reddit",
        "source": f"r/{d.get('subreddit', sub_cfg['name'])}",
        "bias": sub_cfg.get("bias", ""),
        "title": d.get("title", ""),
        "url": WWW + d.get("permalink", ""),
        "text": d.get("selftext", "") if d.get("is_self") else "",
        "created_utc": iso(d.get("created_utc", 0)),
        "meta": {
            "mode": "json",
            "sub": sub_cfg["name"],
            "flair": d.get("link_flair_text") or "",
            "ups": d.get("ups", d.get("score", 0)),
            "ratio": d.get("upvote_ratio"),
            "comments": d.get("num_comments", 0),
            "is_self": bool(d.get("is_self")),
            "link": "" if d.get("is_self") else d.get("url_overridden_by_dest") or d.get("url") or "",
            "stickied": bool(d.get("stickied")),
            "created_ts": d.get("created_utc", 0),
            "rank": rank,
        },
    }


def _fetch_json(http, base: str, path: str, sub_cfg: dict, headers: dict | None = None) -> list[dict]:
    data = http.get_json(base + path, headers=headers, cache=False)  # апвоты меняются — без кэша
    out = []
    for child in data.get("data", {}).get("children", []):
        d = child.get("data", {})
        if child.get("kind") == "t3" and d.get("id"):
            out.append(_post_from_json(d, sub_cfg, len(out)))
    return out


# ---------------- RSS ----------------

_LINK_RE = re.compile(r'<a href="([^"]+)">\[link\]</a>')
_MD_RE = re.compile(r'<div class="md">(.*?)</div>', re.S)


def _post_from_rss(e, sub_cfg: dict, rank: int) -> dict:
    content = e.content[0].value if e.get("content") else ""
    permalink = e.get("link", "")
    m = _LINK_RE.search(content)
    link = html.unescape(m.group(1)) if m else ""
    is_self = not link or link.rstrip("/") == permalink.rstrip("/")
    md = _MD_RE.search(content)
    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", md.group(1)))).strip() if md else ""
    pid = (e.get("id") or "").removeprefix("t3_")
    ts = e.get("published_parsed") or e.get("updated_parsed")
    import calendar
    created = calendar.timegm(ts) if ts else 0
    return {
        "id": f"reddit:{pid}",
        "kind": "reddit",
        "source": f"r/{sub_cfg['name']}",
        "bias": sub_cfg.get("bias", ""),
        "title": e.get("title", ""),
        "url": permalink,
        "text": text,
        "created_utc": iso(created),
        "meta": {
            "mode": "rss",
            "sub": sub_cfg["name"],
            "flair": "",
            "ups": None,  # RSS не отдаёт апвоты
            "comments": None,
            "is_self": is_self,
            "link": "" if is_self else link,
            "stickied": False,
            "created_ts": created,
            "rank": rank,
        },
    }


def _fetch_rss(http, path: str, sub_cfg: dict) -> list[dict]:
    body, _ = http.get(WWW + path, cache=False)
    parsed = feedparser.parse(body)
    return [_post_from_rss(e, sub_cfg, i) for i, e in enumerate(parsed.entries) if e.get("id")]


# ---------------- public ----------------

def fetch_subreddit(http, sub_cfg: dict, cfg: dict) -> list[dict]:
    """Посты сабреддита (дубликаты между эндпоинтами схлопнуты; при дубле берётся лучший rank).
    Бросает исключение, если ни один эндпоинт не ответил."""
    global _public_json_blocked
    posts: dict[str, dict] = {}
    errors = []

    token = _oauth_token(http)
    mode = "oauth" if token else ("rss" if _public_json_blocked else "json")

    for path in _paths(sub_cfg, cfg, rss=False):
        try:
            if mode == "oauth":
                got = _fetch_json(http, OAUTH, path.replace(".json", ""), sub_cfg,
                                  headers={"Authorization": f"bearer {token}"})
            elif mode == "json":
                got = _fetch_json(http, WWW, path, sub_cfg)
            else:
                break
        except requests.HTTPError as e:
            if mode == "json" and e.response is not None and e.response.status_code == 403:
                log.info("Reddit JSON без авторизации заблокирован (403) — переключаюсь на RSS")
                _public_json_blocked = True
                mode = "rss"
                break
            errors.append(f"{path}: {e}")
            continue
        except Exception as e:  # noqa: BLE001
            errors.append(f"{path}: {e}")
            continue
        for p in got:
            posts.setdefault(p["id"], p)

    if mode == "rss":
        for path in _paths(sub_cfg, cfg, rss=True):
            try:
                got = _fetch_rss(http, path, sub_cfg)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{path}: {e}")
                continue
            for p in got:
                prev = posts.get(p["id"])
                if prev is None or p["meta"]["rank"] < prev["meta"]["rank"]:
                    posts[p["id"]] = p

    if errors and not posts:
        raise RuntimeError("; ".join(errors))
    return list(posts.values())


def fetch_r_all(http, limit: int = 100) -> list[dict]:
    """Топ r/all (hot) через RSS: [{rank, sub, id, title, url, created_ts}].
    Пост GME-сабов здесь = внимание всего Reddit, а не только своего саба."""
    import calendar
    body, _ = http.get(f"{WWW}/r/all/.rss?limit={limit}", cache=False)
    out = []
    for rank, e in enumerate(feedparser.parse(body).entries):
        tags = e.get("tags") or []
        ts = e.get("published_parsed") or e.get("updated_parsed")
        out.append({
            "rank": rank,
            "sub": tags[0]["term"] if tags else "",
            "id": "reddit:" + (e.get("id") or "").removeprefix("t3_"),
            "title": e.get("title", ""),
            "url": e.get("link", ""),
            "created_ts": calendar.timegm(ts) if ts else 0,
        })
    return out
