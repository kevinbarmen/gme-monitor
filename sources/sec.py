"""SEC EDGAR: новые филинги GameStop через submissions API."""
from __future__ import annotations

import os
from datetime import datetime

from core import iso

# 8-K items → по-русски (для строки «что это значит»)
EIGHT_K_ITEMS = {
    "1.01": "заключён существенный договор",
    "1.02": "расторгнут существенный договор",
    "1.05": "инцидент кибербезопасности",
    "2.01": "завершено приобретение/продажа активов",
    "2.02": "финансовые результаты (отчётность)",
    "2.03": "новое долговое обязательство",
    "2.05": "расходы на реструктуризацию/выход из бизнеса",
    "2.06": "обесценение активов",
    "3.01": "проблемы с листингом",
    "3.02": "продажа акций без регистрации",
    "3.03": "изменение прав акционеров",
    "4.01": "смена аудитора",
    "4.02": "прежняя отчётность ненадёжна",
    "5.02": "изменения в руководстве/совете директоров",
    "5.03": "изменения устава",
    "5.07": "итоги голосования акционеров",
    "7.01": "раскрытие для рынка (Reg FD)",
    "8.01": "прочие события",
    "9.01": "финансовые документы/приложения",
}

FORM_MEANING = {
    "8-K": "существенное событие",
    "8-K/A": "поправка к 8-K",
    "10-Q": "квартальный отчёт",
    "10-K": "годовой отчёт",
    "10-K/A": "поправка к годовому отчёту",
    "4": "сделка инсайдера с акциями",
    "SC 13D": "крупный акционер (>5%) с активистскими намерениями",
    "SC 13D/A": "изменение доли крупного акционера-активиста",
    "SC 13G": "крупный пассивный акционер (>5%)",
    "SC 13G/A": "изменение доли крупного пассивного акционера",
}


def meaning(form: str, items: str = "") -> str:
    base = FORM_MEANING.get(form.replace("SCHEDULE ", "SC "), form)
    if form.startswith("8-K") and items:
        codes = [c.strip() for c in items.split(",") if c.strip() and c.strip() != "9.01"]
        described = [EIGHT_K_ITEMS.get(c, f"item {c}") for c in codes]
        if described:
            return f"{base}: " + "; ".join(described)
    return base


def fetch_filings(http, sec_cfg: dict) -> list[dict]:
    ua = os.getenv("SEC_USER_AGENT")
    if not ua:
        raise RuntimeError("SEC_USER_AGENT не задан в .env (SEC требует контакт в User-Agent)")
    cik = sec_cfg["cik"].zfill(10)
    data = http.get_json(f"https://data.sec.gov/submissions/CIK{cik}.json", headers={"User-Agent": ua})
    recent = data["filings"]["recent"]
    wanted = set(sec_cfg["forms"])
    out = []
    for i, form in enumerate(recent["form"]):
        if form not in wanted:
            continue
        acc = recent["accessionNumber"][i]
        doc = recent["primaryDocument"][i]
        items = recent.get("items", [""] * len(recent["form"]))[i] or ""
        url = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc}"
               if doc else f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/")
        accepted = recent.get("acceptanceDateTime", [""] * len(recent["form"]))[i]
        accepted = (iso(datetime.fromisoformat(accepted.replace("Z", "+00:00"))) if accepted
                    else recent["filingDate"][i] + "T00:00:00+00:00")
        out.append({
            "id": f"sec:{acc}",
            "kind": "sec",
            "source": "SEC EDGAR",
            "bias": "официальный документ",
            "title": f"{form} — {recent.get('primaryDocDescription', [''] * len(recent['form']))[i] or form}",
            "url": url,
            "text": "",
            "created_utc": accepted,
            "meta": {"form": form, "items": items, "filing_date": recent["filingDate"][i],
                     "meaning": meaning(form, items)},
        })
    return out
