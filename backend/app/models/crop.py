"""Etap 3 — profil uprawy i CropWaterAssessment."""
from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, Field, field_validator

from app.models.common import ModuleStatus, OpenModel, StrictModel, normalize_status


class CropProfile(OpenModel):
    crop_id: str
    name_pl: str
    category: str | None = None
    parameter_status: str
    available_for_assessment: bool
    water_sensitivity_class: str | None = None
    critical_stage_label_pl: str | None = None
    agronomic_notes_pl: str | None = None
    limitations: list[str] = Field(default_factory=list)


class CropAssessRequest(StrictModel):
    analysis_id: str = Field(validation_alias=AliasChoices("analysis_id", "analysis_snapshot_id"))
    crop_id: str


class CropWaterAssessment(OpenModel):
    """Wynik silnika 3.2 (pola silnika zachowane) + identyfikatory i wersje."""
    crop_assessment_id: str
    analysis_id: str
    parcel_id: str
    crop_id: str
    catalog_version: str
    engine_version: str
    status: ModuleStatus
    status_raw: str
    assessable: bool
    crop: dict[str, Any]
    crop_water_balance: dict[str, Any] | None = None
    crop_water_risk_level: str = "unknown"
    indicative_climatic_pressure: str = "unknown"
    monthly_crop_demand_chart: list[dict[str, Any]] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("status", mode="before")
    @classmethod
    def _norm(cls, v: Any) -> str:
        return normalize_status(v)
