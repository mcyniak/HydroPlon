"""Etap 4 — FieldContext, katalog metod, filtr, ranking, wybór."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, Field, field_validator, model_validator

from app.models.common import OpenModel, StrictModel, Tri

YesNoUnknown = Literal["yes", "no", "unknown"]
_YNU_ALIASES = {
    "tak": "yes", "yes": "yes", "true": "yes",
    "nie": "no", "no": "no", "false": "no",
    "nie_wiem": "unknown", "nie wiem": "unknown", "unknown": "unknown", "": "unknown", "none": "unknown",
}

METHOD_IDS = (
    "crop_residues", "organic_matter", "cover_crops", "reduced_tillage", "vegetative_strips",
    "contour_farming", "windbreaks", "runoff_slowing", "controlled_drainage", "retention_pond",
    "efficient_irrigation",
)

ScreeningStatus = Literal["recommended", "conditional", "not_recommended"]
PriorityClass = Literal["high", "medium", "low", "unassessed"]
RelationType = Literal["complementary", "shared_scope", "dependency", "conditional_conflict", "hard_conflict"]
SizingStatus = Literal["known", "derived", "unknown", "requires_design"]


class FieldContext(StrictModel):
    """Deklaracje rolnika z kroku 2 (§55). Domyślnie unknown; nie są dowodem technicznym ani prawnym."""
    subsurface_drainage_status: YesNoUnknown = "unknown"
    existing_retention_status: YesNoUnknown = "unknown"
    existing_retention_types: list[Literal["pond", "weir", "other"]] = Field(default_factory=list)
    source: Literal["farmer_declared"] = "farmer_declared"
    field_context_version: int = 1

    @field_validator("subsurface_drainage_status", "existing_retention_status", mode="before")
    @classmethod
    def _ynu(cls, v: Any) -> str:
        key = str(v if v is not None else "unknown").strip().lower()
        if key not in _YNU_ALIASES:
            raise ValueError("dozwolone wartości: yes/no/unknown (tak/nie/nie_wiem)")
        return _YNU_ALIASES[key]

    @model_validator(mode="after")
    def _clear_types(self) -> "FieldContext":
        # §55.2: zmiana z "Tak" na "Nie"/"Nie wiem" usuwa zaznaczone podtypy.
        if self.existing_retention_status != "yes" and self.existing_retention_types:
            object.__setattr__(self, "existing_retention_types", [])
        else:
            object.__setattr__(self, "existing_retention_types", sorted(set(self.existing_retention_types)))
        return self


class RuleEvaluation(StrictModel):
    condition_id: str
    fact: str
    value: Tri
    severity: Literal["hard", "conditional", "advisory"]
    outcome: Literal["pass", "fail", "unknown", "warning"]
    reason_code: str
    explanation_pl: str


class MethodScreening(StrictModel):
    method_id: str
    name_pl: str
    screening_status: ScreeningStatus
    need_status: Tri
    matched_problem_ids: list[str]
    reason_codes: list[str]
    explanation_pl: list[str]
    rule_evaluations: list[RuleEvaluation]
    evidence_status: str
    missing_data: list[str]
    required_verifications: list[str]
    warnings: list[str]
    existing_infrastructure_notes: list[str] = Field(default_factory=list)


class MethodFilterResponse(StrictModel):
    filter_run_id: str
    analysis_id: str
    crop_assessment_id: str
    crop_id: str
    field_context: FieldContext
    method_catalog_version: str
    rules_version: str
    facts: dict[str, Any]
    methods: list[MethodScreening]

    @model_validator(mode="after")
    def _all_eleven_once(self) -> "MethodFilterResponse":
        ids = [m.method_id for m in self.methods]
        if sorted(ids) != sorted(METHOD_IDS) or len(ids) != len(set(ids)):
            raise ValueError("Filtr musi zwrócić każdą z 11 metod dokładnie raz")
        return self


class RankedMethod(StrictModel):
    method_id: str
    name_pl: str
    screening_status: ScreeningStatus
    priority_class: PriorityClass
    order: int
    link_type: Literal["primary", "secondary", "none"]
    linked_problems: list[dict[str, Any]]
    crop_fit: Literal["supports", "neutral", "conflicts", "unknown"]
    rationale_pl: list[str]
    default_selected: bool


class MethodRankingResponse(StrictModel):
    ranking_run_id: str
    analysis_id: str
    crop_assessment_id: str
    crop_id: str
    field_context: FieldContext
    filter_run_id: str
    ranking_rules_version: str
    method_catalog_version: str
    recommended: list[RankedMethod]
    conditional: list[RankedMethod]
    not_recommended: list[RankedMethod]
    screening: MethodFilterResponse

    def all_methods(self) -> list[RankedMethod]:
        return [*self.recommended, *self.conditional, *self.not_recommended]


class RecommendationRequest(StrictModel):
    analysis_id: str = Field(validation_alias=AliasChoices("analysis_id", "analysis_snapshot_id"))
    crop_assessment_id: str
    field_context: FieldContext = Field(default_factory=FieldContext)


class MethodRelation(StrictModel):
    method_a: str
    method_b: str
    relation_type: RelationType
    reason_code: str
    explanation_pl: str
    required_verifications: list[str]
    shared_scope_keys: list[str]
    escalated_from: str | None = None


class QuantityRequirement(StrictModel):
    method_id: str
    quantity_kind: str
    unit: Literal["ha", "m2", "m", "m3", "szt.", "t", "kg", "usługa"]
    quantity_value: float | None
    sizing_status: SizingStatus
    quantity_source: str
    reliability: str
    missing_inputs: list[str]


class SelectedMethod(StrictModel):
    method_id: str
    name_pl: str
    screening_status: ScreeningStatus
    priority_class: PriorityClass
    origin: Literal["default_recommended", "manual_added"]
    blocked: bool = False


class SelectionRequest(StrictModel):
    analysis_id: str = Field(validation_alias=AliasChoices("analysis_id", "analysis_snapshot_id"))
    ranking_run_id: str
    selected_method_ids: list[str] | None = None
    user_quantities: dict[str, dict[str, float]] = Field(default_factory=dict)


class MethodSelectionResult(StrictModel):
    selection_id: str
    analysis_id: str
    ranking_run_id: str
    crop_id: str
    crop_assessment_id: str
    field_context: FieldContext
    status: Literal["ready", "conditional", "blocked", "empty"]
    selected_methods: list[SelectedMethod]
    deselected_recommended: list[str]
    method_relations: list[MethodRelation]
    shared_scope_keys: dict[str, list[str]]
    quantity_requirements: list[QuantityRequirement]
    required_verifications: list[dict[str, Any]]
    warnings: list[str]
    selection_rules_version: str
    user_quantities: dict[str, dict[str, float]] = Field(default_factory=dict)

    @property
    def selected_method_ids(self) -> list[str]:
        return [m.method_id for m in self.selected_methods]
