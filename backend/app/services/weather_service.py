"""Etap 2, usługa 4 — pogoda i historia 10 lat.

Zasady (Aneks B4, §19.4, korekty K06/R16/R17/R24):
  * bezwzględna izolacja czasu: używane są wyłącznie obserwacje ``obs_date <= as_of_date``,
  * brak pomiaru = None, nigdy 0 mm; dzień z luką przerywa ciąg bezopadowy,
  * ET0 metodą Hargreavesa–Samaniego (FAO-56 eq. 52) z Ra (FAO-56 eq. 21),
  * miesiąc z luką ma ``precip_mm = null`` (silnik 3.2 pomija niepełne miesiące),
  * ``kbw_mm`` to uproszczone saldo P−ET0, NIE KBW IUNG/SMSR,
  * brak cichego uzupełniania z innego źródła (Open-Meteo) — niska kompletność => partial,
  * jawne ``last_observation_date``; data przyszła => brak obserwacji, status partial.
"""
from __future__ import annotations

import calendar
import math
from datetime import date, timedelta
from statistics import mean
from typing import Any

from app.services.common import analysis_rules, base_metadata, r, unavailable

ARCHIVE_START = date(2016, 1, 1)


def extraterrestrial_radiation(lat_deg: float, day: date) -> float:
    """Ra [MJ m-2 d-1], FAO-56 eq. 21–25."""
    doy = day.timetuple().tm_yday
    phi = math.radians(lat_deg)
    dr = 1 + 0.033 * math.cos(2 * math.pi / 365 * doy)
    delta = 0.409 * math.sin(2 * math.pi / 365 * doy - 1.39)
    ws = math.acos(max(-1.0, min(1.0, -math.tan(phi) * math.tan(delta))))
    return (24 * 60 / math.pi) * 0.0820 * dr * (ws * math.sin(phi) * math.sin(delta)
                                               + math.cos(phi) * math.cos(delta) * math.sin(ws))


def hargreaves_et0(tmin: float | None, tmax: float | None, tmean: float | None, lat_deg: float,
                   day: date) -> float | None:
    """ET0 [mm/d] = 0,0023 (Tmean+17,8) √(Tmax−Tmin) · 0,408·Ra. Brak temperatury => None."""
    if tmin is None or tmax is None or tmean is None:
        return None
    ra = extraterrestrial_radiation(lat_deg, day)
    return max(0.0, 0.0023 * (tmean + 17.8) * math.sqrt(max(tmax - tmin, 0.0)) * 0.408 * ra)


def merge_daily(precip_series, thermal_series, lat_deg: float, as_of: date) -> list[dict]:
    """Łączy serie stacji opadowej i termicznej; odrzuca obserwacje po as_of_date."""
    temps = {row["obs_date"]: row for row in (thermal_series or ()) if row["obs_date"] <= as_of}
    out = []
    for row in precip_series or ():
        d = row["obs_date"]
        if d > as_of:
            continue  # izolacja czasu
        t = temps.get(d, {})
        eto = hargreaves_et0(t.get("tmin_c"), t.get("tmax_c"), t.get("tmean_c"), lat_deg, d)
        p = row["precip_mm"]
        out.append({"obs_date": d, "precip_mm": p, "eto_mm": eto, "tmean_c": t.get("tmean_c"),
                    "is_dry": (p < analysis_rules()["weather"]["dry_day_threshold_mm_lt"]) if p is not None else None})
    return out


def max_dry_spell(days: list[dict]) -> int | None:
    """Najdłuższy ciąg dni z P < 1 mm. Dzień bez pomiaru przerywa ciąg (nie jest liczony jako suchy)."""
    if not days:
        return None
    best = cur = 0
    for d in days:
        if d["is_dry"] is True:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def _window(days_by_date: dict, start: date, end: date) -> dict:
    n_expected = (end - start).days + 1
    rows = [days_by_date.get(start + timedelta(i)) for i in range(n_expected)]
    valid = [x for x in rows if x and x["precip_mm"] is not None and x["eto_mm"] is not None]
    complete = len(valid) == n_expected
    p = sum(x["precip_mm"] for x in valid) if complete else None
    e = sum(x["eto_mm"] for x in valid) if complete else None
    return {"expected": n_expected, "valid": len(valid), "complete": complete,
            "precip_mm": p, "eto_mm": e, "balance_mm": (p - e) if complete else None,
            "rows": [x for x in rows if x]}


