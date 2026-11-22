"""Wspólne słowniki i typy kontraktów HydroPlon."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Kanoniczny słownik statusów modułów (specyfikacja §9). "no_data" ze specyfikacji
# kolegi i istniejących silników jest aliasem "unavailable" (rejestr R04).
ModuleStatus = Literal["ok", "partial", "unavailable", "unknown", "error"]
_STATUS_ALIASES = {
    "no_data": "unavailable",
    "not_available": "unavailable",
    "not_implemented": "unavailable",
    "failed": "error",
}
CANONICAL_STATUSES = ("ok", "partial", "unavailable", "unknown", "error")

# Logika trójwartościowa (§36.2)
Tri = Literal["TRUE", "FALSE", "UNKNOWN"]

SourceType = Literal["fixture", "live", "derived", "farmer_declared", "catalog", "user_input"]
Confidence = Literal["high", "medium", "low", "unknown"]


def normalize_status(value: Any) -> str:
    raw = str(value or "unknown").strip().lower()
    raw = _STATUS_ALIASES.get(raw, raw)
    return raw if raw in CANONICAL_STATUSES else "unknown"


_CONFIDENCE_ALIASES = {
    "wysoka": "high", "high": "high",
    "srednia": "medium", "średnia": "medium", "medium": "medium",
    "orientacyjna": "low", "niska": "low", "low": "low",
}


def normalize_confidence(value: Any) -> str:
    return _CONFIDENCE_ALIASES.get(str(value or "").strip().lower(), "unknown")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class OpenModel(BaseModel):
    """Model dopuszczający dodatkowe pola (wyniki usług w formacie specyfikacji kolegi)."""
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class DataProvenance(StrictModel):
    source_type: SourceType
    is_demonstration: bool
    service: str
    note_pl: str | None = None


class SourceRef(StrictModel):
    module: str
    label: str
    source_type: SourceType | None = None
    is_demonstration: bool | None = None
    reference_only: bool | None = None


class ErrorResponse(StrictModel):
    error_code: str
    message_pl: str
    request_id: str
    details: dict[str, Any] = Field(default_factory=dict)
