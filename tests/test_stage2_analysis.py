"""Etap 2 — pięć usług, AnalysisSnapshot, diagnoza; testy 4–7, 28, 29 specyfikacji."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.engine.orchestrator import Orchestrator
from app.models.analysis import AnalysisSnapshot
from app.services.errors import InputValidationError
from app.services.soil_service import convert, saxton_rawls_theta, water_capacity_mm
from app.services.terrain_service import slope_stats_from_dem
from app.services.weather_service import compute_weather, hargreaves_et0, max_dry_spell
from app.store import ResultStore
from conftest import HIST, PARCEL_A, PARCEL_B, PARCEL_C, TODAY


def problems(snap: AnalysisSnapshot) -> dict:
    return {p.problem_id: p for p in snap.diagnosis.problems}


# --- AnalysisSnapshot: kontrakt -----------------------------------------------------------------------
def test_snapshot_contract_and_provenance(orch):
    s = orch.analyze(PARCEL_A, HIST)
    assert s.analysis_id.startswith("an-") and s.data_mode == "fixture"
    assert set(s.module_statuses) == {"soil", "terrain", "hydrology", "weather", "satellite"}
    for name in s.module_statuses:
        md = getattr(s.modules, name).metadata
        assert md.source_type == "fixture" and md.is_demonstration is True
    assert s.time_context.mode == "historical" and s.time_context.knowledge_cutoff_date == TODAY
    assert {"analysis_rules", "diagnosis"} <= set(s.rules_versions)
    assert any("TRYB TESTOWY" in w for w in s.warnings)


# --- 4. Brak danych gleby ---------------------------------------------------------------------------
def test_missing_soil_is_unavailable_not_imputed(orch):
    s = orch.analyze(PARCEL_C, HIST)
    soil = s.modules.soil.model_dump()
    assert s.module_statuses["soil"] == "unavailable"
    assert soil["retention_class"] == "nieznana"
    assert "water_holding_capacity_0_30cm" not in soil or soil["water_holding_capacity_0_30cm"] is None
    assert "low_soil_retention" not in problems(s)
    gaps = {g.module: g.cannot_determine for g in s.diagnosis.data_gaps}
    assert "low_soil_retention" in gaps["soil"]


def test_soil_unit_conversion_and_saxton_rawls():
    assert convert("sand", 800, "g/kg") == pytest.approx(80.0)
    assert convert("soc", 90, "dg/kg") == pytest.approx(0.9)
    with pytest.raises(ValueError):
        convert("sand", 800, "%")
    t33, t1500 = saxton_rawls_theta(80, 6, 1.5)
    assert 0.09 < t33 < 0.13 and 0.03 < t1500 < 0.06  # gleba piaszczysta
    assert water_capacity_mm(15, 15, 2.5, 0)["awc_mm"] > water_capacity_mm(80, 6, 1.5, 0)["awc_mm"]


# --- 5. Brak danych meteorologicznych; brak pomiaru ≠ 0 mm ---------------------------------------------
def test_missing_weather_station_is_unavailable(orch):
    parcel = orch.parcel_by_id(PARCEL_A)
    w = compute_weather({"source_type": "fixture", "is_demonstration": True, "precip": None, "thermal": None},
                        parcel, HIST, "historical")
    assert w["status"] == "unavailable" and w["climate_deficit_risk"] is None and w["monthly_series"] == []


def test_missing_precip_is_not_zero_and_breaks_dry_spell():
    days = [{"is_dry": True}] * 5 + [{"is_dry": None}] + [{"is_dry": True}] * 5
    assert max_dry_spell(days) == 5  # luka przerywa ciąg; nie powstaje sztuczne 11 dni
    parcel = Orchestrator(mode="fixture", result_store=ResultStore(), today=TODAY).parcel_by_id(PARCEL_A)
    start = date(2019, 6, 1)
    series = tuple({"obs_date": start + timedelta(i), "precip_mm": None if i == 3 else 0.0} for i in range(15))
    temps = tuple({"obs_date": start + timedelta(i), "tmin_c": 10.0, "tmax_c": 22.0, "tmean_c": 16.0} for i in range(15))
    w = compute_weather({"source_type": "fixture", "is_demonstration": True, "version": "t",
                         "precip": {"station_id": "X", "name": "X", "distance_km": 1.0, "archive_end": "2019-06-15", "series": series},
                         "thermal": {"station_id": "Y", "name": "Y", "distance_km": 1.0, "archive_end": "2019-06-15", "series": temps}},
                        parcel, date(2019, 6, 15), "historical")
    june = next(m for m in w["monthly_series"] if (m["year"], m["month"]) == (2019, 6))
    assert june["precip_mm"] is None and june["days_included"] == 14  # nie 0 mm


def test_hargreaves_et0_reasonable_and_none_without_temperature():
    et0 = hargreaves_et0(12.0, 26.0, 19.0, 52.5, date(2019, 7, 1))
    assert 3.0 < et0 < 6.5
    assert hargreaves_et0(None, 26.0, 19.0, 52.5, date(2019, 7, 1)) is None


def test_low_completeness_weather_is_partial_without_fallback(orch):
    s = orch.analyze(PARCEL_C, HIST)
    w = s.modules.weather.model_dump()
    assert s.module_statuses["weather"] == "partial"
    assert w["metadata"]["data_completeness_pct"] < 85
    assert w["climate_deficit_risk"] is None
    assert all("open-meteo" not in src.lower() for src in w["metadata"]["sources"])  # brak cichego uzupełnienia


# --- 6. Brak bezchmurnych obserwacji satelitarnych ---------------------------------------------------
def test_no_clear_satellite_observation(orch):
    s = orch.analyze(PARCEL_B, HIST)  # sceny IV–VI 2019 zachmurzone
    sat = s.modules.satellite.model_dump()
    assert s.module_statuses["satellite"] == "unavailable"
    assert sat["latest_valid_observation"] is None
    assert "Brak bezchmurnych obserwacji" in sat["metadata"]["limitations"][0]
    assert s.status == "partial"  # analiza nie kończy się błędem


def test_ndmi_anomaly_is_only_supporting_signal(orch):
    s = orch.analyze(PARCEL_A, HIST)
    assert s.modules.satellite.model_dump()["baseline_comparison"]["canopy_moisture_drop_detected"] is True
    assert [x["signal_id"] for x in s.diagnosis.supporting_signals] == ["vegetation_moisture_anomaly"]
    assert not any("stress" in p or "drought" in p for p in problems(s))


# --- 7. Różnica diagnozy między działkami -------------------------------------------------------------
def test_diagnosis_differs_between_parcels(orch):
    a, b = orch.analyze(PARCEL_A, HIST), orch.analyze(PARCEL_B, HIST)
    pa, pb = problems(a), problems(b)
    assert {"low_soil_retention", "surface_runoff_erosion", "periodic_water_deficit"} <= set(pa)
    assert "low_soil_retention" not in pb and "surface_runoff_erosion" not in pb
    assert pa["periodic_water_deficit"].severity == "high" and pb["periodic_water_deficit"].severity == "moderate"
    for p in a.diagnosis.problems:  # pola wymagane przez specyfikację §10
        assert p.confidence and p.reasons and p.source_refs and p.supporting_indicators is not None
        assert isinstance(p.missing_inputs, list) and isinstance(p.limitations, list)


def test_flood_zone_checked_on_whole_polygon_not_centroid(orch):
    s = orch.analyze(PARCEL_B, HIST)
    flood = s.modules.hydrology.model_dump()["flood_and_basin_context"]
    assert flood["in_flood_hazard_zone"] is True and flood["centroid_in_flood_zone"] is False
    assert "potential_flood_hazard_intersection" in {c.constraint_id for c in s.diagnosis.constraints}


def test_unavailable_flood_layer_is_unknown_not_false(orch):
    s = orch.analyze(PARCEL_C, HIST)
    assert s.modules.hydrology.model_dump()["flood_and_basin_context"]["in_flood_hazard_zone"] is None
    assert "flood_hazard_unknown" in {c.constraint_id for c in s.diagnosis.constraints}


def test_ditch_does_not_imply_drainage_or_water_rights(orch):
    s = orch.analyze(PARCEL_A, HIST)
    h = s.modules.hydrology.model_dump()
    assert h["nearby_water_features"]["has_adjacent_ditch_or_stream"] is True
    assert h["farmer_confirmation_flags"]["subsurface_drainage_status"] == "unknown_requires_confirmation"
    cons = {c.constraint_id: c.status for c in s.diagnosis.constraints}
    assert cons["subsurface_drainage_unverified"] == "unknown" and cons["water_abstraction_right_unverified"] == "unknown"


def test_terrain_slope_from_dem_mechanism():
    flat = [[100.0] * 5 for _ in range(5)]
    slope = [[100.0 + 0.1 * j for j in range(5)] for _ in range(5)]  # 10% spadku przy 1 m
    mask = [[True] * 5 for _ in range(5)]
    assert slope_stats_from_dem(flat, mask, 1.0)["slope_mean_deg"] == 0.0
    s = slope_stats_from_dem(slope, mask, 1.0)
    assert 5.6 < s["slope_mean_deg"] < 5.8 and s["area_over_3deg_pct"] == 100.0


# --- 28. Test historyczny: brak wycieku danych z przyszłości ------------------------------------------
def test_historical_date_isolation(orch):
    s = orch.analyze(PARCEL_A, HIST)
    w = s.modules.weather.model_dump()
    assert w["metadata"]["last_observation_date"] <= "2019-06-15"
    assert all((m["year"], m["month"]) <= (2019, 6) for m in w["monthly_series"])
    june = next(m for m in w["monthly_series"] if (m["year"], m["month"]) == (2019, 6))
    assert june["days_in_window"] == 15 and june["is_partial_month"] is True
    sat = s.modules.satellite.model_dump()
    assert sat["latest_valid_observation"]["acquisition_date"] <= "2019-06-15"
    assert all(t["date"] <= "2019-06-15" for t in sat["time_series"])
    assert s.modules.soil.metadata.data_is_reference is True
    assert any("referencyjn" in lim for lim in s.modules.soil.metadata.limitations)
    assert s.time_context.knowledge_cutoff_date != s.time_context.as_of_date  # osobna granica wiedzy


def test_strict_backtest_sets_knowledge_cutoff_to_as_of(orch):
    s = orch.analyze(PARCEL_A, HIST, strict_backtest=True)
    assert s.time_context.knowledge_cutoff_date == HIST


def test_current_date_reports_last_observation(orch):
    s = orch.analyze(PARCEL_A, TODAY)
    w = s.modules.weather.model_dump()
    assert s.time_context.mode == "current"
    # 21.11: okna analizy (sezon IV–IX) kończą się 30.09 i są pokryte — opóźnienie publikacji IMGW nie robi z tego
    # wyniku częściowego, ale data ostatniej obserwacji jest jawnie podana, a bieżąca seria suchych dni nieznana.
    assert s.module_statuses["weather"] == "ok"
    assert w["metadata"]["last_observation_date"] == "2026-10-31"
    assert any("2026-10-31" in x for x in w["metadata"]["limitations"])
    assert w["target_date_season_snapshot"]["active_dry_spell_days"] is None


# --- 29. Data przyszła ---------------------------------------------------------------------------------
def test_future_date_has_no_fake_observations(orch):
    fut = date(2027, 6, 15)
    s = orch.analyze(PARCEL_A, fut)
    w = s.modules.weather.model_dump()
    assert s.time_context.mode == "future"
    assert w["analysis_mode"] == "future_scenario_no_observations"
    assert w["metadata"]["last_observation_date"] <= TODAY.isoformat()
    assert w["target_date_season_snapshot"]["precip_to_date_mm"] is None
    assert s.module_statuses["satellite"] == "unavailable"


def test_date_before_archive_rejected(orch):
    with pytest.raises(InputValidationError):
        orch.analyze(PARCEL_A, date(2015, 12, 31))


def test_live_mode_source_failures_are_unavailable_without_fixture_fallback():
    """Tryb live bez sieci: awaria każdego źródła => moduł unavailable z powodem; nigdy dane fixture."""
    from app.services.adapters.fixture import FixtureAdapters
    from app.services.adapters.live import LiveAdapters
    from app.services.errors import SourceTimeoutError

    class OfflineLive(LiveAdapters):
        def parcel_by_id(self, teryt_id):
            return FixtureAdapters().parcel_by_id(teryt_id)  # tylko geometria testowa (bez sieci)

        def _fail(self, *a, **k):
            raise SourceTimeoutError("symulowany timeout źródła")
        soil_raw = terrain_raw = hydrology_raw = weather_raw = satellite_raw = _fail

    o = Orchestrator(adapters=OfflineLive(), result_store=ResultStore(), today=TODAY)
    s = o.analyze(PARCEL_A, HIST)
    assert s.data_mode == "live"
    for m in ("soil", "terrain", "hydrology", "weather", "satellite"):
        md = getattr(s.modules, m).metadata
        assert s.module_statuses[m] == "unavailable" and md.unavailable_reason == "source_timeout"
        assert md.is_demonstration is False and md.source_type == "live"
    assert s.diagnosis.problems == []
