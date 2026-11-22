"""Etap 5 — koszty, opłacalność, dotacje, plan wdrożenia, plan końcowy."""
from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import Field, model_validator

from app.models.common import ModuleStatus, OpenModel, StrictModel

CostStatus = Literal["priced", "requires_quote", "requires_sizing", "requires_design"]


class CostItem(StrictModel):
    cost_item_id: str
    component_id: str
    name_pl: str
    method_ids: list[str]
    shared: bool
    cost_type: Literal["CAPEX", "OPEX"]
    cost_category: str
    frequency: Literal["one_time", "annual"]
    quantity: float | None
    unit: str
    quantity_status: str
    unit_price_net_pln: float | None
    vat_rate: float | None
    total_net_pln: float | None
    total_gross_pln: float | None
    status: CostStatus
    price_source: str | None
    price_as_of_date: str | None
    is_demonstration: bool
    applies_to_existing_infrastructure: bool = False
    notes_pl: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_fake_zero(self) -> "CostItem":
        if self.status != "priced" and self.total_net_pln is not None:
            raise ValueError("Pozycja niewyceniona nie może mieć kwoty (brak danych ≠ 0 zł)")
        if self.status == "priced" and (self.total_net_pln is None or self.quantity is None
                                        or self.unit_price_net_pln is None):
            raise ValueError("Pozycja wyceniona wymaga ilości, ceny jednostkowej i kwoty")
        return self


class CostTotals(StrictModel):
    capex_known_net_pln: float | None
    capex_known_gross_pln: float | None
    opex_annual_known_net_pln: float | None
    opex_annual_known_gross_pln: float | None
    capex_is_complete: bool
    opex_is_complete: bool
    capex_per_ha_known_net_pln: float | None
    reference_area_ha: float | None
    unpriced_item_ids: list[str]
    total_status: Literal["complete", "partial", "unknown"]
    explanation_pl: str


class CostEstimate(StrictModel):
    cost_estimate_id: str
    selection_id: str
    analysis_id: str
    status: ModuleStatus
    price_catalog_version: str
    price_catalog_is_demonstration: bool
    price_as_of_date: str | None
    items: list[CostItem]
    totals: CostTotals
    per_method: dict[str, dict[str, Any]]
    deduplicated_shared_items: list[dict[str, Any]]
    assumptions: list[str]
    warnings: list[str]
    engine_version: str


class EconomicInputs(StrictModel):
    """Opcjonalne nadpisania rolnika (§4 części III): plon, cena, koszt; plus jawny scenariusz korzyści."""
    own_yield_t_ha: float | None = Field(default=None, gt=0)
    own_price_pln_t: float | None = Field(default=None, gt=0)
    cultivated_area_ha: float | None = Field(default=None, gt=0)
    scenario_annual_benefit_pln: dict[str, float] = Field(
        default_factory=dict,
        description="Jawny scenariusz użytkownika: roczna korzyść netto per metoda (zł). Nie jest prognozą systemu.")
    scenario_yield_change_t_ha: float | None = Field(default=None, allow_inf_nan=False)
    scenario_annual_savings_pln: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    payback_target_years: int | None = Field(default=None, ge=1, le=50)


class ProfitabilityResult(StrictModel):
    profitability_id: str
    cost_estimate_id: str
    status: ModuleStatus
    result_kind: Literal["break_even_only", "scenario", "insufficient_data"]
    baseline: dict[str, Any]
    investment_variant: dict[str, Any]
    annual_net_benefit_pln: float | None
    simple_payback_years: float | None
    break_even: dict[str, Any]
    sensitivity_cases: list[dict[str, Any]] = Field(default_factory=list)
    yield_effect: dict[str, Any]
    overlap_warnings: list[str]
    assumptions: list[str]
    warnings: list[str]
    headline_pl: str
    engine_version: str


class BeneficiaryProfile(StrictModel):
    is_active_farmer: bool | None = None
    has_producer_number: bool | None = None
    has_legal_title_to_parcel: bool | None = None
    is_young_farmer: bool | None = None


