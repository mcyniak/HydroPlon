"""Etap 2 — TimeContext, wyniki pięciu usług, DiagnosisResult i AnalysisSnapshot."""
from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import AliasChoices, Field, field_validator, model_validator

from app.models.common import ModuleStatus, OpenModel, StrictModel, normalize_status
from app.models.parcel import ParcelProfile

TimeMode = Literal["historical", "current", "future"]


class TimeContext(StrictModel):
    """Sześć pojęć czasu (§23.1). ``as_of_date`` jest kanoniczne (alias: target_date)."""
    as_of_date: date = Field(validation_alias=AliasChoices("as_of_date", "target_date"))
    knowledge_cutoff_date: date
    reference_today: date
    mode: TimeMode
    implementation_start_date: date | None = None
    finance_horizon_years: int = Field(default=10, ge=1, le=50)
    strict_backtest: bool = False
    note_pl: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "TimeContext":
        if self.knowledge_cutoff_date > self.reference_today:
            raise ValueError("knowledge_cutoff_date nie może być późniejsza niż data wygenerowania analizy")
        expected = ("historical" if self.as_of_date < self.reference_today
                    else "current" if self.as_of_date == self.reference_today else "future")
        if self.mode != expected:
            raise ValueError(f"mode={self.mode} niezgodny z datami (oczekiwano {expected})")
        return self


class ModuleMetadata(OpenModel):
    sources: list[str] = Field(default_factory=list)
    source_type: str = "unknown"
    is_demonstration: bool = False
    confidence: str = "unknown"
    data_is_reference: bool = False
    is_historical_snapshot: bool = False
    observation_cutoff: str | None = None
    last_observation_date: str | None = None
    source_version: str | None = None
    limitations: list[str] = Field(default_factory=list)
    unavailable_reason: str | None = None


class ModuleResult(OpenModel):
    """Wynik jednej usługi etapu 2. Dodatkowe pola zachowują format specyfikacji kolegi."""
    status: ModuleStatus
    metadata: ModuleMetadata

    @field_validator("status", mode="before")
    @classmethod
    def _norm(cls, v: Any) -> str:
        return normalize_status(v)


class DiagnosisEvidence(OpenModel):
    module: str
    evidence_status: str
    source_confidence: str | None = None
    source_label: Any = None
    reference_only: bool | None = None
    limitations: list[str] = Field(default_factory=list)


class DiagnosisProblem(OpenModel):
    problem_id: str
    severity: Literal["low", "moderate", "high"]
    confidence: Literal["high", "medium", "low", "unknown"]
    evidence_status: str
    reasons: list[str]
    reason_codes: list[str]
    supporting_indicators: dict[str, Any]
    source_refs: list[str]
    missing_inputs: list[str]
    required_verifications: list[str]
    limitations: list[str]
    evidence: list[DiagnosisEvidence]


class DiagnosisConstraint(OpenModel):
    constraint_id: str
    status: str
    required_verification: str
    reason_pl: str
    source_refs: list[str]


class DataGap(OpenModel):
    module: str
    status: str
    cannot_determine: list[str]
    reason_pl: str


class DiagnosisResult(OpenModel):
    status: ModuleStatus
    diagnosis_version: str
    analysis_snapshot_id: str | None
    parcel_id: str | None
    as_of_date: str
    problems: list[DiagnosisProblem]
    constraints: list[DiagnosisConstraint]
    supporting_signals: list[dict[str, Any]]
    data_gaps: list[DataGap] = Field(default_factory=list)
    missing_modules: list[str]
    warnings: list[str]

    @field_validator("status", mode="before")
    @classmethod
    def _norm(cls, v: Any) -> str:
        return normalize_status(v)


class AnalysisModules(StrictModel):
    soil: ModuleResult
    terrain: ModuleResult
    hydrology: ModuleResult
    weather: ModuleResult
    satellite: ModuleResult


class AnalysisSnapshot(StrictModel):
    analysis_id: str = Field(validation_alias=AliasChoices("analysis_id", "analysis_snapshot_id"))
    status: ModuleStatus
    data_mode: Literal["fixture", "live"]
    parcel: ParcelProfile
    time_context: TimeContext
    modules: AnalysisModules
    module_statuses: dict[str, ModuleStatus]
    diagnosis: DiagnosisResult
    source_versions: dict[str, str]
    rules_versions: dict[str, str]
    warnings: list[str]
    generated_at: str
    timings_s: dict[str, float | None] = Field(default_factory=dict, description="Czas modułów (s); None = po budżecie")

    @property
    def analysis_snapshot_id(self) -> str:
        return self.analysis_id

    def engine_payload(self) -> dict[str, Any]:
        """Postać słownikowa wymagana przez istniejące czyste silniki (diagnosis, crop_water_demand)."""
        return {
            "analysis_id": self.analysis_id,
            "analysis_snapshot_id": self.analysis_id,
            "parcel_id": self.parcel.parcel_id,
            "teryt_id": self.parcel.parcel_id,
            "as_of_date": self.time_context.as_of_date.isoformat(),
            "modules": self.modules.model_dump(mode="json"),
        }


class AnalysisRequest(StrictModel):
    parcel_id: str = Field(validation_alias=AliasChoices("parcel_id", "teryt_id"))
    as_of_date: date | None = Field(default=None, validation_alias=AliasChoices("as_of_date", "target_date"))
    knowledge_cutoff_date: date | None = None
    strict_backtest: bool = False
    time_budget_s: float | None = Field(default=None, ge=2.0, le=28.0,
                                        description="Krótszy budżet = szybki wynik częściowy; moduły po budżecie "
                                                    "kończą się w tle i trafiają do cache (ponowienie da pełny wynik).")
