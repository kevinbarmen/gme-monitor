"""Команды владельца в Telegram-боте. Не демон: `run.py tg` раз в несколько минут забирает
новые сообщения (getUpdates) и выходит.

  /trash  — ответом на сообщение с постом из дайджеста: пост помечается как ошибочно
            пропущенный фильтром и копируется в evals/candidates/ для пополнения eval-набора.
  /help   — подсказка.

Принимаются только сообщения из чата GME_TG_CHAT_ID; остальные игнорируются.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime

import requests

from core import ROOT, esc, kv_get, kv_set, log, send_telegram

CANDIDATES = ROOT / "evals" / "candidates"

HELP = ("Команды:\n/trash — ответь этой командой на сообщение с постом из дайджеста, если пост мусор. "
        "Он будет помечен и попадёт в кандидаты eval-набора.")


def fetch_updates(conn) -> list[dict]:
    token = os.getenv("GME_TG_BOT_TOKEN")
    if not token:
        return []
    offset = int(kv_get(conn, "tg_offset", "0"))
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                     params={"offset": offset, "timeout": 0, "allowed_updates": json.dumps(["message"])}, timeout=20)
    r.raise_for_status()
    updates = r.json().get("result", [])
    if updates:
        kv_set(conn, "tg_offset", str(updates[-1]["update_id"] + 1))
    return updates


def save_candidate(conn, item_id: str) -> str | None:
    """Копирует материал в evals/candidates/ в формате eval-набора. Возвращает имя файла."""
    r = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if r is None:
        return None
    meta = json.loads(r["meta"] or "{}")
    CANDIDATES.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", item_id) + ".json"
    candidate = {
        "id": f"eval:{item_id}",
        "label": "junk",
        "note": "Помечен владельцем командой /trash: фильтр пропустил, а это мусор",
        "labeled_by": "owner (/trash)",
        "labeled_at": datetime.now().isoformat(timespec="seconds"),
        "kind": r["kind"], "source": r["source"], "bias": r["bias"], "title": r["title"], "text": r["text"] or "",
        "meta": {k: meta[k] for k in ("flair", "link") if meta.get(k)},
        "model_verdict": {"score": r["score"], "category": r["category"], "summary": r["summary"],
                          "prompt_version": r["prompt_version"]},
    }
    (CANDIDATES / name).write_text(json.dumps(candidate, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return name


def handle_trash(conn, cfg: dict, msg: dict) -> None:
    reply = msg.get("reply_to_message")
    if not reply:
        send_telegram(cfg, "Команду /trash нужно отправить <b>ответом</b> на сообщение с постом из дайджеста.",
                      reply_to=msg["message_id"])
        return
    row = conn.execute("SELECT item_id FROM tg_messages WHERE message_id=?", (reply["message_id"],)).fetchone()
    if row is None:
        send_telegram(cfg, "Это сообщение не пост из дайджеста (шапку и алерты пометить нельзя).",
                      reply_to=msg["message_id"])
        return
    item_id = row["item_id"]
    conn.execute("UPDATE items SET user_label='trash' WHERE id=?", (item_id,))
    conn.commit()
    name = save_candidate(conn, item_id)
    title = conn.execute("SELECT title FROM items WHERE id=?", (item_id,)).fetchone()["title"]
    log.info("/trash: %s помечен как мусор, кандидат %s", item_id, name)
    send_telegram(cfg, f"🗑 Записал как мусор: <i>{esc(title[:120])}</i>\nКандидат в eval-набор: "
                       f"<code>evals/candidates/{esc(name)}</code>", reply_to=msg["message_id"])


def run(conn, cfg: dict) -> int:
    """Обрабатывает накопившиеся команды. Возвращает число обработанных."""
    owner = str(os.getenv("GME_TG_CHAT_ID") or "")
    handled = 0
    for upd in fetch_updates(conn):
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip()
        if str(msg.get("chat", {}).get("id")) != owner:
            log.warning("TG: сообщение из чужого чата %s проигнорировано", msg.get("chat", {}).get("id"))
            continue
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if command == "/trash":
            handle_trash(conn, cfg, msg)
        elif command in ("/help", "/start"):
            send_telegram(cfg, HELP, reply_to=msg["message_id"])
        else:
            continue  # обычный текст и неизвестные команды молча пропускаем
        handled += 1
    return handled