def compute_weather(raw: dict, parcel: Any, as_of: date, mode: str) -> dict:
    rules = analysis_rules()["weather"]
    st, demo = raw.get("source_type", "unknown"), bool(raw.get("is_demonstration"))
    pst, tst = raw.get("precip"), raw.get("thermal")
    common = {"as_of_date": as_of.isoformat(), "target_date": as_of.isoformat()}
    if not pst:
        return unavailable("weather", reason_code="no_station", source_type=st, is_demonstration=demo,
                           message_pl="Brak przypisanej stacji opadowej — dane meteorologiczne niedostępne.",
                           source_version=raw.get("version"), climate_deficit_risk=None, monthly_series=[], **common)
    lat = parcel.coordinates.point_on_surface_lat
    days = merge_daily(pst["series"], (tst or {}).get("series"), lat, as_of)
    days = [d for d in days if d["obs_date"] >= ARCHIVE_START]
    by_date = {d["obs_date"]: d for d in days}
    observed = [d["obs_date"] for d in days if d["precip_mm"] is not None]
    if not observed:
        return unavailable("weather", reason_code="no_observations_before_as_of", source_type=st,
                           is_demonstration=demo, source_version=raw.get("version"),
                           message_pl="Brak obserwacji meteorologicznych do daty analizy.",
                           climate_deficit_risk=None, monthly_series=[], **common)
    last_obs = max(observed)
    period_end = min(as_of, max(d["obs_date"] for d in days))
    expected_days = (period_end - ARCHIVE_START).days + 1
    valid_p = sum(1 for d in days if d["precip_mm"] is not None)
    valid_both = sum(1 for d in days if d["precip_mm"] is not None and d["eto_mm"] is not None)
    completeness = round(100.0 * valid_both / expected_days, 1) if expected_days > 0 else 0.0

    # --- serie miesięczne (tylko do as_of; bieżący miesiąc liczony z danych dobowych do dnia as_of)
    monthly = []
    y, m = ARCHIVE_START.year, ARCHIVE_START.month
    while (y, m) <= (as_of.year, as_of.month):
        first = date(y, m, 1)
        last = min(date(y, m, calendar.monthrange(y, m)[1]), as_of)
        w = _window(by_date, first, last)
        temps = [x["tmean_c"] for x in w["rows"] if x["tmean_c"] is not None]
        dry = [x for x in w["rows"] if x["is_dry"] is True]
        monthly.append({"year": y, "month": m, "days_in_window": w["expected"], "days_included": w["valid"],
                        "is_partial_month": last.day != calendar.monthrange(y, m)[1],
                        "precip_mm": r(w["precip_mm"]), "eto_mm": r(w["eto_mm"]), "kbw_mm": r(w["balance_mm"]),
                        "tmean_c": r(mean(temps)) if temps and w["complete"] else None,
                        "dry_days_count": len(dry) if w["complete"] else None})
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)

    # --- sezony wegetacyjne IV–IX zakończone przed as_of
    seasons = []
    for year in range(ARCHIVE_START.year, as_of.year + 1):
        s_start, s_end = date(year, 4, 1), date(year, 9, 30)
        if s_end > as_of:
            continue
        w = _window(by_date, s_start, s_end)
        seasons.append({"year": year, "complete": w["complete"], "veg_precip_mm": r(w["precip_mm"]),
                        "veg_eto_mm": r(w["eto_mm"]), "veg_kbw_mm": r(w["balance_mm"]),
                        "max_dry_spell_days": max_dry_spell(w["rows"]),
                        "valid_days_pct": round(100 * w["valid"] / w["expected"], 1)})
    complete_seasons = [s for s in seasons if s["complete"]]
    mean_balance = mean(s["veg_kbw_mm"] for s in complete_seasons) if complete_seasons else None
    share_severe = (sum(s["veg_kbw_mm"] < -150 for s in complete_seasons) / len(complete_seasons)
                    if complete_seasons else None)
    cdd_candidates = [s["max_dry_spell_days"] for s in seasons if s["max_dry_spell_days"] is not None]
    max_cdd = max(cdd_candidates) if cdd_candidates else None

    # --- migawka sezonu do as_of
    season_start = date(as_of.year, 4, 1) if as_of >= date(as_of.year, 4, 1) else date(as_of.year, 1, 1)
    season_end = min(as_of, date(as_of.year, 9, 30)) if season_start.month == 4 else as_of
    snap = _window(by_date, season_start, season_end)
    baseline_vals = []
    for year in range(ARCHIVE_START.year, as_of.year):
        try:
            end_prev = date(year, season_end.month, season_end.day)
        except ValueError:  # 29 lutego w roku nieprzestępnym
            end_prev = date(year, season_end.month, 28)
        wb = _window(by_date, date(year, season_start.month, 1), end_prev)
        if wb["complete"]:
            baseline_vals.append(wb["precip_mm"])
    anomaly = None
    if snap and snap["complete"] and baseline_vals and mean(baseline_vals) > 0:
        anomaly = round(100.0 * (snap["precip_mm"] - mean(baseline_vals)) / mean(baseline_vals), 1)
    trailing = None
    if last_obs >= as_of:
        trailing = 0
        for d in reversed(days):
            if d["is_dry"] is True:
                trailing += 1
            else:
                break

    # --- klasyfikacja ryzyka (heurystyka do walidacji)
    risk = None
    dr = rules["deficit_risk"]
    if mean_balance is not None and max_cdd is not None:
        if (mean_balance < dr["high_if_mean_veg_balance_mm_lt"] or max_cdd >= dr["high_if_max_dry_spell_days_gte"]
                or (share_severe or 0) >= dr["high_if_share_seasons_balance_lt_minus150_gte"]):
            risk = "wysokie"
        elif mean_balance < dr["moderate_if_mean_veg_balance_mm_lt"] or max_cdd >= dr["moderate_if_max_dry_spell_days_gte"]:
            risk = "umiarkowane"
        else:
            risk = "niskie"

    limitations = [
        f"Dane pochodzą ze stacji oddalonych o {pst['distance_km']} km (opady)"
        + (f" i {tst['distance_km']} km (temperatura)" if tst else "")
        + " — lokalne opady na działce mogą odbiegać od pomiaru stacyjnego.",
        f"Analiza ograniczona do obserwacji dostępnych do dnia {as_of.isoformat()}.",
        "Saldo P−ET0 jest uproszczonym wskaźnikiem modelu, nie Klimatycznym Bilansem Wodnym IUNG/SMSR.",
        "ET0 metodą Hargreavesa (tylko temperatura) — orientacyjna; Penman-Monteith wymaga pełnych danych synoptycznych.",
    ]
    warnings = []
    status = "ok"
    # IMGW publikuje archiwum miesięczne z opóźnieniem ~1–2 miesięcy. Brak ostatnich dni jest "częściowy" tylko wtedy,
    # gdy dotyczy okien użytych w analizie (bieżący sezon do season_end); np. w listopadzie sezon IV–IX jest pełny.
    if last_obs < season_end:
        status = "partial"
        msg = (f"Ostatnia dostępna obserwacja: {last_obs.isoformat()}; brak pomiarów do dnia {season_end.isoformat()}"
               + (" (data przyszła — obserwacje nie istnieją; część meteorologiczna nie jest prognozą)."
                  if mode == "future" else " — bieżący sezon niepełny (IMGW publikuje archiwum z opóźnieniem)."))
        warnings.append(msg)
        limitations.append(msg)
    elif last_obs < as_of:
        limitations.append(
            f"Ostatnia obserwacja w archiwum IMGW: {last_obs.isoformat()} (dane publikowane z opóźnieniem). Okna "
            f"analizy (sezony IV–IX, bieżący sezon do {season_end.isoformat()}) są pokryte w całości; liczba dni bez "
            f"opadu po {last_obs.isoformat()} jest nieznana.")
    if completeness < rules["low_completeness_pct_lt"]:
        status = "partial"
        msg = (f"Kompletność danych {completeness}% (< {rules['low_completeness_pct_lt']}%). Brakujących dni nie "
               "uzupełniono innym źródłem ani zerami; niepełne miesiące i sezony pominięto w obliczeniach.")
        warnings.append(msg)
        limitations.append(msg)
    if not complete_seasons:
        limitations.append("Brak pełnego sezonu wegetacyjnego z kompletnymi danymi — ryzyko niedoborów nieustalone.")
    far = pst["distance_km"] > rules["max_station_distance_km_for_confident"]
    if far:
        limitations.append("Stacja opadowa jest odległa o ponad "
                           f"{rules['max_station_distance_km_for_confident']} km — reprezentatywność ograniczona.")
    confidence = ("wysoka" if completeness >= 95 and not far
                  else "srednia" if completeness >= rules["low_completeness_pct_lt"] and not far else "orientacyjna")
    return {
        "status": status,
        "module": "weather",
        **common,
        "analysis_mode": {"historical": "historical_point_in_time", "current": "current_state",
                          "future": "future_scenario_no_observations"}[mode],
        "climate_deficit_risk": risk,
        "stations_used": {
            "precipitation_station": {k: pst[k] for k in ("station_id", "name", "distance_km")} | {"archive_end": pst["archive_end"]},
            "thermal_station": ({k: tst[k] for k in ("station_id", "name", "distance_km")} | {"archive_end": tst["archive_end"]})
            if tst else None,
            "assignment_method": raw.get("assignment_method"),
        },
        "decade_summary_up_to_target": {
            "period_start": ARCHIVE_START.isoformat(), "period_end": period_end.isoformat(),
            "mean_veg_season_precip_mm": r(mean(s["veg_precip_mm"] for s in complete_seasons)) if complete_seasons else None,
            "mean_veg_season_eto_mm": r(mean(s["veg_eto_mm"] for s in complete_seasons)) if complete_seasons else None,
            "mean_veg_season_kbw_mm": r(mean_balance),
            "max_consecutive_dry_days": max_cdd,
            "max_dry_spell_is_lower_bound": any(not s["complete"] for s in seasons),
            "severe_deficit_seasons_share": r(share_severe, 2),
            "completed_veg_seasons_count": len(seasons),
            "complete_data_veg_seasons_count": len(complete_seasons),
        },
        "target_date_season_snapshot": {
            "year": as_of.year,
            "season_window": f"{season_start.isoformat()} .. {season_end.isoformat()}",
            "window_kind": "vegetation_season" if season_start.month == 4 else "winter_accumulation",
            "precip_to_date_mm": r(snap["precip_mm"]) if snap else None,
            "eto_to_date_mm": r(snap["eto_mm"]) if snap else None,
            "kbw_to_date_mm": r(snap["balance_mm"]) if snap else None,
            "window_complete": bool(snap and snap["complete"]),
            "precip_anomaly_vs_baseline_pct": anomaly,
            "baseline_years_count": len(baseline_vals),
            "active_dry_spell_days": trailing,
        },
        "annual_series": seasons,
        "monthly_series": monthly,
        "metadata": base_metadata(
            sources=[raw.get("source_label", "IMGW")], source_type=st, is_demonstration=demo,
            confidence=confidence, data_is_reference=False, source_version=raw.get("version"),
            limitations=limitations, observation_cutoff=as_of.isoformat(), last_observation_date=last_obs.isoformat(),
            eto_method=rules["eto_method"], dry_day_threshold_mm=rules["dry_day_threshold_mm_lt"],
            data_completeness_pct=completeness, precip_days_valid=valid_p, thresholds_status=analysis_rules()["status"],
            warnings=warnings),
    }
