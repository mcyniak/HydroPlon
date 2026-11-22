"""HydroPlon: wyjasnialna diagnoza dzialki, niezalezna od uprawy.

Wejscie: zapisany AnalysisSnapshot z etapu 2 (modules: soil, terrain,
hydrology, weather, satellite). Wyjscie: deterministyczny JSON do etapow 3-4.
Nie jest to urzedowe stwierdzenie suszy, pozwolenie ani ekspertyza.

Pola i wartosci zgodne z przykladowymi kontraktami etap 2 v1. Wyniki
sa orientacyjne, a progi dziedzinowe sa odpowiedzialnoscia serwisow upstream.

v1.1.0 (prototyp HydroPlon, rejestr R06): rozszerzenie WSTECZNIE ZGODNE.
Kazdy problem otrzymuje dodatkowo: confidence, reasons, supporting_indicators,
source_refs, missing_inputs, limitations (wymagane przez sec. 19.6 planu i specyfikację).
Dodano data_gaps oraz ograniczenie "flood_hazard_unknown", gdy warstwa
powodziowa jest niedostepna (blad zrodla => unknown, nie false; K17).
Istniejace pola i reguly emisji problemow nie zostaly zmienione.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping

DIAGNOSIS_VERSION = "1.1.0"
_MODULE_NAMES = ("soil", "terrain", "hydrology", "weather", "satellite")
_LEVELS = {"niska": "low", "niskie": "low", "low": "low",
           "umiarkowana": "moderate", "umiarkowane": "moderate", "srednia": "moderate",
           "średnia": "moderate", "moderate": "moderate",
           "wysoka": "high", "wysokie": "high", "high": "high"}


def _record(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _status(record: Mapping[str, Any]) -> str:
    return str(record.get("status", "no_data"))


def _level(value: Any) -> str | None:
    return _LEVELS.get(str(value).strip().lower()) if value is not None else None


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _limitation(module: Mapping[str, Any]) -> list[str]:
    v = _record(module.get("metadata")).get("limitations")
    return [str(x) for x in v] if isinstance(v, list) else []


def _evidence(source: str, module: Mapping[str, Any]) -> dict[str, Any]:
    metadata = _record(module.get("metadata"))
    return {
        "module": source,
        "evidence_status": "estimated",
        "source_confidence": metadata.get("confidence", "unknown"),
        "source_label": metadata.get("source") or metadata.get("sources") or source,
        "reference_only": source in ("soil", "terrain", "hydrology"),
        "limitations": _limitation(module),
    }


_CONF = {"wysoka": "high", "high": "high", "srednia": "medium", "średnia": "medium", "medium": "medium",
         "orientacyjna": "low", "low": "low", "niska": "low"}
_DOWNGRADE = {"high": "medium", "medium": "low", "low": "low", "unknown": "unknown"}
_GAPS = {
    "soil": ["low_soil_retention", "low_organic_matter"],
    "terrain": ["surface_runoff_erosion"],
    "weather": ["periodic_water_deficit"],
    "hydrology": ["drainage_context", "flood_hazard", "water_features_nearby"],
    "satellite": ["vegetation_moisture_signal"],
}
_PROBLEM_META = {
    "low_soil_retention": {
        "missing_inputs": ["field_soil_samples", "deeper_profile_water_capacity"],
        "required_verifications": ["soil_sampling_before_costly_works"]},
    "low_organic_matter": {
        "missing_inputs": ["laboratory_soil_organic_matter"],
        "required_verifications": ["soil_sampling_before_costly_works"]},
    "surface_runoff_erosion": {
        "missing_inputs": ["field_observation_of_flow_paths"],
        "required_verifications": ["field_inspection_runoff_paths"]},
    "periodic_water_deficit": {
        "missing_inputs": ["on_farm_precipitation_record", "actual_soil_moisture"],
        "required_verifications": []},
}


def _indicators(pid: str, module: Mapping[str, Any]) -> dict[str, Any]:
    if pid == "low_soil_retention":
        wc = _record(module.get("water_holding_capacity_0_30cm"))
        return {"retention_class": module.get("retention_class"), "awc_0_30cm_mm": wc.get("awc_mm"),
                "awc_uncertainty_range_mm": wc.get("awc_uncertainty_range_mm"),
                "sand_pct": _record(module.get("granulometry")).get("sand_pct")}
    if pid == "low_organic_matter":
        om = _record(module.get("organic_matter"))
        return {"organic_matter_pct": om.get("organic_matter_pct"),
                "heuristic_threshold_pct": om.get("low_humus_threshold_pct")}
    if pid == "surface_runoff_erosion":
        sl = _record(module.get("slope"))
        return {"runoff_erosion_risk": module.get("runoff_erosion_risk"), "slope_mean_deg": sl.get("mean_deg"),
                "area_over_3deg_pct": sl.get("area_over_3deg_pct"), "area_over_6deg_pct": sl.get("area_over_6deg_pct")}
    if pid == "periodic_water_deficit":
        ds = _record(module.get("decade_summary_up_to_target"))
        md = _record(module.get("metadata"))
        st = _record(_record(module.get("stations_used")).get("precipitation_station"))
        return {"climate_deficit_risk": module.get("climate_deficit_risk"),
                "mean_veg_season_p_minus_et0_mm": ds.get("mean_veg_season_kbw_mm"),
                "max_consecutive_dry_days": ds.get("max_consecutive_dry_days"),
                "data_completeness_pct": md.get("data_completeness_pct"),
                "precip_station_distance_km": st.get("distance_km")}
    return {}


def _source_label(module: Mapping[str, Any], name: str) -> str:
    md = _record(module.get("metadata"))
    src = md.get("source") or md.get("sources") or name
    if isinstance(src, list):
        src = "; ".join(str(x) for x in src) or name
    return f"{name}: {src}"


def diagnose_parcel(analysis_snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Zamienia surowe kategorie etapu 2 na problemy i ograniczenia.

    Nie stosuje nowych progow numerycznych. Zwraca puste problemy przy
    braku dowodow, nie przekonwertuje unknown na false i nie imputuje wartosci.
    """
    snapshot = _record(analysis_snapshot)
    modules = _record(snapshot.get("modules"))
    as_of = _date(snapshot.get("as_of_date") or snapshot.get("target_date"))
    if as_of is None:
        raise ValueError("AnalysisSnapshot musi zawierac poprawne as_of_date lub target_date")

    warnings: list[str] = []
    missing: list[str] = []
    usable: dict[str, dict[str, Any]] = {}
    for name in _MODULE_NAMES:
        rec = _record(modules.get(name))
        if _status(rec) not in ("ok", "partial"):
            missing.append(name)
            continue
        # Nie wykorzystuj wynikow opatrzonych data z przyszlosci.
        source_target = _date(rec.get("target_date") or rec.get("as_of_date"))
        if name in ("weather", "satellite") and source_target and source_target > as_of:
            missing.append(name)
            warnings.append(f"{name}: data wyniku pozniejsza od as_of_date; dane pominieto")
            continue
        if name == "satellite":
            obs_dt = _date(_record(rec.get("latest_valid_observation")).get("acquisition_date"))
            if obs_dt and obs_dt > as_of:
                missing.append(name)
                warnings.append("satellite: obserwacja pozniejsza od as_of_date; dane pominieto")
                continue
        usable[name] = rec

    problems: list[dict[str, Any]] = []
    constraints: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []

    def add_problem(pid: str, severity: str, module_name: str, reason: str,
                    code: str) -> None:
        module = usable[module_name]
        md = _record(module.get("metadata"))
        conf = _CONF.get(str(md.get("confidence", "")).strip().lower(), "unknown")
        if _status(module) == "partial":
            conf = _DOWNGRADE[conf]
        meta = _PROBLEM_META.get(pid, {})
        problems.append({
            "problem_id": pid,
            "severity": severity,
            "confidence": conf,
            "evidence_status": "estimated",
            "reason_codes": [code],
            "reasons_pl": [reason],
            "reasons": [reason],
            "supporting_indicators": _indicators(pid, module),
            "source_refs": [_source_label(module, module_name)],
            "missing_inputs": list(meta.get("missing_inputs", [])),
            "evidence": [_evidence(module_name, module)],
            "required_verifications": list(meta.get("required_verifications", [])),
            "limitations": _limitation(module),
        })

    soil = usable.get("soil")
    if soil:
        retention = _level(soil.get("retention_class"))
        if retention == "low":
            add_problem("low_soil_retention", "high", "soil",
                        "Model glebowy wskazuje niska zdolnosc magazynowania wody w warstwie 0-30 cm.",
                        "LOW_SOIL_RETENTION_MODEL")
        om = _record(soil.get("organic_matter"))
        if om.get("low_humus_flag") is True:
            add_problem("low_organic_matter", "moderate", "soil",
                        "Model glebowy sygnalizuje ograniczoną zawartość materii organicznej; próg flagi wymaga walidacji agronomicznej.",
                        "LOW_OM_HEURISTIC")

    terrain = usable.get("terrain")
    if terrain:
        risk = _level(terrain.get("runoff_erosion_risk"))
        if risk in ("moderate", "high"):
            add_problem("surface_runoff_erosion", risk, "terrain",
                        "Model terenu wskazuje podwyższone ryzyko spływu lub erozji; wymaga weryfikacji terenowej.",
                        "TERRAIN_RUNOFF_RISK")

    weather = usable.get("weather")
    if weather:
        risk = _level(weather.get("climate_deficit_risk"))
        if risk in ("moderate", "high"):
            add_problem("periodic_water_deficit", risk, "weather",
                        "Własny wskaźnik klimatyczny wskazuje ryzyko okresowych niedoborów; nie jest to oficjalna diagnoza IUNG.",
                        "WEATHER_CLIMATIC_PRESSURE")
        if _record(weather.get("metadata")).get("data_completeness_pct") is None:
            warnings.append("weather: brak jawnej kompletności danych pomiarowych")

    hydro = usable.get("hydrology")
    if hydro:
        flags = _record(hydro.get("farmer_confirmation_flags"))
        drain = flags.get("subsurface_drainage_status")
        if drain in (None, "unknown_requires_confirmation", "unknown"):
            constraints.append({
                "constraint_id": "subsurface_drainage_unverified",
                "status": "unknown",
                "required_verification": "verify_existing_drainage",
                "reason_pl": "Mapa rowów nie potwierdza istnienia ani sprawności drenażu podziemnego.",
                "source_refs": ["hydrology"],
            })
        permit = flags.get("water_abstraction_permit_status")
        if permit in (None, "unknown_requires_confirmation", "unknown"):
            constraints.append({
                "constraint_id": "water_abstraction_right_unverified",
                "status": "unknown",
                "required_verification": "verify_water_abstraction_rights",
                "reason_pl": "Bliskość cieku lub rowu nie dowodzi prawa do poboru wody.",
                "source_refs": ["hydrology"],
            })
        flood = _record(hydro.get("flood_and_basin_context"))
        if flood.get("in_flood_hazard_zone") is None and "in_flood_hazard_zone" in flood:
            constraints.append({
                "constraint_id": "flood_hazard_unknown",
                "status": "unknown",
                "required_verification": "verify_flood_hazard_maps",
                "reason_pl": "Warstwa zagrożenia powodziowego była niedostępna; brak informacji nie oznacza braku zagrożenia.",
                "source_refs": ["hydrology"],
            })
        if flood.get("in_flood_hazard_zone") is True:
            constraints.append({
                "constraint_id": "potential_flood_hazard_intersection",
                "status": "requires_verification",
                "required_verification": "verify_full_polygon_flood_intersection",
                "reason_pl": "Wskazano zagrożenie powodziowe. Przed inwestycją trzeba zbadać przecięcie strefy z całą geometrią działki.",
                "source_refs": ["hydrology"],
            })

    satellite = usable.get("satellite")
    if satellite:
        baseline = _record(satellite.get("baseline_comparison"))
        if baseline.get("canopy_moisture_drop_detected") is True:
            signals.append({
                "signal_id": "vegetation_moisture_anomaly",
                "evidence_status": "estimated",
                "source_refs": ["satellite"],
                "description_pl": "Satelita wskazuje anomalię wilgotności okrywy roślinnej; bez znajomości uprawy i fenologii nie potwierdza suszy.",
            })

    data_gaps: list[dict[str, Any]] = []
    for name in _MODULE_NAMES:
        if name in missing:
            data_gaps.append({"module": name, "status": _status(_record(modules.get(name))),
                              "cannot_determine": _GAPS[name],
                              "reason_pl": "Moduł niedostępny lub odrzucony; nie ustalono tych elementów diagnozy."})
    if soil and _level(soil.get("retention_class")) is None:
        data_gaps.append({"module": "soil", "status": _status(soil), "cannot_determine": ["low_soil_retention"],
                          "reason_pl": "Klasa retencji gleby nieustalona."})
    if terrain and _level(terrain.get("runoff_erosion_risk")) is None:
        data_gaps.append({"module": "terrain", "status": _status(terrain), "cannot_determine": ["surface_runoff_erosion"],
                          "reason_pl": "Ryzyko spływu nieustalone."})
    if weather and _level(weather.get("climate_deficit_risk")) is None:
        data_gaps.append({"module": "weather", "status": _status(weather), "cannot_determine": ["periodic_water_deficit"],
                          "reason_pl": "Ryzyko niedoborów nieustalone (brak kompletnych sezonów danych)."})

    if not problems and usable:
        warnings.append("Brak wykrytych problemów nie potwierdza braku zagrożenia suszą.")
    if missing:
        warnings.append("Nie wszystkie analizy etapu 2 byly dostepne; diagnoza czesciowa.")
    if any(k in usable for k in ("soil", "terrain", "hydrology")):
        warnings.append("Warstwy gleby, terenu i hydrologii są danymi referencyjnymi, nie pomiarami stanu na konkretny dzień analizy.")

    # Stabilna kolejnosc ulatwia porownywanie snapshotow i testy.
    problems.sort(key=lambda p: p["problem_id"])
    constraints.sort(key=lambda c: c["constraint_id"])
    evidence_summary = {n: _evidence(n, v) for n, v in usable.items()}
    status = "no_data" if not usable else ("partial" if missing or any(_status(v)=="partial" for v in usable.values()) else "ok")
    return {
        "status": status,
        "diagnosis_version": DIAGNOSIS_VERSION,
        "analysis_snapshot_id": snapshot.get("analysis_snapshot_id") or snapshot.get("analysis_id"),
        "parcel_id": snapshot.get("parcel_id") or snapshot.get("teryt_id"),
        "as_of_date": as_of.isoformat(),
        "problems": problems,
        "constraints": constraints,
        "supporting_signals": signals,
        "source_modules": evidence_summary,
        "missing_modules": missing,
        "data_gaps": data_gaps,
        "warnings": _unique(warnings),
    }


# Alias przydatny w orkiestratorze etapu 2.
get_parcel_diagnosis = diagnose_parcel
