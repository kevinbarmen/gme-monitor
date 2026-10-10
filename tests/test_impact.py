"""Офлайн-тесты еженедельного eval «оценка важности vs движение цены».

  python tests/test_impact.py
"""
from __future__ import annotations

import copy
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core  # noqa: E402
import impact  # noqa: E402


def test_spearman():
    assert abs(impact.spearman([1, 2, 3, 4, 5], [2, 4, 6, 8, 10]) - 1) < 1e-9
    assert abs(impact.spearman([1, 2, 3, 4, 5], [9, 7, 5, 3, 1]) + 1) < 1e-9
    assert abs(impact.spearman([1, 2, 2, 3], [1, 3, 2, 4]) - 0.9486832980505138) < 1e-9  # с совпадающими рангами
    assert impact.spearman([1, 1, 1], [1, 2, 3]) is None  # постоянный ряд
    assert impact.spearman([1, 2], [1, 2]) is None        # слишком мало точек


def test_abs_move():
    dates = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]
    closes = [100.0, 110.0, 99.0, 121.0]
    assert abs(impact.abs_move(dates, closes, "2026-01-06", 1) - 10) < 1e-9   # 100 → 110
    assert abs(impact.abs_move(dates, closes, "2026-01-06", 2) - 1) < 1e-9    # 100 → 99, модуль
    assert impact.abs_move(dates, closes, "2026-01-05", 1) is None            # нет закрытия «до»
    assert impact.abs_move(dates, closes, "2026-01-08", 2) is None            # будущее ещё не наступило


def _fill(conn, start: date, days: int, good: bool) -> None:
    """Торговые дни подряд: в «важные» дни цена двигается сильно (good) или наоборот."""
    price = 100.0
    conn.execute("INSERT OR REPLACE INTO prices(date,close) VALUES(?,?)", ((start - timedelta(days=1)).isoformat(), price))
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        score = 2 + (i % 7)  # 2..8
        move = (score if good else (10 - score)) / 100
        price *= 1 + move
        conn.execute("INSERT OR REPLACE INTO prices(date,close) VALUES(?,?)", (d, price))
        conn.execute("INSERT INTO items(id,kind,source,title,created_utc,first_seen,score,score_status) "
                     "VALUES(?,?,?,?,?,?,?,'ok')", (f"reddit:{d}", "reddit", "r/GME", "t", d + "T12:00:00+00:00",
                                                    core.iso(), score))
        # второй материал того же дня с меньшей оценкой не должен стать отдельным наблюдением
        conn.execute("INSERT INTO items(id,kind,source,title,created_utc,first_seen,score,score_status) "
                     "VALUES(?,?,?,?,?,?,1,'ok')", (f"news:{d}", "news", "n", "t", d + "T13:00:00+00:00", core.iso()))
    conn.commit()


def test_weekly_report_and_warning():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cfg = copy.deepcopy(core.load_config())
        cfg["paths"]["db"] = f"{tmp}/t.db"
        cfg["impact_eval"].update(weekday=0, min_n=10, warn_weeks=4, warn_rho=0.1, horizons=[1, 5])
        conn = core.connect_db(cfg)
        monday = date(2026, 3, 2)
        assert monday.weekday() == 0
        assert impact.weekly(conn, cfg, today=monday + timedelta(days=1)) is None  # не тот день недели

        _fill(conn, monday - timedelta(days=40), 40, good=True)
        line = impact.weekly(conn, cfg, today=monday)
        assert "Всего — 1 дн.: ρ=+1.00 (N=40)" in line, line
        assert "Неделя — 1 дн.: недостаточно данных (N=7)" in line   # за неделю дней мало — числа нет
        assert "⚠" not in line
        assert impact.evaluate(conn, cfg)["1d"]["n"] == 40  # дни, а не 80 материалов
        conn.close()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cfg = copy.deepcopy(core.load_config())
        cfg["paths"]["db"] = f"{tmp}/t.db"
        cfg["impact_eval"].update(weekday=0, min_n=10, warn_weeks=4, warn_rho=0.1, horizons=[1, 5])
        conn = core.connect_db(cfg)
        monday = date(2026, 3, 2)
        _fill(conn, monday - timedelta(days=60), 88, good=False)  # важные дни — самые тихие
        lines = [impact.weekly(conn, cfg, today=monday + timedelta(days=7 * w)) for w in range(4)]
        assert all("ρ=-1.00" in ln for ln in lines)
        assert all("⚠" not in ln for ln in lines[:3]) and "⚠" in lines[3]  # предупреждение с 4-й недели подряд
        assert conn.execute("SELECT COUNT(*) FROM impact_eval").fetchone()[0] == 4
        conn.close()


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if stream:
            stream.reconfigure(encoding="utf-8", errors="replace")
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"OK  {t.__name__}")
    print(f"\n{len(tests)} тестов пройдено")
