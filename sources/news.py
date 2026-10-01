"""Новостные RSS-фиды."""
from __future__ import annotations

import calendar
import hashlib
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import feedparser

from core import iso

TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "guccounter", "ncid", ".tsrc"}


def normalize_url(url: str) -> str:
    p = urlparse(url.strip())
    q = [(k, v) for k, v in parse_qsl(p.query) if k.lower() not in TRACKING]
    return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), "", urlencode(q), ""))


def fetch_feed(http, feed_cfg: dict) -> list[dict]:
    body, _ = http.get(feed_cfg["url"])
    parsed = feedparser.parse(body)
    if parsed.bozo and not parsed.entries:
        raise RuntimeError(f"не RSS: {parsed.bozo_exception}")
    out = []
    for e in parsed.entries:
        link = e.get("link") or ""
        if not link:
            continue
        url = normalize_url(link)
        ts = e.get("published_parsed") or e.get("updated_parsed")
        publisher = ""
        if e.get("source") and isinstance(e.source, dict):
            publisher = e.source.get("title", "")
        out.append({
            "id": "news:" + hashlib.sha1(url.encode()).hexdigest()[:16],
            "kind": "news",
            "source": f"{feed_cfg['name']}" + (f" / {publisher}" if publisher else ""),
            "bias": feed_cfg.get("bias", ""),
            "title": e.get("title", "").strip(),
            "url": link,
            "text": _strip_html(e.get("summary", ""))[:1000],
            "created_utc": iso(calendar.timegm(ts)) if ts else iso(),
            "meta": {"feed": feed_cfg["name"], "norm_url": url, "publisher": publisher},
        })
    return out


def _strip_html(s: str) -> str:
    import re
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()
