"""Общая инфраструктура: конфиг, логи, SQLite, HTTP с rate limit, Telegram, маячок, лок."""
from __future__ import annotations

import html
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent

log = logging.getLogger("gme")
source_log = logging.getLogger("gme.sources")  # ошибки сбора — в отдельный файл


# ---------------- config ----------------

def load_config(path: str | Path | None = None) -> dict:
    load_dotenv(ROOT / ".env")
    with open(path or ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve_path(p: str) -> Path:
    path = Path(os.path.expanduser(p))
    return path if path.is_absolute() else ROOT / path


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(ts: float | datetime | None = None) -> str:
    if ts is None:
        ts = now_utc()
    if isinstance(ts, (int, float)):
        ts = datetime.fromtimestamp(ts, timezone.utc)
    return ts.isoformat(timespec="seconds")


# ---------------- logging ----------------

def setup_logging(cfg: dict, verbose: bool = False) -> None:
    logs_dir = resolve_path(cfg["paths"]["logs"])
    logs_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    main = RotatingFileHandler(logs_dir / "monitor.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    main.setFormatter(fmt)
    errors = RotatingFileHandler(logs_dir / "source_errors.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    errors.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)

    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.handlers[:] = [main, console] if sys.stderr else [main]  # под pythonw консоли нет
    source_log.addHandler(errors)  # плюс наследует main/console от "gme"


# ---------------- SQLite ----------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,            -- reddit:<id> / news:<hash> / sec:<accession>
    kind TEXT NOT NULL,             -- reddit | news | sec
    source TEXT NOT NULL,           -- r/Superstonk, Google News, SEC EDGAR
    bias TEXT,
    title TEXT,
    url TEXT,
    text TEXT,
    meta TEXT,                      -- JSON: апвоты, флеар, форма филинга и т.п.
    created_utc TEXT,
    first_seen TEXT NOT NULL,
    passed_filter INTEGER,          -- NULL = ещё не фильтровали
    filter_reason TEXT,
    scored_at TEXT,
    score INTEGER,
    category TEXT,
    summary TEXT,
    digest_sent_at TEXT
);
CREATE INDEX IF NOT EXISTS items_kind_created ON items(kind, created_utc);
CREATE TABLE IF NOT EXISTS reddit_snapshots (
    post_id TEXT, ts TEXT, ups INTEGER, PRIMARY KEY (post_id, ts)
);
CREATE TABLE IF NOT EXISTS alerts (
    key TEXT PRIMARY KEY, sent_at TEXT, text TEXT
);
CREATE TABLE IF NOT EXISTS source_health (
    source TEXT PRIMARY KEY, last_ok TEXT, last_error TEXT, last_error_at TEXT,
    fail_since TEXT, down_alert_sent INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS http_cache (
    url TEXT PRIMARY KEY, etag TEXT, last_modified TEXT, body TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


def connect_db(cfg: dict) -> sqlite3.Connection:
    path = resolve_path(cfg["paths"]["db"])
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def kv_get(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))
    conn.commit()


def upsert_item(conn, item: dict) -> bool:
    """Вставляет новый материал. Возвращает True, если он новый.
    Для уже известных обновляет только meta (апвоты растут)."""
    exists = conn.execute("SELECT 1 FROM items WHERE id=?", (item["id"],)).fetchone()
    meta = json.dumps(item.get("meta") or {}, ensure_ascii=False)
    if exists:
        conn.execute("UPDATE items SET meta=? WHERE id=?", (meta, item["id"]))
        return False
    conn.execute(
        "INSERT INTO items(id,kind,source,bias,title,url,text,meta,created_utc,first_seen) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (item["id"], item["kind"], item["source"], item.get("bias"), item.get("title"), item.get("url"),
         item.get("text"), meta, item.get("created_utc"), iso()),
    )
    return True


def alert_already_sent(conn, key: str) -> bool:
    return conn.execute("SELECT 1 FROM alerts WHERE key=?", (key,)).fetchone() is not None


def mark_alert(conn, key: str, text: str) -> None:
    conn.execute("INSERT OR IGNORE INTO alerts(key,sent_at,text) VALUES(?,?,?)", (key, iso(), text))
    conn.commit()


# ---------------- source health ----------------

def health_ok(conn, source: str) -> None:
    conn.execute(
        "INSERT INTO source_health(source,last_ok,fail_since,down_alert_sent) VALUES(?,?,NULL,0) "
        "ON CONFLICT(source) DO UPDATE SET last_ok=excluded.last_ok, fail_since=NULL, down_alert_sent=0",
        (source, iso()),
    )
    conn.commit()


def health_fail(conn, source: str, error: str) -> None:
    source_log.error("источник %s недоступен: %s", source, error)
    now = iso()
    conn.execute(
        "INSERT INTO source_health(source,last_error,last_error_at,fail_since) VALUES(?,?,?,?) "
        "ON CONFLICT(source) DO UPDATE SET last_error=excluded.last_error, last_error_at=excluded.last_error_at, "
        "fail_since=COALESCE(source_health.fail_since, excluded.fail_since)",
        (source, error[:500], now, now),
    )
    conn.commit()


# ---------------- HTTP ----------------

class Http:
    """requests.Session с нормальным UA, паузой между запросами к одному хосту
    и условными GET (ETag / Last-Modified) — не перекачиваем одно и то же."""

    def __init__(self, cfg: dict, conn: sqlite3.Connection):
        self.cfg = cfg["http"]
        self.conn = conn
        self.s = requests.Session()
        self.s.headers["User-Agent"] = self.cfg["user_agent"]
        self._last_hit: dict[str, float] = {}

    def _host_cfg(self, host: str) -> dict:
        return (self.cfg.get("hosts") or {}).get(host, {})

    def _throttle(self, host: str) -> None:
        interval = self._host_cfg(host).get("min_interval_sec", self.cfg["min_interval_sec"])
        wait = interval - (time.monotonic() - self._last_hit.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    def _request(self, url: str, headers: dict) -> requests.Response:
        host = urlparse(url).netloc
        for attempt in range(2):
            self._throttle(host)
            r = self.s.get(url, headers=headers, timeout=self.cfg["timeout_sec"])
            if r.status_code != 429 or attempt:
                return r
            # один повтор после паузы, которую просит сервер (но не дольше 60 с)
            retry = r.headers.get("Retry-After") or r.headers.get("x-ratelimit-reset") or "10"
            try:
                delay = min(float(retry) + 1, 60)
            except ValueError:
                delay = 10
            log.debug("429 от %s, жду %.0f с", host, delay)
            time.sleep(delay)
        return r

    def get(self, url: str, headers: dict | None = None, cache: bool = True) -> tuple[str, bool]:
        """Возвращает (body, changed). changed=False — сервер ответил 304, тело из кэша."""
        host = urlparse(url).netloc
        h = dict(headers or {})
        if "User-Agent" not in h and self._host_cfg(host).get("user_agent"):
            h["User-Agent"] = self._host_cfg(host)["user_agent"]
        cached = None
        if cache:
            cached = self.conn.execute("SELECT * FROM http_cache WHERE url=?", (url,)).fetchone()
            if cached:
                if cached["etag"]:
                    h["If-None-Match"] = cached["etag"]
                if cached["last_modified"]:
                    h["If-Modified-Since"] = cached["last_modified"]
        r = self._request(url, h)
        if r.status_code == 304 and cached:
            return cached["body"], False
        r.raise_for_status()
        if cache:
            self.conn.execute(
                "INSERT INTO http_cache(url,etag,last_modified,body,fetched_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(url) DO UPDATE SET etag=excluded.etag, last_modified=excluded.last_modified, "
                "body=excluded.body, fetched_at=excluded.fetched_at",
                (url, r.headers.get("ETag"), r.headers.get("Last-Modified"), r.text, iso()),
            )
            self.conn.commit()
        return r.text, True

    def get_json(self, url: str, headers: dict | None = None, cache: bool = True):
        body, _ = self.get(url, headers=headers, cache=cache)
        return json.loads(body)


# ---------------- Telegram ----------------

TG_LIMIT = 4000


def esc(s: str | None) -> str:
    return html.escape(s or "", quote=False)


def _split(text: str) -> list[str]:
    parts, cur = [], ""
    for block in text.split("\n\n"):
        piece = (cur + "\n\n" + block) if cur else block
        if len(piece) <= TG_LIMIT:
            cur = piece
            continue
        if cur:
            parts.append(cur)
        while len(block) > TG_LIMIT:  # на случай одного огромного блока
            parts.append(block[:TG_LIMIT])
            block = block[TG_LIMIT:]
        cur = block
    if cur:
        parts.append(cur)
    return parts


def send_telegram(cfg: dict, text: str) -> bool:
    """Отправляет HTML-сообщение в отдельного GME-бота. Без токена — печатает в консоль."""
    token = os.getenv("GME_TG_BOT_TOKEN")
    chat_id = os.getenv("GME_TG_CHAT_ID")
    if not token or not chat_id:
        if cfg["telegram"].get("dry_run_if_no_token", True):
            print("\n===== [TG dry-run] =====\n" + text + "\n========================\n")
            return True
        log.error("GME_TG_BOT_TOKEN / GME_TG_CHAT_ID не заданы в .env")
        return False
    ok = True
    for part in _split(text):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": part, "parse_mode": "HTML",
                      "disable_web_page_preview": True},
                timeout=20,
            )
            if r.status_code != 200:
                log.error("Telegram %s: %s", r.status_code, r.text[:300])
                ok = False
        except requests.RequestException as e:
            log.error("Telegram недоступен: %s", e)
            ok = False
    return ok


# ---------------- beacon / lock ----------------

def write_beacon(cfg: dict) -> None:
    path = resolve_path(cfg["paths"]["beacon"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(iso(datetime.now().astimezone()), encoding="utf-8")


class RunLock:
    """Не даём двум запускам (cron + ручной) работать одновременно."""

    def __init__(self, cfg: dict, stale_after_sec: int = 3600):
        self.path = resolve_path(cfg["paths"]["lock"])
        self.stale = stale_after_sec

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and time.time() - self.path.stat().st_mtime < self.stale:
            raise RuntimeError(f"уже запущено (лок {self.path}); если это не так — удалите файл")
        self.path.write_text(str(os.getpid()))
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)
