"""HydroPlon / etap 3.2: orientacyjna ocena klimatycznego zapotrzebowania uprawy.

FAO-56: ETc = Kc * ET0, bez wymyslania glebszego profilu gleby,
bez utozsamiania P-ETc z oficjalnym KBW IUNG i bez wyrokowania o suszy.
Input: AnalysisSnapshot etapu 2 + zwalidowany wpis katalogu upraw.

Tryb MVP: jesli dostepne sa tylko MIESIECZNE statystyki stacyjne, dane sa
usredniane i proporcjonalnie dzielone na dni sezonu (z jawna flaga).
Dla dokladniejszych wynikow nalezy przekazac serie dobowe.
"""
from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date, timedelta
from math import isfinite
from typing import Any, Mapping

ENGINE_VERSION = "2.0.0"
MONTH_LABELS = ["", "Sty", "Lut", "Mar", "Kwi", "Maj", "Cze",
                "Lip", "Sie", "Wrz", "Paź", "Lis", "Gru"]


def _as_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _calendar_date(year: int, month: int, day: int) -> date:
    # W przeciwienstwie do min(day, 28) nie przycinamy faktycznych dat.
    return date(year, month, day)


def _bounds_for_year(year: int, crop_spec: Mapping[str, Any]) -> tuple[date, date]:
    cal = crop_spec["phenology_calendar_pl"]
    start = _calendar_date(year, int(cal["start_month"]), int(cal["start_day"]))
    end = _calendar_date(year, int(cal["end_month"]), int(cal["end_day"]))
    if end < start:
        end = _calendar_date(year + 1, int(cal["end_month"]), int(cal["end_day"]))
    return start, end


def _season_containing_day(day: date, crop_spec: Mapping[str, Any]) -> tuple[date, date] | None:
    for year in (day.year - 1, day.year):
        start, end = _bounds_for_year(year, crop_spec)
        if start <= day <= end:
            return start, end
    return None


def get_daily_kc_fao56(day: date, crop_spec: Mapping[str, Any]) -> tuple[float | None, str]:
    """Cztery fazy FAO-56; poza sezonem zwracamy None, a nie arbitralne Kc."""
    season = _season_containing_day(day, crop_spec)
    if not season:
        return None, "off_season"
    start, end = season
    lengths = crop_spec["phenology_calendar_pl"]["stage_lengths_days"]
    kc = crop_spec["kc_values"]
    l_ini = int(lengths["initial"])
    l_dev = int(lengths["development"])
    l_mid = int(lengths["mid_season"])
    l_late = int(lengths["late_season"])
    if min(l_ini, l_dev, l_mid, l_late) < 1:
        raise ValueError("Dlugosci faz musza byc dodatnie")
    days_in_season = (end - start).days + 1
    if abs(sum((l_ini, l_dev, l_mid, l_late)) - days_in_season) > 2:
        raise ValueError("Niezgodnosc liczby dni faz FAO-56 z kalendarzem uprawy")
    offset = (day - start).days
    k_ini, k_mid, k_end = (float(kc[x]) for x in ("kc_ini", "kc_mid", "kc_end"))
    if offset < l_ini:
        return k_ini, "initial"
    if offset < l_ini + l_dev:
        frac = (offset - l_ini + 1) / l_dev
        return k_ini + frac * (k_mid - k_ini), "development"
    if offset < l_ini + l_dev + l_mid:
        return k_mid, "mid_season"
    frac = min((offset - l_ini - l_dev - l_mid + 1) / l_late, 1.0)
    return k_mid + frac * (k_end - k_mid), "late_season"


def _crop_days_and_kc(year: int, month: int, crop_spec: Mapping[str, Any],
                      last_day: int | None = None) -> tuple[int, float, int]:
    days = calendar.monthrange(year, month)[1]
    included = min(days, last_day or days)
    kcs = []
    for d in range(1, included + 1):
        kc, _ = get_daily_kc_fao56(date(year, month, d), crop_spec)
        if kc is not None:
            kcs.append(kc)
    return len(kcs), (sum(kcs) / included if included else 0.0), included


