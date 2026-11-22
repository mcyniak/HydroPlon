"""Wspólne narzędzia usług etapu 2."""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from app.config import DATA_DIR

REFERENCE_NOTE_HISTORICAL = {
    "soil": "Dane referencyjne współczesnego modelu glebowego, a nie pomiar z wybranego dnia historycznego.",
    "terrain": "Współczesny Numeryczny Model Terenu wykorzystany jako dane referencyjne (brak modelu rzeźby "
               "terenu z konkretnego dnia historycznego).",
    "hydrology": "Bieżące warstwy sieci wodnej i map zagrożenia powodziowego wykorzystane jako dane referencyjne "
                 "dla daty historycznej.",
}


@lru_cache(maxsize=1)
def analysis_rules() -> dict:
    return json.loads((DATA_DIR / "analysis_rules.json").read_text(encoding="utf-8"))


def base_metadata(*, sources: list[str], source_type: str, is_demonstration: bool, confidence: str,
                  data_is_reference: bool, source_version: str | None, limitations: list[str],
                  **extra: Any) -> dict:
    return {
        "sources": sources,
        "source_type": source_type,
        "is_demonstration": is_demonstration,
        "confidence": confidence,
        "data_is_reference": data_is_reference,
        "is_historical_snapshot": False,
        "source_version": source_version,
        "limitations": limitations,
        **extra,
    }


def unavailable(module: str, *, reason_code: str, message_pl: str, source_type: str, is_demonstration: bool,
                status: str = "unavailable", source_version: str | None = None, **fields: Any) -> dict:
    """Wynik modułu bez danych: status + powód; żadnych wartości zastępczych."""
    return {
        "status": status,
        "module": module,
        **fields,
        "metadata": base_metadata(
            sources=[], source_type=source_type, is_demonstration=is_demonstration, confidence="unknown",
            data_is_reference=module in ("soil", "terrain", "hydrology"), source_version=source_version,
            limitations=[message_pl], unavailable_reason=reason_code),
    }


def r(value: float | None, nd: int = 1) -> float | None:
    return None if value is None else round(float(value), nd)
