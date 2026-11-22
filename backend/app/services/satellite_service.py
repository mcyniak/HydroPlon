"""Etap 2, usługa 5 — obserwacje satelitarne (Sentinel-2: NDVI/NDMI).

Zasady (Aneks B5, §19.5): tylko sceny ``acquisition_date <= as_of_date``; scena
przyjęta, gdy ≥ 70% czystych pikseli działki; brak sceny w oknie 45 dni => brak
danych (bez fikcyjnej wartości); anomalia NDMI to sygnał pomocniczy, nigdy
potwierdzony stres wodny. Flaga ``canopy_moisture_drop_detected`` jest ustalana
w analysis_service, bo wymaga salda P−ET0 z modułu pogody (R23).
"""
from __future__ import annotations

from datetime import date
from statistics import median
from typing import Any

from app.services.common import analysis_rules, base_metadata, unavailable

NO_CLEAR_MSG = "Brak bezchmurnych obserwacji satelitarnych Sentinel-2 w analizowanym okresie."


def _doy_distance(a: date, b: date) -> int:
    da, db = a.timetuple().tm_yday, b.timetuple().tm_yday
    d = abs(da - db)
    return min(d, 365 - d)


def compute_satellite(raw: dict, parcel: Any, as_of: date) -> dict:
    rules = analysis_rules()["satellite"]
    st, demo = raw.get("source_type", "unknown"), bool(raw.get("is_demonstration"))
    rec = raw.get("record") or {}
    common = {"as_of_date": as_of.isoformat(), "target_date": as_of.isoformat()}
    scenes = [s | {"_d": date.fromisoformat(s["acquisition_date"])} for s in rec.get("scenes") or []]
    scenes = [s for s in scenes if s["_d"] <= as_of]  # izolacja czasu
    clear = sorted([s for s in scenes if s["valid_parcel_pixels_pct"] >= rules["valid_pixels_pct_gte"]],
                   key=lambda s: s["_d"])
    series = [{"date": s["acquisition_date"], "ndvi_median": s["ndvi_median"], "ndmi_median": s["ndmi_median"],
               "valid_pixels_pct": s["valid_parcel_pixels_pct"]} for s in clear]
    recent = [s for s in clear if (as_of - s["_d"]).days <= rules["latest_window_days"]]
    if not recent:
        res = unavailable("satellite", reason_code="no_clear_observation_in_window", source_type=st,
                          is_demonstration=demo, source_version=raw.get("version"),
                          message_pl=NO_CLEAR_MSG + f" (okno {rules['latest_window_days']} dni przed {as_of.isoformat()}).",
                          latest_valid_observation=None, baseline_comparison=None, time_series=series,
                          rejected_cloudy_scenes_in_window=sum(
                              1 for s in scenes if (as_of - s["_d"]).days <= rules["latest_window_days"]
                              and s["valid_parcel_pixels_pct"] < rules["valid_pixels_pct_gte"]), **common)
        return res
    latest = recent[-1]
    ref = [s for s in clear if s["_d"].year < latest["_d"].year
           and _doy_distance(s["_d"], latest["_d"]) <= rules["baseline_window_days"]]
    baseline = None
    if ref:
        b_ndvi, b_ndmi = median(s["ndvi_median"] for s in ref), median(s["ndmi_median"] for s in ref)
        baseline = {"historical_window_days": rules["baseline_window_days"],
                    "reference_years_count": len({s["_d"].year for s in ref}),
                    "ndvi_baseline_median": round(b_ndvi, 3), "ndvi_anomaly": round(latest["ndvi_median"] - b_ndvi, 3),
                    "ndmi_baseline_median": round(b_ndmi, 3), "ndmi_anomaly": round(latest["ndmi_median"] - b_ndmi, 3),
                    "canopy_moisture_drop_detected": None}
    limitations = [
        "Niski wskaźnik NDVI/NDMI nie stanowi samodzielnego dowodu suszy — może wynikać ze żniw, zaorania pola, "
        "płodozmianu lub chorób roślin.",
        "Działka ewidencyjna może obejmować kilka różnych upraw lub zadrzewienia śródpolne, co uśrednia wartość wskaźnika.",
        "Tło suszowe EDO nie jest dostępne w tym trybie.",
    ]
    if latest["ndvi_iqr"] > rules["heterogeneity_ndvi_iqr_gt"]:
        limitations.append("Duże zróżnicowanie NDVI wewnątrz działki — prawdopodobnie różne sposoby użytkowania terenu.")
    if baseline is None:
        limitations.append("Brak obserwacji referencyjnych z wcześniejszych lat w tym samym oknie — anomalii nie wyznaczono.")
    return {
        "status": "ok" if baseline else "partial",
        "module": "satellite",
        **common,
        "latest_valid_observation": {
            "acquisition_date": latest["acquisition_date"],
            "days_before_target_date": (as_of - latest["_d"]).days,
            "scene_id": latest["scene_id"],
            "valid_parcel_pixels_pct": latest["valid_parcel_pixels_pct"],
            "ndvi_median": latest["ndvi_median"], "ndvi_iqr": latest["ndvi_iqr"],
            "ndmi_median": latest["ndmi_median"], "ndmi_iqr": latest["ndmi_iqr"],
        },
        "baseline_comparison": baseline,
        "edo_drought_background": None,
        "time_series": series,
        "metadata": base_metadata(
            sources=[raw.get("source_label", "Sentinel-2")], source_type=st, is_demonstration=demo,
            confidence="srednia", data_is_reference=False, source_version=raw.get("version"),
            limitations=limitations, observation_cutoff=as_of.isoformat(),
            last_observation_date=latest["acquisition_date"], spatial_resolution_m=10,
            thresholds_status=analysis_rules()["status"]),
    }


def apply_canopy_rule(sat: dict, weather: dict, as_of: date) -> dict:
    """Flaga spadku wilgotności okrywy (Aneks B5 §4.4). Brak pogody => null, nie false."""
    rules = analysis_rules()["satellite"]["canopy_drop"]
    base = sat.get("baseline_comparison")
    latest = sat.get("latest_valid_observation")
    if sat.get("status") not in ("ok", "partial") or not base or not latest:
        return sat
    obs = date.fromisoformat(latest["acquisition_date"])
    in_season = obs.month in rules["season_months"] and (as_of - obs).days <= rules["max_obs_age_days"]
    anomaly_ok = base["ndmi_anomaly"] <= rules["ndmi_anomaly_lte"]
    balance = None
    if weather.get("status") in ("ok", "partial"):
        snap = weather.get("target_date_season_snapshot") or {}
        balance = snap.get("kbw_to_date_mm")
    if not in_season or not anomaly_ok:
        flag = False
    elif balance is None:
        flag = None
    else:
        flag = balance < rules["requires_season_balance_mm_lt"]
    base["canopy_moisture_drop_detected"] = flag
    base["canopy_rule"] = {"in_season_recent": in_season, "ndmi_anomaly_below_threshold": anomaly_ok,
                           "season_balance_mm": balance, "rule_status": analysis_rules()["status"],
                           "interpretation_pl": "Sygnał pomocniczy; nie potwierdza stresu wodnego ani suszy."}
    return sat