class FundingMatch(StrictModel):
    program_id: str
    program_name_pl: str
    institution: str
    support_type: str
    method_ids: list[str]
    cost_item_ids: list[str]
    action_eligibility: Literal["TRUE", "FALSE", "UNKNOWN"]
    beneficiary_eligibility: Literal["TRUE", "FALSE", "UNKNOWN"]
    match_status: Literal["possible", "needs_verification", "not_eligible"]
    call_id: str | None
    call_status_on_as_of: Literal["open", "closed", "announced", "unknown"]
    call_status_now: Literal["open", "closed", "announced", "unknown"]
    call_window: dict[str, Any] | None
    is_historical_view: bool
    conditions_pl: list[str]
    missing_data: list[str]
    reasons_pl: list[str]
    official_source_url: str | None
    verified_at: str | None
    is_demonstration: bool
    disclaimer_pl: str
    code: str | None = None
    rates_pl: list[str] = Field(default_factory=list)
    estimate_pl: list[str] = Field(default_factory=list)
    method_practices_pl: dict[str, str] = Field(default_factory=dict)
    how_to_apply_pl: str | None = None
    call_basis_pl: str | None = None
    source_urls: list[str] = Field(default_factory=list)


class FundingMatchResult(StrictModel):
    funding_match_id: str
    selection_id: str
    as_of_date: str
    knowledge_cutoff_date: str
    reference_today: str
    status: ModuleStatus
    catalog_version: str
    catalog_is_demonstration: bool
    matches: list[FundingMatch]
    unmatched_method_ids: list[str]
    double_funding_warnings: list[str]
    warnings: list[str]
    engine_version: str


class PlanTask(StrictModel):
    task_id: str
    template_id: str
    phase: int = Field(ge=1, le=8)
    phase_label_pl: str
    order: int
    title_pl: str
    purpose_pl: str
    rationale_pl: list[str]
    timing_pl: str
    depends_on: list[str]
    method_ids: list[str]
    cost_item_ids: list[str]
    resources_pl: list[str]
    requires_specialist: bool
    start_condition_pl: str
    completion_check_pl: str
    monitoring_indicator: str | None
    status: Literal["ready_to_consider", "requires_verification"]
    source_rule: str
    program_ids: list[str] = Field(default_factory=list)
    window_from: date | None = None
    window_to: date | None = None
    window_note_pl: str | None = None


class ImplementationPlan(StrictModel):
    implementation_plan_id: str
    selection_id: str
    status: Literal["ok", "conditional", "error"]
    tasks: list[PlanTask]
    milestones: list[dict[str, Any]]
    stop_go_conditions: list[str]
    cycle_detected: list[str] | None
    deduplicated_tasks: list[dict[str, Any]]
    implementation_start_date: date | None
    warnings: list[str]
    engine_version: str


class PlanRequest(StrictModel):
    selection_id: str
    economic_inputs: EconomicInputs = Field(default_factory=EconomicInputs)
    beneficiary_profile: BeneficiaryProfile = Field(default_factory=BeneficiaryProfile)
    user_quantities: dict[str, dict[str, float]] = Field(default_factory=dict)
    implementation_start_date: date | None = None


class FinalPlan(OpenModel):
    plan_id: str
    plan_version: str
    status: ModuleStatus
    generated_at: str
    ids: dict[str, str]
    versions: dict[str, str]
    time_context: dict[str, Any]
    parcel_summary: dict[str, Any]
    crop_summary: dict[str, Any]
    selected_methods: list[dict[str, Any]]
    section_1_profitability: dict[str, Any]
    section_2_costs: dict[str, Any]
    section_3_funding: dict[str, Any]
    section_4_implementation: dict[str, Any]
    section_5_analysis: dict[str, Any]
    warnings: list[str]
    data_provenance: dict[str, Any]
    disclaimers_pl: list[str]
