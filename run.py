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
import time
from collections.abc import Callable
from datetime import datetime, timedelta

import alerts
import digest
import filter as heuristics
import journal
import scorer
from core import (Http, PermanentError, RunLock, connect_db, health_fail, health_ok, iso, kv_get, kv_set,
                  load_config, log, source_log,
                  now_utc, setup_logging, upsert_item, write_beacon, ROOT)
from sources import news, price, reddit, sec


def _source_tasks(cfg: dict, conn, http, ctx: dict) -> list[tuple[str, Callable[[], str]]]:
    """Каждый источник — независимая задача (имя, функция). Функция сама сохраняет данные,
    возвращает строку для лога и бросает исключение при ошибке. Результаты для алертов — в ctx."""
    snap_ts = iso()
    snap_since = now_utc() - timedelta(hours=cfg["reddit"]["velocity"]["max_post_age_hours"] * 2)
    tasks: list[tuple[str, Callable[[], str]]] = []

    def reddit_task(sub_cfg: dict) -> Callable[[], str]:
        def run() -> str:
            posts = reddit.fetch_subreddit(http, sub_cfg, cfg["reddit"])
            new = sum(upsert_item(conn, p) for p in posts)
            for p in posts:
                if p["meta"]["ups"] is not None and datetime.fromisoformat(p["created_utc"]) >= snap_since:
                    conn.execute("INSERT OR IGNORE INTO reddit_snapshots(post_id,ts,ups) VALUES(?,?,?)",
                                 (p["id"], snap_ts, p["meta"]["ups"]))
            conn.commit()
            return f"{len(posts)} постов, новых {new}"
        return run

    def news_task(feed_cfg: dict) -> Callable[[], str]:
        def run() -> str:
            items = news.fetch_feed(http, feed_cfg)
            new = sum(upsert_item(conn, it) for it in items)
            conn.commit()
            return f"{len(items)} записей, новых {new}"
        return run

    def rall_task() -> str:
        # внимание всего Reddit: GME-сабы в топе r/all
        hits = reddit.fetch_r_all(http, cfg["reddit"]["r_all"]["limit"])
        tracked = {s["name"].lower() for s in cfg["reddit"]["subreddits"]}
        ctx["rall_hits"] = journal.store_rall(conn, hits, tracked)
        return f"{len(hits)} постов, из отслеживаемых сабов {len(ctx['rall_hits'])}"

    def sec_task() -> str:
        filings = sec.fetch_filings(http, cfg["sec"])
        new = [f["id"] for f in filings if upsert_item(conn, f)]
        conn.commit()
        if not kv_get(conn, "sec_initialized"):
            # первый запуск: историю не алертим, в дайджест — только последние N дней
            cutoff = iso(now_utc() - timedelta(days=cfg["sec"]["first_run_lookback_days"]))
            conn.execute("UPDATE items SET digest_sent_at='baseline' WHERE kind='sec' AND created_utc<?", (cutoff,))
            conn.commit()
            kv_set(conn, "sec_initialized", iso())
            return f"первый запуск — {len(new)} филингов записаны как база, алертов нет"
        ctx["new_sec"].extend(new)
        return f"{len(filings)} филингов в выборке, новых {len(new)}"

    def price_task() -> str:
        if not conn.execute("SELECT 1 FROM prices LIMIT 1").fetchone():
            journal.store_bars(conn, price.fetch_history(http, cfg["price"]["ticker"], "max"))
            log.info("price: загружена вся история цен для дневника")
        move = price.fetch_day_move(http, cfg["price"]["ticker"])
        journal.store_bars(conn, move["bars"])
        ctx["move"] = move
        return f"{move['ticker']} {move['price']:.2f} ({move['change_pct']:+.2f}%)"

    for sub_cfg in cfg["reddit"]["subreddits"]:
        tasks.append((f"reddit:r/{sub_cfg['name']}", reddit_task(sub_cfg)))
    if cfg["reddit"].get("r_all", {}).get("enabled"):
        tasks.append(("reddit:r/all", rall_task))
    for feed_cfg in cfg["news"]["feeds"]:
        tasks.append((f"news:{feed_cfg['name']}", news_task(feed_cfg)))
    tasks.append(("sec", sec_task))
    tasks.append(("price", price_task))
    return tasks


def _filter_and_alert(conn, cfg: dict, ctx: dict) -> None:
    """Фильтр + алерты по тому, что уже собрано. Безопасно вызывать повторно:
    каждый алерт отправляется один раз (таблица alerts)."""
    heuristics.run(conn, cfg)
    alerts.check_sec(conn, cfg, ctx["new_sec"])
    if ctx["move"]:
        alerts.check_price(conn, cfg, ctx["move"])
    alerts.check_velocity(conn, cfg)
    alerts.check_rall(conn, cfg, ctx["rall_hits"])


def collect(cfg: dict) -> None:
    """Сбор по всем источникам. Источники независимы: упавшие повторяются раундами
    (30с → 2мин → 10мин по умолчанию), остальные в это время уже обработаны и
    алерты по ним отправлены. После последней попытки источник помечается недоступным."""
    conn = connect_db(cfg)
    http = Http(cfg, conn)
    ctx: dict = {"new_sec": [], "rall_hits": [], "move": None}
    pending = _source_tasks(cfg, conn, http, ctx)
    delays = [0, *cfg["health"]["retry_delays_sec"]]
    errors: dict[str, str] = {}

    for attempt, delay in enumerate(delays, 1):
        if delay:
            log.info("повтор через %d с (попытка %d/%d): %s", delay, attempt, len(delays),
                     ", ".join(name for name, _ in pending))
            time.sleep(delay)
        failed, any_ok = [], False
        for name, task in pending:
            try:
                summary = task()
            except PermanentError as e:  # ошибка настройки — повтор не поможет
                errors[name] = str(e)
                health_fail(conn, name, str(e))
                continue
            except Exception as e:  # noqa: BLE001 — любая ошибка источника
                errors[name] = str(e)
                failed.append((name, task))
                source_log.warning("%s: попытка %d/%d не удалась: %s", name, attempt, len(delays), e)
                continue
            health_ok(conn, name)
            any_ok = True
            log.info("%s: %s%s", name, summary, f" (с {attempt}-й попытки)" if attempt > 1 else "")
        if any_ok or attempt == 1:
            _filter_and_alert(conn, cfg, ctx)
        pending = failed
        if not pending:
            break

    for name, _ in pending:  # исчерпали повторы
        health_fail(conn, name, errors[name])
    alerts.check_source_health(conn, cfg)

    conn.execute("DELETE FROM reddit_snapshots WHERE ts<?", (iso(now_utc() - timedelta(days=7)),))
    conn.commit()
    kv_set(conn, "last_collect", iso())
    write_beacon(cfg)
    down = conn.execute("SELECT COUNT(*) n FROM source_health WHERE fail_since IS NOT NULL").fetchone()["n"]
    log.info("сбор завершён%s", f", недоступных источников: {down}" if down else "")


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
        # дайджест ждёт, если сбор ещё повторяет упавшие источники
        with RunLock(cfg, wait_sec=1500 if args.command == "digest" else 0):
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
