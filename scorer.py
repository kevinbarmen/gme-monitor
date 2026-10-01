"""Слой 2 — скоринг через Claude API, раз в день, одним батчем."""
from __future__ import annotations

import json
from datetime import timedelta

import anthropic

from core import iso, log, now_utc

CATEGORIES = ["фундаментал", "событие", "заявление руководства", "анализ", "новости рынка", "хайп"]

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "score": {"type": "integer"},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "keep": {"type": "boolean"},
                    "summary_ru": {"type": "string"},
                },
                "required": ["id", "score", "category", "keep", "summary_ru"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


def pending(conn, cfg: dict) -> list:
    since = iso(now_utc() - timedelta(hours=cfg["scoring"]["lookback_hours"]))
    return conn.execute(
        "SELECT * FROM items WHERE kind IN ('reddit','news') AND passed_filter=1 AND scored_at IS NULL "
        "AND created_utc>=? ORDER BY created_utc DESC LIMIT ?",
        (since, cfg["scoring"]["max_items_per_batch"]),
    ).fetchall()


def _payload(rows, max_chars: int) -> str:
    items = []
    for r in rows:
        meta = json.loads(r["meta"] or "{}")
        entry = {
            "id": r["id"],
            "source": r["source"],
            "bias": r["bias"],
            "title": r["title"],
            "text": (r["text"] or "")[:max_chars],
        }
        if r["kind"] == "reddit":
            entry.update(flair=meta.get("flair"), upvotes=meta.get("ups"), comments=meta.get("comments"),
                         external_link=meta.get("link") or None)
        items.append(entry)
    return json.dumps(items, ensure_ascii=False)


def _call(client, scfg: dict, content: str):
    kwargs = dict(
        model=scfg["model"],
        max_tokens=scfg["max_tokens"],
        system=scfg["system_prompt"],
        output_config={"effort": scfg["effort"], "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": "Материалы для оценки (JSON):\n" + content}],
    )
    if scfg.get("use_fallbacks"):
        try:
            return client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        except anthropic.BadRequestError as e:
            log.warning("fallbacks не приняты (%s) — повтор без них", e)
    return client.messages.create(**kwargs)


def run(conn, cfg: dict) -> tuple[int, str | None]:
    """Скорит всё ожидающее. Возвращает (сколько оценено, ошибка или None)."""
    rows = pending(conn, cfg)
    if not rows:
        log.info("скоринг: нечего оценивать")
        return 0, None
    scfg = cfg["scoring"]
    try:
        client = anthropic.Anthropic()
        resp = _call(client, scfg, _payload(rows, scfg["text_chars_per_item"]))
    except anthropic.APIConnectionError as e:
        return 0, f"Claude API недоступен: {e}"
    except anthropic.APIStatusError as e:
        return 0, f"Claude API {e.status_code}: {e.message}"
    except Exception as e:  # noqa: BLE001 — например, нет ключа
        return 0, f"Claude API: {e}"

    if resp.stop_reason == "refusal":
        return 0, "Claude отказался оценивать батч (refusal)"
    if resp.stop_reason == "max_tokens":
        return 0, "ответ Claude обрезан по max_tokens — уменьшите max_items_per_batch"
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        results = json.loads(text)["items"]
    except (json.JSONDecodeError, KeyError) as e:
        return 0, f"не разобрать ответ Claude: {e}"

    known = {r["id"] for r in rows}
    now = iso()
    n = 0
    for it in results:
        if it["id"] not in known:
            continue
        score = max(1, min(10, int(it["score"])))
        if not it["keep"] or it["category"] == "хайп":
            score = min(score, 3)
        conn.execute("UPDATE items SET scored_at=?, score=?, category=?, summary=? WHERE id=?",
                     (now, score, it["category"], it["summary_ru"], it["id"]))
        n += 1
    # то, что модель пропустила, не оставляем висеть вечно
    conn.execute(
        f"UPDATE items SET scored_at=?, score=0 WHERE scored_at IS NULL AND id IN ({','.join('?' * len(known))})",
        (now, *known))
    conn.commit()
    u = resp.usage
    log.info("скоринг: оценено %d/%d, токены in=%s out=%s", n, len(rows), u.input_tokens, u.output_tokens)
    return n, None
