"""Eval скоринга по реальности: совпадает ли «оценка важности» с тем, как потом двигалась цена.

Раз в неделю считается ранговая корреляция Спирмена между оценкой и |движением цены| через
1 и 5 торговых дней — за неделю и накопительно. Корреляция около нуля или отрицательная
несколько недель подряд = скоринг не отличает важное от неважного → чинить промпт.

Единица наблюдения — ДЕНЬ, а не отдельная новость: берётся максимальная оценка среди материалов
дня. У всех новостей одного дня движение цены одно и то же, и считать их отдельными наблюдениями
значило бы завышать N.

На малой выборке корреляция шумная, поэтому рядом всегда показывается N, а при N меньше
impact_eval.min_n число не выводится вовсе.
"""
from __future__ import annotations

import bisect
import json
from datetime import date, datetime, timedelta

from core import iso, log


def _ranks(values: list[float]) -> list[float]:
    """Ранги со средним рангом для совпадающих значений."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Ранговая корреляция; None, если точек меньше трёх или один из рядов постоянен."""
    n = len(xs)
    if n < 3:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx == 0 or vy == 0:
        return None
    return cov / (vx * vy) ** 0.5


def daily_scores(conn) -> dict[str, int]:
    """Максимальная оценка материалов по дням (день — по дате публикации, UTC)."""
    rows = conn.execute(
        "SELECT substr(created_utc,1,10) d, MAX(score) s FROM items WHERE kind IN ('reddit','news') "
        "AND COALESCE(score_status,'ok')='ok' AND score>=1 AND COALESCE(injection,0)=0 GROUP BY d")
    return {r["d"]: r["s"] for r in rows}


def abs_move(dates: list[str], closes: list[float], day: str, horizon: int) -> float | None:
    """|изменение цены| от закрытия ДО дня события к закрытию через horizon торговых дней
    (день события или первый торговый день после него считается первым)."""
    i = bisect.bisect_left(dates, day)
    if i == 0 or i + horizon - 1 >= len(dates):
        return None
    return abs(closes[i + horizon - 1] / closes[i - 1] - 1) * 100


def pairs(conn, horizon: int, since: str | None = None, until: str | None = None) -> list[tuple[str, int, float]]:
    prices = conn.execute("SELECT date, close FROM prices ORDER BY date").fetchall()
    dates, closes = [p["date"] for p in prices], [p["close"] for p in prices]
    # выходные и праздники относим к ближайшему следующему торговому дню: у субботы, воскресенья и
    # понедельника движение цены одно и то же, это одно наблюдение с максимальной оценкой
    by_trading_day: dict[str, int] = {}
    for day, score in daily_scores(conn).items():
        if (since and day < since) or (until and day >= until):
            continue
        i = bisect.bisect_left(dates, day)
        if i < len(dates):
            by_trading_day[dates[i]] = max(score, by_trading_day.get(dates[i], 0))
    out = []
    for day, score in sorted(by_trading_day.items()):
        move = abs_move(dates, closes, day, horizon)
        if move is not None:
            out.append((day, score, move))
    return out


def evaluate(conn, cfg: dict, since: str | None = None, until: str | None = None) -> dict:
    """{'1d': {'n', 'rho'}, '5d': {...}} — rho None, если данных меньше min_n."""
    icfg = cfg["impact_eval"]
    result = {}
    for h in icfg["horizons"]:
        p = pairs(conn, h, since, until)
        rho = spearman([x[1] for x in p], [x[2] for x in p]) if len(p) >= icfg["min_n"] else None
        result[f"{h}d"] = {"n": len(p), "rho": None if rho is None else round(rho, 3)}
    return result


def _fmt(res: dict, cfg: dict) -> str:
    parts = []
    for h in cfg["impact_eval"]["horizons"]:
        r = res[f"{h}d"]
        parts.append(f"{h} дн.: " + (f"ρ={r['rho']:+.2f} (N={r['n']})" if r["rho"] is not None
                                     else f"недостаточно данных (N={r['n']})"))
    return ", ".join(parts)


def weekly(conn, cfg: dict, today: date | None = None) -> str | None:
    """В назначенный день недели считает eval, сохраняет строку в impact_eval и возвращает текст
    для дайджеста. В остальные дни — None."""
    icfg = cfg["impact_eval"]
    today = today or datetime.now().date()
    if not icfg.get("enabled") or today.weekday() != icfg["weekday"]:
        return None
    week_start = (today - timedelta(days=7)).isoformat()
    week = evaluate(conn, cfg, since=week_start, until=today.isoformat())
    total = evaluate(conn, cfg, until=today.isoformat())
    conn.execute("INSERT OR REPLACE INTO impact_eval(week_end,week,cumulative,created_at) VALUES(?,?,?,?)",
                 (today.isoformat(), json.dumps(week), json.dumps(total), iso()))
    conn.commit()

    line = (f"🎯 Оценка важности vs реальное движение цены (Спирмен, по дням). "
            f"Неделя — {_fmt(week, cfg)}. Всего — {_fmt(total, cfg)}.")
    # предупреждение: накопительная корреляция около нуля или отрицательная N недель подряд
    recent = conn.execute("SELECT cumulative FROM impact_eval ORDER BY week_end DESC LIMIT ?",
                          (icfg["warn_weeks"],)).fetchall()
    key = f"{icfg['horizons'][0]}d"
    rhos = [json.loads(r["cumulative"])[key]["rho"] for r in recent]
    if len(rhos) == icfg["warn_weeks"] and all(r is not None and r <= icfg["warn_rho"] for r in rhos):
        line += (f"\n⚠ Корреляция не выше {icfg['warn_rho']} уже {icfg['warn_weeks']} нед. подряд: оценка важности "
                 f"не связана с движением цены — стоит пересмотреть скоринг-промпт.")
        log.warning("impact eval: корреляция <= %s %d недель подряд", icfg["warn_rho"], icfg["warn_weeks"])
    log.info("impact eval: неделя %s, всего %s", week, total)
    return line


def report(conn, cfg: dict) -> str:
    """Таблица для `run.py impact`: по дням и итог."""
    icfg = cfg["impact_eval"]
    lines = ["Оценка важности (максимум за торговый день, выходные отнесены к понедельнику) vs |движение цены|", ""]
    by_h = {h: {d: (s, m) for d, s, m in pairs(conn, h)} for h in icfg["horizons"]}
    days = sorted(set().union(*by_h.values()))
    lines.append(f"{'торг. день':<12}{'оценка':>7}" + "".join(f"{'|Δ| ' + str(h) + ' дн.':>12}" for h in icfg["horizons"]))
    for day in days:
        score = next(by_h[h][day][0] for h in icfg["horizons"] if day in by_h[h])
        cells = "".join(f"{by_h[h][day][1]:>11.1f}%" if day in by_h[h] else f"{'—':>12}" for h in icfg["horizons"])
        lines.append(f"{day:<12}{score:>7}{cells}")
    lines += ["", "Накопительно: " + _fmt(evaluate(conn, cfg), cfg),
              f"(число не показывается при N < {icfg['min_n']}: на малой выборке корреляция — шум)"]
    return "\n".join(lines)
