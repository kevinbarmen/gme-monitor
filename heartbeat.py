"""Heartbeat для всех моих систем. Архитектурно независим от GME-монитора:
ничего не импортирует из проекта, свой конфиг heartbeat.yaml, свои ключи в .env.

Каждая система после успешного цикла пишет timestamp в свой файл-маячок.
Новая система = новый маячок + строка в heartbeat.yaml, код не меняется.

  python heartbeat.py            отправить статус в TG
  python heartbeat.py --dry-run  только напечатать
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent


def read_beacon(path: Path) -> datetime | None:
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8").strip()
    try:
        ts = datetime.fromtimestamp(float(raw), timezone.utc)  # unix time
    except ValueError:
        try:
            ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))  # ISO 8601
        except ValueError:
            ts = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)  # хотя бы mtime
    return ts if ts.tzinfo else ts.astimezone()


def build_status(cfg: dict) -> str:
    now = datetime.now(timezone.utc)
    lines = []
    for s in cfg["systems"]:
        path = Path(os.path.expanduser(s["beacon"]))
        ts = read_beacon(path)
        verb_ok = s.get("ok_word", "жива")
        if ts is None:
            lines.append(f"{s['name']}: ⚠ нет маячка ({path})")
            continue
        local = ts.astimezone().strftime("%d.%m %H:%M")
        age_h = (now - ts).total_seconds() / 3600
        if age_h > s["max_age_hours"]:
            lines.append(f"{s['name']}: ⚠ молчит с {local}")
        else:
            lines.append(f"{s['name']}: {verb_ok}, {s.get('label', 'последний цикл')} {local}")
    return " / ".join(lines)


def send(text: str) -> bool:
    token = os.getenv("HEARTBEAT_TG_BOT_TOKEN")
    chat_id = os.getenv("HEARTBEAT_TG_CHAT_ID")
    if not token or not chat_id:
        print("HEARTBEAT_TG_BOT_TOKEN / HEARTBEAT_TG_CHAT_ID не заданы", file=sys.stderr)
        return False
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat_id, "text": text}, timeout=20)
    if r.status_code != 200:
        print(f"Telegram {r.status_code}: {r.text[:300]}", file=sys.stderr)
    return r.status_code == 200


def main() -> int:
    if sys.stdout:  # под pythonw потока нет
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # эмодзи в консоли Windows
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(HERE / "heartbeat.yaml"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    load_dotenv(HERE / ".env")
    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    text = "💓 " + build_status(cfg)
    print(text)
    if args.dry_run:
        return 0
    return 0 if send(text) else 1


if __name__ == "__main__":
    sys.exit(main())
