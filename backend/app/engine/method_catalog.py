"""Etap 4, moduł 1 — wersjonowany katalog 11 metod (bez oceny działki, kosztów i dotacji)."""
from __future__ import annotations

import json
from functools import lru_cache

from app.config import DATA_DIR
from app.models.methods import METHOD_IDS

PROBLEM_VOCABULARY = ("low_soil_retention", "periodic_water_deficit", "low_organic_matter", "surface_runoff_erosion",
                      "wind_exposure", "uncontrolled_drainage_outflow", "limited_water_storage",
                      "irrigation_water_management")
SEVERITIES = ("hard", "conditional", "advisory")


@lru_cache(maxsize=1)
def load_methods() -> dict:
    data = json.loads((DATA_DIR / "methods.json").read_text(encoding="utf-8"))
    validate_methods(data)
    return data


@lru_cache(maxsize=1)
def load_pair_rules() -> dict:
    data = json.loads((DATA_DIR / "method_pair_rules.json").read_text(encoding="utf-8"))
    ids = set(METHOD_IDS)
    for rule in data["rules"]:
        if not set(rule["methods"]) <= ids or len(rule["methods"]) != 2:
            raise RuntimeError(f"Reguła pary odwołuje się do nieznanej metody: {rule['methods']}")
        if rule["relation_type"] == "hard_conflict" and not rule.get("requires_confirmed_facts"):
            raise RuntimeError("hard_conflict bez potwierdzonych przesłanek jest zabroniony")
    return data


def validate_methods(data: dict) -> None:
    ids = [m["method_id"] for m in data["methods"]]
    if len(ids) != len(set(ids)) or sorted(ids) != sorted(METHOD_IDS):
        raise RuntimeError(f"Katalog musi zawierać każdą z 11 metod dokładnie raz, ma: {ids}")
    for m in data["methods"]:
        for p in m["target_problems"]["primary"] + m["target_problems"]["secondary"]:
            if p not in PROBLEM_VOCABULARY:
                raise RuntimeError(f"{m['method_id']}: nieznany problem {p}")
        for c in m["conditions"]:
            if c["severity"] not in SEVERITIES or c["expect"] not in ("TRUE", "FALSE"):
                raise RuntimeError(f"{m['method_id']}: niepoprawny warunek {c['condition_id']}")


def methods_by_id() -> dict[str, dict]:
    return {m["method_id"]: m for m in load_methods()["methods"]}


def catalog_order(method_id: str) -> int:
    return methods_by_id()[method_id]["catalog_order"]
