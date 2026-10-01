"""GME-монитор — точка входа.

  python run.py collect           сбор + фильтр + проверка алертов (cron: каждые 2 часа)
  python run.py digest [--force]  скоринг Claude + дайджест (cron: раз в день)
  python run.py status            состояние источников и базы
  python run.py print-cron        строки crontab по настройкам из config.yaml
  python run.py journal           пересобрать data/journal.md и journal.csv
  python run.py patterns [--since 2023-01-01]  цена после событий разного типа vs обычный день
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta

import alerts
import digest
import filter as heuristics
import journal
import scorer
from core import (Http, RunLock, connect_db, health_fail, health_ok, iso, kv_get, kv_set, load_config, log,
                  now_utc, setup_logging, upsert_item, write_beacon, ROOT)
from sources import news, price, reddit, sec


def collect(cfg: dict) -> None:
    conn = connect_db(cfg)
    http = Http(cfg, conn)
    snap_ts = iso()
    snap_since = now_utc() - timedelta(hours=cfg["reddit"]["velocity"]["max_post_age_hours"] * 2)

    # Reddit
    for sub_cfg in cfg["reddit"]["subreddits"]:
        name = f"reddit:r/{sub_cfg['name']}"
        try:
            posts = reddit.fetch_subreddit(http, sub_cfg, cfg["reddit"])
        except Exception as e:  # noqa: BLE001
            health_fail(conn, name, str(e))
            continue
        new = sum(upsert_item(conn, p) for p in posts)
        for p in posts:
            if p["meta"]["ups"] is not None and datetime.fromisoformat(p["created_utc"]) >= snap_since:
                conn.execute("INSERT OR IGNORE INTO reddit_snapshots(post_id,ts,ups) VALUES(?,?,?)",
                             (p["id"], snap_ts, p["meta"]["ups"]))
        conn.commit()
        health_ok(conn, name)
        log.info("%s: %d постов, новых %d", name, len(posts), new)

    # Внимание всего Reddit: GME-сабы в топе r/all
    rall_hits: list[dict] = []
    if cfg["reddit"].get("r_all", {}).get("enabled"):
        try:
            hits = reddit.fetch_r_all(http, cfg["reddit"]["r_all"]["limit"])
            tracked = {s["name"].lower() for s in cfg["reddit"]["subreddits"]}
            rall_hits = journal.store_rall(conn, hits, tracked)
            health_ok(conn, "reddit:r/all")
            log.info("r/all: %d постов, из отслеживаемых сабов %d", len(hits), len(rall_hits))
        except Exception as e:  # noqa: BLE001
            health_fail(conn, "reddit:r/all", str(e))

    # Новости
    for feed_cfg in cfg["news"]["feeds"]:
        name = f"news:{feed_cfg['name']}"
        try:
            items = news.fetch_feed(http, feed_cfg)
        except Exception as e:  # noqa: BLE001
            health_fail(conn, name, str(e))
            continue
        new = sum(upsert_item(conn, it) for it in items)
        conn.commit()
        health_ok(conn, name)
        log.info("%s: %d записей, новых %d", name, len(items), new)

    # SEC
    new_sec: list[str] = []
    try:
        filings = sec.fetch_filings(http, cfg["sec"])
        new_sec = [f["id"] for f in filings if upsert_item(conn, f)]
        conn.commit()
        health_ok(conn, "sec")
        log.info("sec: %d филингов в выборке, новых %d", len(filings), len(new_sec))
        if not kv_get(conn, "sec_initialized"):
            # первый запуск: историю не алертим, в дайджест — только последние N дней
            cutoff = iso(now_utc() - timedelta(days=cfg["sec"]["first_run_lookback_days"]))
            conn.execute("UPDATE items SET digest_sent_at='baseline' WHERE kind='sec' AND created_utc<?", (cutoff,))
            conn.commit()
            kv_set(conn, "sec_initialized", iso())
            log.info("sec: первый запуск — %d филингов записаны как база, алертов нет", len(new_sec))
            new_sec = []
    except Exception as e:  # noqa: BLE001
        health_fail(conn, "sec", str(e))

    heuristics.run(conn, cfg)

    # Алерты
    alerts.check_sec(conn, cfg, new_sec)
    try:
        if not conn.execute("SELECT 1 FROM prices LIMIT 1").fetchone():
            journal.store_bars(conn, price.fetch_history(http, cfg["price"]["ticker"], "max"))
            log.info("price: загружена вся история цен для дневника")
        move = price.fetch_day_move(http, cfg["price"]["ticker"])
        journal.store_bars(conn, move["bars"])
        health_ok(conn, "price")
        log.info("price: %s %.2f (%+.2f%%)", move["ticker"], move["price"], move["change_pct"])
        alerts.check_price(conn, cfg, move)
    except Exception as e:  # noqa: BLE001
        health_fail(conn, "price", str(e))
    alerts.check_velocity(conn, cfg)
    alerts.check_rall(conn, cfg, rall_hits)
    alerts.check_source_health(conn, cfg)

    conn.execute("DELETE FROM reddit_snapshots WHERE ts<?", (iso(now_utc() - timedelta(days=7)),))
    conn.commit()
    kv_set(conn, "last_collect", iso())
    write_beacon(cfg)
    log.info("сбор завершён")


def run_digest(cfg: dict, force: bool) -> None:
    conn = connect_db(cfg)
    if digest.already_sent_today(conn) and not force:
        log.info("дайджест за сегодня уже отправлен (--force чтобы повторить)")
        return
    heuristics.run(conn, cfg)
    _, err = scorer.run(conn, cfg)
    if err:
        log.error("скоринг: %s", err)
    digest.run(conn, cfg, err)


def status(cfg: dict) -> None:
    conn = connect_db(cfg)
    print(f"последний сбор:    {kv_get(conn, 'last_collect', '—')}")
    print(f"последний дайджест: {kv_get(conn, 'last_digest_date', '—')}\n")
    print("источники:")
    for r in conn.execute("SELECT * FROM source_health ORDER BY source"):
        state = "OK" if not r["fail_since"] else f"ПАДАЕТ с {r['fail_since']}"
        print(f"  {r['source']:<28} {state:<36} last_ok={r['last_ok'] or '—'}")
        if r["fail_since"]:
            print(f"      {r['last_error']}")
    print("\nматериалы:")
    for r in conn.execute("SELECT kind, COUNT(*) n, SUM(passed_filter=1) p, SUM(scored_at IS NOT NULL) s "
                          "FROM items GROUP BY kind"):
        print(f"  {r['kind']:<8} всего {r['n']:<6} прошли фильтр {r['p'] or 0:<6} оценено {r['s'] or 0}")
    print("\nпричины отсева (топ):")
    for r in conn.execute("SELECT filter_reason, COUNT(*) n FROM items WHERE passed_filter=0 "
                          "GROUP BY filter_reason ORDER BY n DESC LIMIT 10"):
        print(f"  {r['filter_reason']:<30} {r['n']}")


def print_cron(cfg: dict, windows: bool) -> None:
    sch = cfg["schedule"]
    py = ROOT / (".venv/Scripts/pythonw.exe" if windows else ".venv/bin/python")  # pythonw — без окна консоли
    dh, dm = cfg["digest"]["time"].split(":")
    hh, hm = sch["heartbeat_time"].split(":")
    if windows:
        every = sch["collect_every_hours"] * 60
        print(f'schtasks /Create /TN "GME collect" /SC MINUTE /MO {every} /ST {sch.get("collect_start_hour", 0):02d}:{sch["collect_minute"]:02d} '
              f'/TR "\\"{py}\\" \\"{ROOT / "run.py"}\\" collect" /F')
        print(f'schtasks /Create /TN "GME digest" /SC DAILY /ST {dh}:{dm} '
              f'/TR "\\"{py}\\" \\"{ROOT / "run.py"}\\" digest" /F')
        print(f'schtasks /Create /TN "Heartbeat" /SC DAILY /ST {hh}:{hm} '
              f'/TR "\\"{py}\\" \\"{ROOT / "heartbeat.py"}\\"" /F')
        return
    print("# GME-монитор (сверьте минуты с crontab CS2-модели: crontab -l)")
    print(f"{sch['collect_minute']} {sch.get('collect_start_hour', 0)}-23/{sch['collect_every_hours']} * * * cd {ROOT} && {py} run.py collect "
          f">> data/logs/cron.log 2>&1")
    print(f"{int(dm)} {int(dh)} * * * cd {ROOT} && {py} run.py digest >> data/logs/cron.log 2>&1")
    print(f"{int(hm)} {int(hh)} * * * cd {ROOT} && {py} heartbeat.py >> data/logs/cron.log 2>&1")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # эмодзи/кириллица в консоли Windows
        if stream:  # под pythonw потоков нет
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="GME-монитор")
    ap.add_argument("command", choices=["collect", "digest", "status", "print-cron", "journal", "patterns"])
    ap.add_argument("--force", action="store_true", help="digest: отправить повторно за сегодня")
    ap.add_argument("--windows", action="store_true", help="print-cron: команды schtasks для Windows")
    ap.add_argument("--since", help="patterns: начало периода, YYYY-MM-DD (по умолчанию 2021-01-01)")
    ap.add_argument("--config", help="путь к config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.command == "print-cron":
        print_cron(cfg, args.windows)
        return 0
    if args.command == "status":
        status(cfg)
        return 0
    if args.command == "journal":
        journal.export(connect_db(cfg), cfg)
        print("data/journal.md, data/journal.csv обновлены")
        return 0
    if args.command == "patterns":
        print(journal.patterns(connect_db(cfg), cfg, args.since))
        return 0

    setup_logging(cfg, args.verbose)
    try:
        with RunLock(cfg):
            if args.command == "collect":
                collect(cfg)
            else:
                run_digest(cfg, args.force)
    except Exception:
        log.exception("%s упал", args.command)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