def _monthly_climatology(rows: list[dict[str, Any]], target: date) -> dict[int, dict[str, Any]]:
    """Odrzuca miesiace niepelne i jakiekolwiek obserwacje pozniejsze niz target."""
    grouped: dict[int, list[tuple[float, float, int]]] = defaultdict(list)
    for row in rows:
        try:
            year, month = int(row["year"]), int(row["month"])
            end = date(year, month, calendar.monthrange(year, month)[1])
            count = int(row.get("days_included", -1))
        except (KeyError, TypeError, ValueError):
            continue
        if end > target or count != calendar.monthrange(year, month)[1]:
            continue
        precip, eto = _number(row.get("precip_mm")), _number(row.get("eto_mm"))
        if precip is None or eto is None or precip < 0 or eto < 0:
            continue
        grouped[month].append((precip, eto, year))
    return {m: {
        "mean_precip_mm": sum(x[0] for x in data) / len(data),
        "mean_eto_mm": sum(x[1] for x in data) / len(data),
        "years_count": len(data),
        "years": sorted(x[2] for x in data),
    } for m, data in grouped.items()}


def evaluate_crop_water_demand(analysis_payload: Mapping[str, Any],
                               crop_spec: Mapping[str, Any]) -> dict[str, Any]:
    """Oblicza szacunkowe ETc i wskaznik P-ETc z jawnymi ograniczeniami.

    Ryzyko niedoboru w strefie korzeniowej pozostaje UNKNOWN bez bilansu
    aktualnego uwilgotnienia. Nie stosuje progow IUNG do P-ETc.
    """
    snapshot = _dict(analysis_payload)
    as_of = _as_date(snapshot.get("as_of_date") or snapshot["target_date"])
    modules = _dict(snapshot.get("modules"))
    weather, soil, sat = (_dict(modules.get(key)) for key in ("weather", "soil", "satellite"))
    crop_id = str(crop_spec["crop_id"])
    warnings: list[str] = []
    if crop_spec.get("parameter_status") not in ("provisional", "validated"):
        raise ValueError(f"Brak kompletnych parametrow uprawy {crop_id}; wymagaja walidacji")

    if weather.get("target_date") and _as_date(weather["target_date"]) > as_of:
        raise ValueError("Dane pogodowe wybiegaja poza as_of_date")

    all_rows = weather.get("monthly_series") if weather.get("status") in ("ok", "partial") else None
    rows = [dict(x) for x in all_rows if isinstance(x, Mapping)] if isinstance(all_rows, list) else []
    climatology = _monthly_climatology(rows, as_of)
    if not climatology:
        warnings.append("Brak kompletnych miesiecznych serii P i ET0: nie obliczamy bilansu uprawy.")
    else:
        warnings.append("Dane miesieczne: ETc i P w granicach sezonu oszacowano zakladajac rownomierny rozklad wewnatrz miesiaca.")

    # Uzywamy dat rzeczywistych, a nie sztucznego roku z min(day,28).
    # Dla miesiecznej klimatologii rok 2024 umozliwia poprawna obsluge 29 lutego.
    reference_year = 2024
    monthly_chart: list[dict[str, Any]] = []
    expected_months: list[int] = []
    for month in range(1, 13):
        cropped_days, kc_avg_all_days, days_in_month = _crop_days_and_kc(reference_year, month, crop_spec)
        if cropped_days == 0:
            continue
        expected_months.append(month)
        meteo = climatology.get(month)
        if meteo:
            # P i ET0 miesieczne rozdzielono proportionally; przy braku dziennych
            # danych jest to tylko wartosc orientacyjna, NIE bilans gleby.
            precip = meteo["mean_precip_mm"] * cropped_days / days_in_month
            etc = meteo["mean_eto_mm"] * kc_avg_all_days
            balance = precip - etc
        else:
            precip = etc = balance = None
        monthly_chart.append({
            "month": month, "month_label": MONTH_LABELS[month],
            "active_crop_days": cropped_days,
            "kc_avg_over_calendar_month": round(kc_avg_all_days, 4),
            "mean_precip_mm": round(precip, 2) if precip is not None else None,
            "mean_eto_mm": round(meteo["mean_eto_mm"], 2) if meteo else None,
            "mean_crop_etc_mm": round(etc, 2) if etc is not None else None,
            "indicative_p_minus_etc_mm": round(balance, 2) if balance is not None else None,
            "reference_years_count": meteo["years_count"] if meteo else 0,
            "is_critical_month": month in crop_spec.get("critical_months", []),
        })

    # Migawka bieżącego sezonu: używamy WYŁĄCZNIE miesięcy roku as_of_date
    # zakończonych przed datą analizy i ewentualnego częściowego bieżącego.
    # Jeśli brak któregokolwiek miesiąca od początku sezonu - wynik UNKNOWN.
    current_by_month: dict[int, dict[str, Any]] = {}
    for row in rows:
        try:
            year, month, count = int(row["year"]), int(row["month"]), int(row["days_included"])
            total = calendar.monthrange(year, month)[1]
        except (KeyError, ValueError, TypeError):
            continue
        if year != as_of.year or month > as_of.month:
            continue
        max_days = as_of.day if month == as_of.month else total
        if count != max_days or count < 1:
            continue
        precip, eto = _number(row.get("precip_mm")), _number(row.get("eto_mm"))
        if precip is None or eto is None or precip < 0 or eto < 0:
            continue
        crop_days, mean_kc, observed_days = _crop_days_and_kc(year, month, crop_spec, count)
        if crop_days == 0:
            continue
        current_by_month[month] = {
            "month": month,
            "approx_precip_mm": precip * crop_days / observed_days,
            "approx_etc_mm": eto * mean_kc,
            "count": count,
        }
    current_expected_months = []
    for month in range(1, as_of.month + 1):
        # Miesiące sezonu uprawy od początku roku do wskazanej daty.
        if _crop_days_and_kc(as_of.year, month, crop_spec,
                            as_of.day if month == as_of.month else None)[0] > 0:
            current_expected_months.append(month)
    current_complete = bool(current_expected_months) and all(
        m in current_by_month for m in current_expected_months)
    current_balance = round(sum(
        current_by_month[m]["approx_precip_mm"] - current_by_month[m]["approx_etc_mm"]
        for m in current_expected_months), 2) if current_complete else None
    if current_expected_months and not current_complete:
        warnings.append("Brak wszystkich miesiecy sezonu do daty analizy; bilans bieżącego sezonu pozostaje nieznany.")

    present = {int(r["month"]): r for r in monthly_chart
               if r["indicative_p_minus_etc_mm"] is not None}
    completed_full = bool(expected_months) and all(m in present for m in expected_months)
    critical = [m for m in crop_spec.get("critical_months", []) if m in expected_months]
    completed_critical = bool(critical) and all(m in present for m in critical)

    def sum_metrics(months: list[int], key: str) -> float | None:
        if not all(m in present for m in months):
            return None
        return round(sum(float(present[m][key]) for m in months), 2)

    season_balance = sum_metrics(expected_months, "indicative_p_minus_etc_mm") if completed_full else None
    critical_balance = sum_metrics(critical, "indicative_p_minus_etc_mm") if completed_critical else None
    pressure = ("possible_climatic_shortfall" if critical_balance is not None and critical_balance < 0
                else "not_indicated_by_this_indicator" if critical_balance is not None else "unknown")
    if season_balance is None:
        warnings.append("Brak kompletnego sezonu odniesienia; suma sezonowa pozostaje nieznana.")

    # AWC z 0-30 cm nie uprawnia do estymacji calej strefy korzeniowej.
    awc_30 = None
    if soil.get("status") in ("ok", "partial"):
        awc_30 = _number(_dict(soil.get("water_holding_capacity_0_30cm")).get("awc_mm"))
    warnings.append("SoilGrids 0-30 cm nie pozwala bez pomiaru oszacowac zasobu wody calej strefy korzeniowej ani dni jej wystarczalnosci.")

    sat_interp = {"status": "no_data", "interpretation_pl": "Brak wiarygodnej obserwacji satelitarnej do interpretacji uprawy."}
    latest = _dict(sat.get("latest_valid_observation"))
    if sat.get("status") in ("ok", "partial") and latest:
        obs_day = latest.get("acquisition_date")
        if obs_day and _as_date(obs_day) <= as_of:
            _, stage = get_daily_kc_fao56(as_of, crop_spec)
            anomaly = _dict(sat.get("baseline_comparison")).get("canopy_moisture_drop_detected") is True
            if stage == "off_season":
                desc = "Obserwacja poza przyjetym sezonem; spadek wskaznikow nie swiadczy o suszy uprawy."
            elif anomaly:
                desc = "Obserwacja sugeruje anomalie wilgotnosci okrywy; moze miec wiele przyczyn i nie potwierdza samodzielnie stresu wodnego."
            else:
                desc = "Obserwacje satelitarne sa wskazowka pomocnicza; nie stanowia pomiaru wilgotnosci gleby."
            sat_interp = {"status": "contextual_estimate", "observed_at": obs_day,
                          "expected_stage": stage, "interpretation_pl": desc}
        else:
            warnings.append("Satelita: odrzucono obserwacje niezgodna czasowo z as_of_date.")

    result_status = "ok" if completed_full and completed_critical else ("partial" if present else "no_data")
    return {
        "status": result_status,
        "engine_version": ENGINE_VERSION,
        "analysis_id": snapshot.get("analysis_id") or snapshot.get("analysis_snapshot_id"),
        "parcel_id": snapshot.get("parcel_id") or snapshot.get("teryt_id"),
        "as_of_date": as_of.isoformat(),
        "crop": {
            "crop_id": crop_id,
            "name_pl": crop_spec.get("name_pl"),
            "critical_stage_label_pl": crop_spec.get("critical_stage_label_pl"),
            "parameter_status": crop_spec.get("parameter_status"),
            "parameter_basis_pl": crop_spec.get("parameter_basis_pl"),
            "parameter_sources": crop_spec.get("parameter_sources", []),
        },
        "crop_water_balance": {
            "indicative_season_p_minus_etc_mm": season_balance,
            "indicative_critical_window_p_minus_etc_mm": critical_balance,
            "indicative_critical_shortfall_mm": (round(max(0.0, -critical_balance), 2)
                                                   if critical_balance is not None else None),
            "target_season_to_date_p_minus_etc_mm": current_balance,
            "target_season_has_complete_observation": current_complete,
            "observation_cutoff_date": as_of.isoformat(),
            "season_has_complete_baseline": completed_full,
            "critical_window_has_complete_baseline": completed_critical,
            "metric_type": "indicative_P_minus_ETc_not_IUNG_KBW",
            "data_scope": "full_month_historical_climatology_to_as_of_date",
        },
        "crop_water_risk_level": "unknown",  # wymaga zwalidowanego modelu agronomicznego
        "indicative_climatic_pressure": pressure,
        "root_zone_retention": {
            "awc_0_30cm_mm": awc_30,
            "estimated_root_zone_awc_mm": None,
            "readily_available_water_raw_mm": None,
            "days_of_water_buffer_in_peak_summer": None,
        },
        "satellite_phenology_interpretation": sat_interp,
        "monthly_crop_demand_chart": monthly_chart,
        "limitations": list(dict.fromkeys([*warnings, *crop_spec.get("limitations", [])])),
    }
