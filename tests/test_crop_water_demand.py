from datetime import date
import json
from pathlib import Path

import pytest

from app.engine.crop_water_demand import evaluate_crop_water_demand, get_daily_kc_fao56

DATA = Path(__file__).resolve().parents[1] / "backend" / "app" / "data" / "crops.json"
CATALOG = json.loads(DATA.read_text(encoding="utf-8"))
CROP = next(x for x in CATALOG["crops"] if x["crop_id"] == "maize_grain")


def sample_months():
    rows = []
    for year in (2016, 2017, 2018):
        for month in range(1, 13):
            from calendar import monthrange
            rows.append({"year": year, "month": month,
                         "days_included": monthrange(year, month)[1],
                         "precip_mm": 50.0, "eto_mm": 100.0})
    return rows


def sample_snapshot(rows=None):
    return {"analysis_id": "analysis-test", "teryt_id": "par-1", "target_date": "2019-06-15",
            "modules": {"weather": {"status": "ok", "target_date": "2019-06-15",
                                    "monthly_series": sample_months() if rows is None else rows},
                        "soil": {"status": "ok", "water_holding_capacity_0_30cm": {"awc_mm": 22.4}},
                        "satellite": {"status": "no_data"}}}


def test_full_history_produces_estimate_but_not_iung_official_drought():
    result = evaluate_crop_water_demand(sample_snapshot(), CROP)
    assert result["status"] == "ok"
    assert result["crop_water_balance"]["metric_type"] == "indicative_P_minus_ETc_not_IUNG_KBW"
    assert result["crop_water_risk_level"] == "unknown"
    assert result["crop_water_balance"]["indicative_season_p_minus_etc_mm"] is not None
    assert result["root_zone_retention"]["days_of_water_buffer_in_peak_summer"] is None
    assert result["root_zone_retention"]["awc_0_30cm_mm"] == 22.4


def test_missing_precip_is_unknown_not_zero():
    result = evaluate_crop_water_demand(sample_snapshot([]), CROP)
    assert result["status"] == "no_data"
    assert result["crop_water_balance"]["indicative_season_p_minus_etc_mm"] is None
    assert all(x["mean_precip_mm"] is None for x in result["monthly_crop_demand_chart"])


def test_history_filters_future_complete_and_current_partial_months():
    rows = sample_months()
    rows.append({"year": 2019,"month":6,"days_included":15,"precip_mm":0.0,"eto_mm":400.0})
    rows.append({"year": 2020,"month":6,"days_included":30,"precip_mm":10000.0,"eto_mm":0.0})
    result = evaluate_crop_water_demand(sample_snapshot(rows), CROP)
    june = next(x for x in result["monthly_crop_demand_chart"] if x["month"] == 6)
    assert june["reference_years_count"] == 3
    assert june["mean_eto_mm"] == 100.0


def test_kc_uses_real_dates_including_29_february():
    crop = dict(CROP)
    crop["phenology_calendar_pl"] = {
        "start_month": 2, "start_day": 1, "end_month": 3, "end_day": 10,
        "stage_lengths_days": {"initial": 10, "development": 10, "mid_season": 10, "late_season": 9}
    }
    k1, s1 = get_daily_kc_fao56(date(2024,2,29),crop)
    k2, s2 = get_daily_kc_fao56(date(2024,2,28),crop)
    assert k1 is not None and k2 is not None
    assert get_daily_kc_fao56(date(2024,1,31),crop) == (None,"off_season")


def test_unverified_profile_does_not_produce_numbers():
    incomplete = dict(CROP, parameter_status="pending_validation", kc_values=None)
    with pytest.raises(ValueError, match="Brak kompletnych parametrow"):
        evaluate_crop_water_demand(sample_snapshot(), incomplete)


def test_satellite_anomaly_not_called_confirmed_drought():
    s=sample_snapshot()
    s["modules"]["satellite"]={"status":"ok", "target_date":"2019-06-15",
                                "latest_valid_observation":{"acquisition_date":"2019-06-11"},
                                "baseline_comparison":{"canopy_moisture_drop_detected":True}}
    r=evaluate_crop_water_demand(s,CROP)
    text=r["satellite_phenology_interpretation"]["interpretation_pl"].lower()
    assert "nie potwierdza" in text


def test_target_date_season_only_uses_observations_before_cutoff():
    rows = sample_months()
    from calendar import monthrange
    rows.extend([
        {"year":2019, "month":4, "days_included":30, "precip_mm":20, "eto_mm":80},
        {"year":2019, "month":5, "days_included":31, "precip_mm":25, "eto_mm":95},
        {"year":2019, "month":6, "days_included":15, "precip_mm":10, "eto_mm":50},
        {"year":2019, "month":7, "days_included":31, "precip_mm":900, "eto_mm":1},
    ])
    r = evaluate_crop_water_demand(sample_snapshot(rows), CROP)
    assert r["crop_water_balance"]["target_season_has_complete_observation"] is True
    assert r["crop_water_balance"]["target_season_to_date_p_minus_etc_mm"] is not None
    assert r["crop_water_balance"]["observation_cutoff_date"] == "2019-06-15"
