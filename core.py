"""Общая инфраструктура: конфиг, логи, SQLite, HTTP с rate limit, Telegram, маячок, лок."""
from __future__ import annotations

import html
import json
import logging
import os
import re
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
CREATE TABLE IF NOT EXISTS scoring_runs (
    ts TEXT PRIMARY KEY, prompt_version TEXT, model TEXT, n_items INTEGER, n_valid INTEGER,
    n_invalid INTEGER,              -- невалидных ответов в первом запросе
    n_unscored INTEGER,             -- осталось «не оценён» после повтора
    n_injection INTEGER, input_tokens INTEGER, output_tokens INTEGER
);
CREATE TABLE IF NOT EXISTS fundamentals (  -- последние значения из отчётности, с филингом-источником
    key TEXT PRIMARY KEY, label TEXT, value REAL, unit TEXT, period_start TEXT, period_end TEXT,
    form TEXT, filed TEXT, accession TEXT, url TEXT, concept TEXT, stale INTEGER, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS events (        -- события по типам с ценами: в день события и через N торг. дней
    type TEXT, trading_day TEXT, source TEXT, item_id TEXT, close_0 REAL,
    closes_after TEXT,              -- JSON {"1": close, "5": close, "20": close}; null — день ещё не наступил
    updated_at TEXT, PRIMARY KEY (type, trading_day)
);
CREATE TABLE IF NOT EXISTS impact_eval (   -- еженедельно: корреляция оценки важности и движения цены
    week_end TEXT PRIMARY KEY, week TEXT, cumulative TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS tg_messages (  -- какое сообщение дайджеста про какой материал (для /trash)
    message_id INTEGER PRIMARY KEY, item_id TEXT, sent_at TEXT
);
CREATE TABLE IF NOT EXISTS usage (     -- каждый запрос к Claude: для бюджета и стоимости
    ts TEXT, day TEXT, purpose TEXT, model TEXT, prompt_version TEXT,
    input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL
);
CREATE INDEX IF NOT EXISTS usage_day ON usage(day);
CREATE TABLE IF NOT EXISTS cycles (    -- трейс каждого прогона collect/digest
    ts TEXT PRIMARY KEY, kind TEXT, duration_sec REAL, data TEXT
);
-- дневник: цена, внимание Reddit, записи по дням
CREATE TABLE IF NOT EXISTS prices (
    date TEXT PRIMARY KEY, open REAL, high REAL, low REAL, close REAL, volume INTEGER
);
CREATE TABLE IF NOT EXISTS rall_hits (   -- посты отслеживаемых сабов в топ-100 r/all
    post_id TEXT, ts TEXT, rank INTEGER, sub TEXT, title TEXT, url TEXT, PRIMARY KEY (post_id, ts)
);
CREATE TABLE IF NOT EXISTS journal (
    date TEXT PRIMARY KEY,          -- локальная дата дайджеста
    market_day TEXT, close REAL, change_pct REAL, volume INTEGER, vol_ratio REAL,
    sec TEXT, top TEXT, rall TEXT,  -- JSON
    reddit_n INTEGER, news_n INTEGER
);
"""


# колонки items, добавленные после первого релиза
MIGRATIONS = {
    "score_status": "TEXT",     # ok | unscored (невалидный ответ модели — значения не выдумываются)
    "injection": "INTEGER",     # модель заметила попытку prompt injection
    "prompt_version": "TEXT",   # хэш prompts/scorer.md, которым оценён материал
    "last_seen": "TEXT",        # когда материал последний раз встретился при сборе
    "user_label": "TEXT",       # ручная пометка из Telegram (/trash)
    "context": "TEXT",          # JSON: масштаб и аргументы за/против (блок «что это значит»)
    "event_type": "TEXT",       # тип события из analogs.event_types ('-' — не отслеживается)
    "details": "TEXT",          # JSON: разобранная суть филинга (Form 4: кто, сколько, на какую сумму)
}


def connect_db(cfg: dict) -> sqlite3.Connection:
    path = resolve_path(cfg["paths"]["db"])
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    have = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
    for col, decl in MIGRATIONS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE items ADD COLUMN {col} {decl}")
    conn.commit()
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
    now = iso()
    if exists:
        # last_seen — для счётчика «пропущено по кэшу»: материал встретился снова, но уже оценён
        conn.execute("UPDATE items SET meta=?, last_seen=? WHERE id=?", (meta, now, item["id"]))
        return False
    conn.execute(
        "INSERT INTO items(id,kind,source,bias,title,url,text,meta,created_utc,first_seen,last_seen) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (item["id"], item["kind"], item["source"], item.get("bias"), item.get("title"), item.get("url"),
         item.get("text"), meta, item.get("created_utc"), now, now),
    )
    return True


# ---------------- токены, стоимость, трейс ----------------

def cost_usd(cfg: dict, model: str, input_tokens: int, output_tokens: int) -> float:
    price_in, price_out = cfg["budget"]["pricing_usd_per_mtok"].get(model, (0, 0))
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


def record_usage(conn, cfg: dict, purpose: str, model: str, prompt_version: str,
                 input_tokens: int, output_tokens: int) -> None:
    conn.execute(
        "INSERT INTO usage(ts,day,purpose,model,prompt_version,input_tokens,output_tokens,cost_usd) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (iso(), datetime.now().date().isoformat(), purpose, model, prompt_version, input_tokens, output_tokens,
         cost_usd(cfg, model, input_tokens, output_tokens)))
    conn.commit()


def tokens_today(conn) -> int:
    row = conn.execute("SELECT COALESCE(SUM(input_tokens+output_tokens),0) n FROM usage WHERE day=?",
                       (datetime.now().date().isoformat(),)).fetchone()
    return row["n"]


def record_cycle(conn, kind: str, duration_sec: float, data: dict) -> None:
    """Трейс прогона: одна строка в таблицу cycles и одна строка в лог."""
    conn.execute("INSERT OR REPLACE INTO cycles(ts,kind,duration_sec,data) VALUES(?,?,?,?)",
                 (iso(), kind, round(duration_sec, 1), json.dumps(data, ensure_ascii=False)))
    conn.commit()
    log.info("трейс %s (%.0f с): %s", kind, duration_sec, json.dumps(data, ensure_ascii=False))


def alert_already_sent(conn, key: str) -> bool:
    return conn.execute("SELECT 1 FROM alerts WHERE key=?", (key,)).fetchone() is not None


def mark_alert(conn, key: str, text: str) -> None:
    conn.execute("INSERT OR IGNORE INTO alerts(key,sent_at,text) VALUES(?,?,?)", (key, iso(), text))
    conn.commit()


# ---------------- source health ----------------

class PermanentError(Exception):
    """Ошибка настройки источника (нет ключа и т.п.) — повторять бессмысленно."""


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


def _visible_len(s: str) -> int:
    """Длина текста так, как её считает Telegram: без HTML-тегов (длинные ссылки в href не в счёт)."""
    return len(re.sub(r"<[^>]+>", "", s))


def _split(text: str) -> list[str]:
    """Делит сообщение на части по лимиту Telegram. Режет только по границам абзацев и строк,
    чтобы не разорвать HTML-тег; строку без тегов длиннее лимита режет по символам."""
    units: list[tuple[str, str]] = []  # (разделитель перед куском, кусок)
    for block in text.split("\n\n"):
        if _visible_len(block) <= TG_LIMIT:
            units.append(("\n\n", block))
            continue
        for i, line in enumerate(block.split("\n")):
            sep = "\n\n" if i == 0 else "\n"
            while "<" not in line and len(line) > TG_LIMIT:
                units.append((sep, line[:TG_LIMIT]))
                line, sep = line[TG_LIMIT:], "\n"
            units.append((sep, line))
    parts, cur = [], ""
    for sep, unit in units:
        piece = cur + sep + unit if cur else unit
        if _visible_len(piece) <= TG_LIMIT:
            cur = piece
        else:
            parts.append(cur)
            cur = unit
    if cur:
        parts.append(cur)
    return parts


def send_telegram(cfg: dict, text: str, silent: bool = False, reply_to: int | None = None) -> list[int]:
    """Отправляет HTML-сообщение в отдельного GME-бота. Возвращает id отправленных сообщений
    (пустой список = не отправлено). Без токена — печатает в консоль и возвращает [0]."""
    token = os.getenv("GME_TG_BOT_TOKEN")
    chat_id = os.getenv("GME_TG_CHAT_ID")
    if not token or not chat_id:
        if cfg["telegram"].get("dry_run_if_no_token", True):
            print("\n===== [TG dry-run] =====\n" + text + "\n========================\n")
            return [0]
        log.error("GME_TG_BOT_TOKEN / GME_TG_CHAT_ID не заданы в .env")
        return []
    ids: list[int] = []
    ok = True
    for part in _split(text):
        payload = {"chat_id": chat_id, "text": part, "parse_mode": "HTML", "disable_web_page_preview": True,
                   "disable_notification": silent}
        if reply_to:
            payload["reply_to_message_id"] = reply_to
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=20)
            if r.status_code != 200:
                log.error("Telegram %s: %s", r.status_code, r.text[:300])
                ok = False
            else:
                ids.append(r.json()["result"]["message_id"])
        except requests.RequestException as e:
            log.error("Telegram недоступен: %s", e)
            ok = False
    return ids if ok else []


# ---------------- beacon / lock ----------------

def write_beacon(cfg: dict) -> None:
    path = resolve_path(cfg["paths"]["beacon"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(iso(datetime.now().astimezone()), encoding="utf-8")


class RunLock:
    """Не даём двум запускам (cron + ручной) работать одновременно."""

    def __init__(self, cfg: dict, stale_after_sec: int = 3600, wait_sec: int = 0):
        self.path = resolve_path(cfg["paths"]["lock"])
        self.stale = stale_after_sec
        self.wait = wait_sec  # сколько ждать освобождения, прежде чем сдаться

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + self.wait
        while self._held() and time.time() < deadline:
            time.sleep(10)
        if self._held():
            raise RuntimeError(f"уже запущено (лок {self.path}); если это не так — удалите файл")
        self.path.write_text(str(os.getpid()))
        return self

    def _held(self) -> bool:
        try:
            return time.time() - self.path.stat().st_mtime < self.stale
        except FileNotFoundError:
            return False

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)
