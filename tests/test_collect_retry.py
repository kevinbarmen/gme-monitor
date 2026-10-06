"""Офлайн-тест повторов по источникам (без сети): независимость, бэкофф, пометка «недоступен».

  python tests/test_collect_retry.py
"""
from __future__ import annotations

import copy
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core  # noqa: E402
import run  # noqa: E402


def make_cfg(tmp: str) -> dict:
    cfg = copy.deepcopy(core.load_config())
    cfg["paths"] = {"db": f"{tmp}/t.db", "logs": f"{tmp}/logs", "beacon": f"{tmp}/gme.ts", "lock": f"{tmp}/run.lock"}
    cfg["health"]["retry_delays_sec"] = [30, 120, 600]
    return cfg


def test_rounds():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cfg = make_cfg(tmp)
        calls: dict[str, int] = {}
        sleeps: list[float] = []
        filter_runs: list[int] = []

        def source(name: str, fail_times: int = 0, permanent: bool = False):
            def task() -> str:
                calls[name] = calls.get(name, 0) + 1
                if permanent:
                    raise core.PermanentError("нет ключа")
                if calls[name] <= fail_times:
                    raise RuntimeError(f"{name}: timeout")
                return "ok"
            return name, task

        tasks = [source("always_ok"), source("flaky", fail_times=2), source("dead", fail_times=99),
                 source("misconfigured", permanent=True), source("second_ok")]

        run._source_tasks = lambda cfg, conn, http, ctx: tasks
        run._filter_and_alert = lambda conn, cfg, ctx: filter_runs.append(len(sleeps))
        run.alerts.check_source_health = lambda conn, cfg: None
        run.time.sleep = sleeps.append  # не ждём по-настоящему, но запоминаем паузы

        run.collect(cfg)

        conn = core.connect_db(cfg)
        health = {r["source"]: r for r in conn.execute("SELECT * FROM source_health")}

        # падение одних источников не отменило остальные
        assert calls["always_ok"] == 1 and calls["second_ok"] == 1
        # бэкофф ровно 30с → 2мин → 10мин
        assert sleeps == [30, 120, 600], sleeps
        # flaky поднялся с третьей попытки и считается здоровым
        assert calls["flaky"] == 3 and health["flaky"]["fail_since"] is None
        # dead: первая попытка + 3 повтора, затем помечен недоступным
        assert calls["dead"] == 4 and health["dead"]["fail_since"] is not None
        assert "timeout" in health["dead"]["last_error"]
        # ошибка настройки не повторяется
        assert calls["misconfigured"] == 1 and health["misconfigured"]["fail_since"] is not None
        # алерты по рабочим источникам ушли сразу (до первой паузы) и ещё раз после успешного повтора
        assert filter_runs == [0, 2], filter_runs
        # маячок записан: цикл завершён, хотя источник лежит
        assert Path(cfg["paths"]["beacon"]).exists()
        # недоступный источник виден в суточной сводке
        import digest
        notes = " ".join(digest.service_notes(conn))
        assert "dead" in notes and "misconfigured" in notes and "flaky" not in notes
        conn.close()


def test_all_ok_no_sleep():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cfg = make_cfg(tmp)
        sleeps: list[float] = []
        run._source_tasks = lambda cfg, conn, http, ctx: [("a", lambda: "ok"), ("b", lambda: "ok")]
        run._filter_and_alert = lambda conn, cfg, ctx: None
        run.alerts.check_source_health = lambda conn, cfg: None
        run.time.sleep = sleeps.append
        run.collect(cfg)
        assert sleeps == []
        conn = core.connect_db(cfg)
        assert conn.execute("SELECT COUNT(*) FROM source_health WHERE fail_since IS NOT NULL").fetchone()[0] == 0
        conn.close()


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if stream:
            stream.reconfigure(encoding="utf-8", errors="replace")
    os.environ["GME_TG_BOT_TOKEN"] = ""  # тест ничего не шлёт в Telegram
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"OK  {t.__name__}")
    print(f"\n{len(tests)} тестов пройдено")
