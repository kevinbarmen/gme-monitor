"""GME-монитор — точка входа.

  python run.py collect           сбор + фильтр + проверка алертов (cron: каждые 2 часа)
  python run.py digest [--force]  скоринг Claude + дайджест (cron: раз в день)
  python run.py tg                забрать команды из Telegram (/trash); раз в несколько минут
  python run.py status            состояние источников и базы
  python run.py print-cron        строки crontab по настройкам из config.yaml
  python run.py journal           пересобрать data/journal.md и journal.csv
  python run.py patterns [--since 2023-01-01]  цена после событий разного типа vs обычный день
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta

import alerts
import context
import digest
import filter as heuristics
import fundamentals
import impact
import journal
import scorer
import tg_commands
from core import (ROOT, Http, PermanentError, RunLock, connect_db, esc, health_fail, health_ok, iso, kv_get,
                  kv_set, load_config, log, now_utc, record_cycle, setup_logging, source_log, tokens_today,
                  upsert_item, write_beacon)
from sources import news, price, reddit, sec


def _source_tasks(cfg: dict, conn, http, ctx: dict) -> list[tuple[str, Callable[[], dict | str]]]:
    """Каждый источник — независимая задача (имя, функция). Функция сама сохраняет данные,
    возвращает строку для лога и бросает исключение при ошибке. Результаты для алертов — в ctx."""
    snap_ts = iso()
    snap_since = now_utc() - timedelta(hours=cfg["reddit"]["velocity"]["max_post_age_hours"] * 2)
    tasks: list[tuple[str, Callable[[], dict | str]]] = []

    def reddit_task(sub_cfg: dict) -> Callable[[], dict]:
        def run() -> dict:
            posts = reddit.fetch_subreddit(http, sub_cfg, cfg["reddit"])
            new = sum(upsert_item(conn, p) for p in posts)
            for p in posts:
                if p["meta"]["ups"] is not None and datetime.fromisoformat(p["created_utc"]) >= snap_since:
                    conn.execute("INSERT OR IGNORE INTO reddit_snapshots(post_id,ts,ups) VALUES(?,?,?)",
                                 (p["id"], snap_ts, p["meta"]["ups"]))
            conn.commit()
            return {"summary": f"{len(posts)} постов, новых {new}", "fetched": len(posts), "new": new}
        return run

    def news_task(feed_cfg: dict) -> Callable[[], dict]:
        def run() -> dict:
            items = news.fetch_feed(http, feed_cfg)
            new = sum(upsert_item(conn, it) for it in items)
            conn.commit()
            return {"summary": f"{len(items)} записей, новых {new}", "fetched": len(items), "new": new}
        return run

    def rall_task() -> str:
        # внимание всего Reddit: GME-сабы в топе r/all
        hits = reddit.fetch_r_all(http, cfg["reddit"]["r_all"]["limit"])
        tracked = {s["name"].lower() for s in cfg["reddit"]["subreddits"]}
        ctx["rall_hits"] = journal.store_rall(conn, hits, tracked)
        return f"{len(hits)} постов, из отслеживаемых сабов {len(ctx['rall_hits'])}"

    def sec_task() -> dict | str:
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
        return {"summary": f"{len(filings)} филингов в выборке, новых {len(new)}", "fetched": len(filings), "new": len(new)}

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
    def fundamentals_task() -> str:
        # раз в день; сразу — если только что появился новый 10-Q/10-K
        new_report = any(
            json.loads(conn.execute("SELECT meta FROM items WHERE id=?", (i,)).fetchone()["meta"])["form"]
            in ("10-Q", "10-K", "10-K/A") for i in ctx["new_sec"])
        return fundamentals.refresh(conn, http, cfg, force=new_report)

    tasks.append(("sec", sec_task))
    tasks.append(("fundamentals", fundamentals_task))
    tasks.append(("price", price_task))
    return tasks


def _filter_and_alert(conn, cfg: dict, ctx: dict) -> dict:
    """Фильтр + алерты по тому, что уже собрано. Безопасно вызывать повторно:
    каждый алерт отправляется один раз (таблица alerts)."""
    counts = heuristics.run(conn, cfg)
    alerts.check_sec(conn, cfg, ctx["new_sec"])
    if ctx["move"]:
        alerts.check_price(conn, cfg, ctx["move"])
    alerts.check_velocity(conn, cfg)
    alerts.check_rall(conn, cfg, ctx["rall_hits"])
    return counts


def collect(cfg: dict) -> None:
    """Сбор по всем источникам. Источники независимы: упавшие повторяются раундами
    (30с → 2мин → 10мин по умолчанию), остальные в это время уже обработаны и
    алерты по ним отправлены. После последней попытки источник помечается недоступным.
    Весь цикл ограничен budget.cycle_timeout_sec."""
    started = time.monotonic()
    timeout = cfg["budget"]["cycle_timeout_sec"]
    conn = connect_db(cfg)
    http = Http(cfg, conn)
    ctx: dict = {"new_sec": [], "rall_hits": [], "move": None}
    pending = _source_tasks(cfg, conn, http, ctx)
    delays = [0, *cfg["health"]["retry_delays_sec"]]
    errors: dict[str, str] = {}
    trace_sources: dict[str, dict] = {}
    filter_counts: dict = {}
    timed_out = False

    for attempt, delay in enumerate(delays, 1):
        if delay:
            if time.monotonic() - started + delay > timeout:
                timed_out = True
                break
            log.info("повтор через %d с (попытка %d/%d): %s", delay, attempt, len(delays),
                     ", ".join(name for name, _ in pending))
            time.sleep(delay)
        failed, any_ok = [], False
        for name, task in pending:
            if time.monotonic() - started > timeout:
                timed_out = True
                errors.setdefault(name, "не успел: таймаут цикла")
                failed.append((name, task))
                continue
            try:
                result = task()
            except PermanentError as e:  # ошибка настройки — повтор не поможет
                errors[name] = str(e)
                health_fail(conn, name, str(e))
                trace_sources[name] = {"ok": False, "error": str(e)[:120]}
                continue
            except Exception as e:  # noqa: BLE001 — любая ошибка источника
                errors[name] = str(e)
                failed.append((name, task))
                source_log.warning("%s: попытка %d/%d не удалась: %s", name, attempt, len(delays), e)
                continue
            if isinstance(result, str):
                result = {"summary": result}
            health_ok(conn, name)
            any_ok = True
            trace_sources[name] = {"ok": True, "attempt": attempt,
                                   **{k: v for k, v in result.items() if k != "summary"}}
            log.info("%s: %s%s", name, result["summary"], f" (с {attempt}-й попытки)" if attempt > 1 else "")
        if any_ok or attempt == 1:
            filter_counts = _filter_and_alert(conn, cfg, ctx) or {}
        pending = failed
        if not pending or timed_out:
            break

    for name, _ in pending:  # исчерпали повторы или время
        health_fail(conn, name, errors[name])
        trace_sources[name] = {"ok": False, "error": errors[name][:120]}
    if timed_out:
        alerts.notify_once(conn, cfg, f"timeout:{datetime.now().date().isoformat()}",
                           f"⏱ <b>GME-монитор: цикл сбора упёрся в таймаут</b> ({timeout} с)\n"
                           f"Не дождались: {esc(', '.join(name for name, _ in pending) or '—')}. "
                           f"Остальные источники обработаны.")
    alerts.check_source_health(conn, cfg)

    conn.execute("DELETE FROM reddit_snapshots WHERE ts<?", (iso(now_utc() - timedelta(days=7)),))
    conn.commit()
    kv_set(conn, "last_collect", iso())
    write_beacon(cfg)
    down = conn.execute("SELECT COUNT(*) n FROM source_health WHERE fail_since IS NOT NULL").fetchone()["n"]
    record_cycle(conn, "collect", time.monotonic() - started, {
        "sources": trace_sources, "filter": filter_counts, "unavailable": down, "timed_out": timed_out,
        "pending_scoring": len(scorer.pending(conn, cfg)),
    })
    log.info("сбор завершён%s", f", недоступных источников: {down}" if down else "")


def run_digest(cfg: dict, force: bool) -> None:
    started = time.monotonic()
    conn = connect_db(cfg)
    if digest.already_sent_today(conn) and not force:
        log.info("дайджест за сегодня уже отправлен (--force чтобы повторить)")
        return
    heuristics.run(conn, cfg)
    tokens_before = tokens_today(conn)
    n_scored, err = scorer.run(conn, cfg)
    if err:
        log.error("скоринг: %s", err)
        if err.startswith(scorer.BUDGET_PREFIX):
            alerts.notify_once(conn, cfg, f"budget:{datetime.now().date().isoformat()}",
                               f"⛔ <b>GME-монитор: {esc(err)}</b>\nСкоринг остановлен до завтра, сбор данных и "
                               f"алерты работают. Лимит — budget.max_tokens_per_day в config.yaml.")
    context_stats = None
    if not err:
        try:
            context_stats = context.run(conn, cfg)  # масштаб и аргументы для материалов с высокой оценкой
        except Exception:  # noqa: BLE001 — контекст не должен ронять дайджест
            log.exception("контекст: ошибка")
    stats = digest.run(conn, cfg, err)

    used = conn.execute(
        "SELECT COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o, COALESCE(SUM(cost_usd),0) c "
        "FROM usage WHERE day=? AND purpose='digest'", (datetime.now().date().isoformat(),)).fetchone()
    left = len(scorer.pending(conn, cfg))
    if left:
        log.warning("скоринг: %d материалов не вошли в батч (лимит max_items_per_batch) — пойдут в следующий", left)
    record_cycle(conn, "digest", time.monotonic() - started, {
        "scored_now": n_scored, "scoring_error": err, "sent": stats is not None, **(stats or {}),
        "context": context_stats,
        "left_for_next_batch": left, "tokens_today_before": tokens_before, "tokens_today": tokens_today(conn),
        "digest_input_tokens": used["i"], "digest_output_tokens": used["o"], "digest_cost_usd": round(used["c"], 4),
    })


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
    limit = cfg["budget"]["max_tokens_per_day"]
    week = conn.execute("SELECT COALESCE(SUM(cost_usd),0) c, COALESCE(SUM(input_tokens+output_tokens),0) t "
                        "FROM usage WHERE day>=?", ((datetime.now().date() - timedelta(days=7)).isoformat(),)).fetchone()
    print(f"\nтокены сегодня: {tokens_today(conn)} из {limit};  за 7 дней: {week['t']} токенов ≈ ${week['c']:.2f}")
    print("\nпоследние прогоны (трейс):")
    for r in conn.execute("SELECT * FROM cycles ORDER BY ts DESC LIMIT 4"):
        print(f"  {r['ts']}  {r['kind']:<8} {r['duration_sec']:>6.0f} с  {r['data'][:400]}")
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
    print(f"3-59/5 * * * * cd {ROOT} && {py} run.py tg >> data/logs/cron.log 2>&1")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # эмодзи/кириллица в консоли Windows
        if stream:  # под pythonw потоков нет
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="GME-монитор")
    ap.add_argument("command", choices=["collect", "digest", "tg", "status", "print-cron", "journal", "patterns",
                                        "fundamentals", "impact"])
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
    if args.command == "impact":
        print(impact.report(connect_db(cfg), cfg))
        return 0
    if args.command == "fundamentals":
        conn = connect_db(cfg)
        if args.force:
            print(fundamentals.refresh(conn, Http(cfg, conn), cfg, force=True))
        print(fundamentals.report(conn))
        return 0
    if args.command == "tg":  # лёгкая задача раз в несколько минут, без общего лока
        try:
            n = tg_commands.run(connect_db(cfg), cfg)
            if n:
                log.info("TG-команды: обработано %d", n)
            return 0
        except Exception:
            log.exception("TG-команды: ошибка")
            return 1
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
