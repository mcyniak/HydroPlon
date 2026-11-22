"""Fakty trójwartościowe (TRUE/FALSE/UNKNOWN) dla filtra i rankingu metod.

Fakty są wyprowadzane wyłącznie z kanonicznych wyników (diagnoza, moduły etapu 2,
ocena uprawy, katalog upraw, FieldContext). Filtr nie przelicza surowych
wskaźników od zera (§36.3). Brak informacji = UNKNOWN, nigdy FALSE (I18).
"""
from __future__ import annotations

from typing import Any

from app.engine.crop_catalog import get_crop_spec, is_arable
from app.models.analysis import AnalysisSnapshot
from app.models.crop import CropWaterAssessment
from app.models.methods import FieldContext
from app.services.common import analysis_rules

TRUE, FALSE, UNKNOWN = "TRUE", "FALSE", "UNKNOWN"
PROBLEM_MODULE = {"low_soil_retention": "soil", "low_organic_matter": "soil", "surface_runoff_erosion": "terrain",
                  "periodic_water_deficit": "weather"}
# Problemy bez źródła danych w pięciu usługach etapu 2 — zawsze UNKNOWN (R22).
UNSOURCED_PROBLEMS = ("wind_exposure", "uncontrolled_drainage_outflow", "limited_water_storage",
                      "irrigation_water_management")


def _tri(value: Any) -> str:
    return TRUE if value is True else FALSE if value is False else UNKNOWN


def build_facts(snapshot: AnalysisSnapshot, assessment: CropWaterAssessment, fc: FieldContext) -> dict[str, Any]:
    diag = snapshot.diagnosis
    present = {p.problem_id: p for p in diag.problems}
    gaps = {item for g in diag.data_gaps for item in g.cannot_determine}
    mods = snapshot.modules.model_dump(mode="json")
    facts: dict[str, Any] = {}
    problem_details: dict[str, Any] = {}
    for pid, module in PROBLEM_MODULE.items():
        if pid in present:
            facts[f"problem:{pid}"] = TRUE
            problem_details[pid] = {"severity": present[pid].severity, "confidence": present[pid].confidence}
        elif mods[module]["status"] in ("ok", "partial") and pid not in gaps:
            facts[f"problem:{pid}"] = FALSE
        else:
            facts[f"problem:{pid}"] = UNKNOWN
    for pid in UNSOURCED_PROBLEMS:
        facts[f"problem:{pid}"] = UNKNOWN

    # Teren — nachylenie (próg = próg umiarkowanego ryzyka spływu z analysis_rules)
    terrain = mods["terrain"]
    slope = terrain.get("slope") or {}
    rr = analysis_rules()["terrain"]["runoff_risk"]
    if terrain["status"] in ("ok", "partial") and slope.get("area_over_3deg_pct") is not None:
        facts["terrain.slope_present"] = _tri(
            slope["area_over_3deg_pct"] >= rr["moderate_if_area_over_3deg_pct_gte"]
            or (slope.get("mean_deg") or 0) >= rr["moderate_if_slope_mean_deg_gte"])
    else:
        facts["terrain.slope_present"] = UNKNOWN
    micro = terrain.get("microrelief") or {}
    facts["terrain.depressions_flag"] = _tri(micro.get("has_potential_retention_basins"))

    # Hydrologia
    hyd = mods["hydrology"]
    nwf = hyd.get("nearby_water_features") or {}
    flood = hyd.get("flood_and_basin_context") or {}
    usable_h = hyd["status"] in ("ok", "partial")
    facts["hydrology.ditch_or_stream_adjacent"] = _tri(nwf.get("has_adjacent_ditch_or_stream")) if usable_h else UNKNOWN
    facts["hydrology.flood_zone_intersects"] = _tri(flood.get("in_flood_hazard_zone")) if usable_h else UNKNOWN

    # Uprawa (z katalogu — fakt kategorii, nie parametr agronomiczny)
    spec = get_crop_spec(assessment.crop_id)
    facts["crop.is_arable"] = _tri(is_arable(spec))
    facts["crop.assessed"] = _tri(assessment.assessable and assessment.status in ("ok", "partial"))
    pressure = assessment.indicative_climatic_pressure
    facts["crop.climatic_pressure"] = (TRUE if pressure == "possible_climatic_shortfall"
                                       else FALSE if pressure == "not_indicated_by_this_indicator" else UNKNOWN)
    facts["crop.water_sensitivity_class"] = spec.get("water_sensitivity_class")

    # Deklaracje rolnika (FieldContext) — nie są potwierdzeniem technicznym ani prawnym
    facts["field.drainage_present"] = {"yes": TRUE, "no": FALSE, "unknown": UNKNOWN}[fc.subsurface_drainage_status]
    ret = fc.existing_retention_status
    types = set(fc.existing_retention_types)
    # "Tak" bez zaznaczenia podtypu nie przesądza o rodzaju urządzenia => UNKNOWN.
    facts["field.existing_pond_declared"] = TRUE if "pond" in types else (FALSE if ret == "no" else UNKNOWN)
    facts["field.existing_weir_declared"] = TRUE if "weir" in types else (FALSE if ret == "no" else UNKNOWN)
    facts["field.drainage_technical_state_ok"] = UNKNOWN

    # Brak źródeł danych w prototypie — zawsze UNKNOWN (wymagają dokumentów/oceny)
    facts["water.abstraction_right_confirmed"] = UNKNOWN
    facts["water.source_capacity_confirmed"] = UNKNOWN
    facts["legal.water_works_permits"] = UNKNOWN
    facts["legal.land_available_for_planting"] = UNKNOWN
    facts["design.hydrotechnical_available"] = UNKNOWN
    facts["farm.technology_compatible"] = UNKNOWN
    facts["layout.same_strip_confirmed"] = UNKNOWN
    facts["_problem_details"] = problem_details
    facts["_field_context_declarations"] = fc.model_dump(mode="json")
    return facts
