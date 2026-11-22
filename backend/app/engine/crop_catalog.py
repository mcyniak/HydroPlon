"""Etap 3.1 — odczyt i walidacja katalogu ośmiu upraw (crops.json) oraz ocena uprawy.

Parametry pięciu upraw ``pending_validation`` NIE są uzupełniane. Ocena takiej
uprawy zwraca status ``unavailable`` (bez liczb), a nie błąd blokujący kreator —
część VIII wymaga, by wszystkie 8 upraw były wybieralne (rejestr R28).
"""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from app.config import DATA_DIR
from app.engine.crop_water_demand import ENGINE_VERSION, evaluate_crop_water_demand
from app.engine.ids import make_id
from app.models.analysis import AnalysisSnapshot
from app.models.crop import CropProfile, CropWaterAssessment
from app.services.errors import NotFoundError

CANONICAL_CROP_IDS = ("maize_grain", "winter_wheat", "winter_rapeseed", "potato", "sugar_beet",
                      "spring_barley", "winter_triticale_rye", "grassland")
LEGACY_ALIASES = {"maize": "maize_grain"}
ASSESSABLE_STATUSES = ("provisional", "validated")
# Kategoria katalogu => czy uprawa jest polowa/orna (fakt z katalogu, nie parametr agronomiczny).
NON_ARABLE_CATEGORIES = ("uzytek zielony",)


@lru_cache(maxsize=1)
def load_catalog() -> dict:
    data = json.loads((DATA_DIR / "crops.json").read_text(encoding="utf-8"))
    validate_catalog(data)
    return data


def validate_catalog(data: dict) -> None:
    ids = [c["crop_id"] for c in data["crops"]]
    if len(ids) != len(set(ids)):
        raise RuntimeError("Niejednoznaczne crop_id w katalogu upraw")
    if sorted(ids) != sorted(CANONICAL_CROP_IDS):
        raise RuntimeError(f"Katalog upraw musi zawierać dokładnie 8 kanonicznych identyfikatorów, ma: {ids}")
    for c in data["crops"]:
        if c["parameter_status"] in ASSESSABLE_STATUSES:
            p = c["depletion_fraction_p"]
            if not (0 < p < 1):
                raise RuntimeError(f"{c['crop_id']}: p musi być w (0,1)")
            if any(v < 0 for v in c["kc_values"].values()):
                raise RuntimeError(f"{c['crop_id']}: Kc nie może być ujemne")
        else:
            if c.get("kc_values") is not None or c.get("phenology_calendar_pl") is not None:
                raise RuntimeError(f"{c['crop_id']}: profil pending_validation nie może mieć wpisanych parametrów")


def resolve_crop_id(crop_id: str) -> tuple[str, list[str]]:
    warnings = []
    cid = (crop_id or "").strip()
    if cid in LEGACY_ALIASES:
        warnings.append(f"Identyfikator '{cid}' jest historyczny; użyto kanonicznego '{LEGACY_ALIASES[cid]}'.")
        cid = LEGACY_ALIASES[cid]
    if cid not in CANONICAL_CROP_IDS:
        raise NotFoundError(f"Nieznany identyfikator uprawy: {crop_id!r}. Nie przyjęto uprawy domyślnej.")
    return cid, warnings


def get_crop_spec(crop_id: str) -> dict:
    cid, _ = resolve_crop_id(crop_id)
    return next(c for c in load_catalog()["crops"] if c["crop_id"] == cid)


def crop_profiles() -> list[CropProfile]:
    return [CropProfile(**{k: v for k, v in c.items()}, available_for_assessment=c["parameter_status"] in ASSESSABLE_STATUSES)
            for c in load_catalog()["crops"]]


def is_arable(crop_spec: dict) -> bool:
    return crop_spec.get("category") not in NON_ARABLE_CATEGORIES


def assess_crop(snapshot: AnalysisSnapshot, crop_id: str) -> CropWaterAssessment:
    cid, warnings = resolve_crop_id(crop_id)
    spec = get_crop_spec(cid)
    catalog_version = load_catalog()["version"]
    key = {"analysis_id": snapshot.analysis_id, "crop_id": cid, "catalog_version": catalog_version,
           "engine_version": ENGINE_VERSION}
    assessment_id = make_id("ca", key)
    common = {"crop_assessment_id": assessment_id, "analysis_id": snapshot.analysis_id,
              "parcel_id": snapshot.parcel.parcel_id, "crop_id": cid, "catalog_version": catalog_version,
              "engine_version": ENGINE_VERSION}
    if spec["parameter_status"] not in ASSESSABLE_STATUSES:
        return CropWaterAssessment(
            **common, status="unavailable", status_raw="parameters_pending_validation", assessable=False,
            crop={"crop_id": cid, "name_pl": spec["name_pl"], "parameter_status": spec["parameter_status"],
                  "category": spec.get("category")},
            crop_water_balance=None,
            limitations=list(spec.get("limitations", [])) + [
                "Parametry agronomiczne tej uprawy (Kc, kalendarz, głębokość korzeni) nie zostały zweryfikowane — "
                "obliczenia ETc nie są wykonywane i nie przyjęto wartości innego gatunku."],
            warnings=warnings)
    result: dict[str, Any] = evaluate_crop_water_demand(snapshot.engine_payload(), spec)
    raw_status = result.pop("status")
    for k in ("analysis_id", "parcel_id", "as_of_date", "engine_version"):
        result.pop(k, None)
    return CropWaterAssessment(**common, status=raw_status, status_raw=raw_status, assessable=True,
                               warnings=warnings, as_of_date=snapshot.time_context.as_of_date.isoformat(),
                               **result)
