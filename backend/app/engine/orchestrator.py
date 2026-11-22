"""Orkiestrator HydroPlon:
działka → 5 usług → diagnoza → ocena uprawy → filtr → ranking → wybór → koszty →
opłacalność → dotacje → harmonogram → raport.

* Każdy wynik trafia do repozytorium z rodzicami (śledzenie zależności).
* Identyfikatory są adresowane treścią => powtarzalność i brak mieszania wyników.
* Zanim etap użyje wyniku poprzedniego, sprawdzana jest spójność identyfikatorów
  (np. ocena uprawy z innej analizy => ConsistencyError / HTTP 409).
* PipelineSession odwzorowuje stan kreatora (§59) i unieważnia wyniki zależne
  po zmianie działki, daty, uprawy, FieldContext lub zestawu metod.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.config import settings
from app.engine.cost_engine import estimate_costs
from app.engine.crop_catalog import assess_crop
from app.engine.funding_matcher import match_funding
from app.engine.implementation_planner import build_plan
from app.engine.method_filter import run_filter
from app.engine.method_ranking import rank
from app.engine.method_selection import select_methods
from app.engine.profitability_engine import evaluate as evaluate_profitability
from app.models.analysis import AnalysisSnapshot
from app.models.crop import CropWaterAssessment
from app.models.economics import (
    BeneficiaryProfile,
    CostEstimate,
    EconomicInputs,
    FinalPlan,
    FundingMatchResult,
    ImplementationPlan,
    ProfitabilityResult,
)
from app.models.methods import FieldContext, MethodRankingResponse, MethodSelectionResult
from app.models.parcel import ParcelProfile
from app.reports.report_builder import build_final_plan
from app.services.adapters.live import get_adapters
from app.services.analysis_service import build_time_context, run_analysis
from app.services.errors import ConsistencyError, UnprocessableError
from app.services.parcel_service import resolve_by_id, resolve_by_point
from app.store import ResultStore, store as default_store


class Orchestrator:
    def __init__(self, mode: str | None = None, result_store: ResultStore | None = None, today: date | None = None,
                 adapters=None):
        self.adapters = adapters or get_adapters(mode or settings.mode)
        self.store = result_store or default_store
        self.today = today

    # ------------------------------------------------------------------ etap 1
    def parcel_by_id(self, parcel_id: str) -> ParcelProfile:
        p = resolve_by_id(parcel_id, self.adapters)
        self.store.put(f"parcel:{p.parcel_id}:{p.geometry_version}", "parcel", p)
        return p

    def parcel_by_point(self, lon: float, lat: float) -> ParcelProfile:
        p = resolve_by_point(lon, lat, self.adapters)
        self.store.put(f"parcel:{p.parcel_id}:{p.geometry_version}", "parcel", p)
        return p

    # ------------------------------------------------------------------ etap 2
    def analyze(self, parcel_id: str, as_of: date | None = None, *, knowledge_cutoff: date | None = None,
                strict_backtest: bool = False, time_budget_s: float | None = None) -> AnalysisSnapshot:
        parcel = self.parcel_by_id(parcel_id)
        tc = build_time_context(as_of, knowledge_cutoff=knowledge_cutoff, today=self.today or settings.today(),
                                strict_backtest=strict_backtest)
        snap = run_analysis(parcel, tc, self.adapters, budget_s=time_budget_s)
        self.store.put(snap.analysis_id, "analysis", snap, parents=(f"parcel:{parcel.parcel_id}:{parcel.geometry_version}",))
        return snap

    def get_analysis(self, analysis_id: str) -> AnalysisSnapshot:
        return self.store.get(analysis_id, "analysis")

    # ------------------------------------------------------------------ etap 3
    def assess(self, analysis_id: str, crop_id: str) -> CropWaterAssessment:
        snap = self.get_analysis(analysis_id)
        ca = assess_crop(snap, crop_id)
        self.store.put(ca.crop_assessment_id, "crop_assessment", ca, parents=(analysis_id,))
        return ca

    # ------------------------------------------------------------------ etap 4
    def recommend(self, analysis_id: str, crop_assessment_id: str, field_context: FieldContext | None = None
                  ) -> MethodRankingResponse:
        snap = self.get_analysis(analysis_id)
        ca: CropWaterAssessment = self.store.get(crop_assessment_id, "crop_assessment")
        if ca.analysis_id != analysis_id:
            raise ConsistencyError("Ocena uprawy należy do innej analizy działki — nie można ich połączyć.",
                                   details={"analysis_id": analysis_id, "crop_assessment_analysis_id": ca.analysis_id})
        rk = rank(run_filter(snap, ca, field_context or FieldContext()))
        self.store.put(rk.ranking_run_id, "ranking", rk, parents=(crop_assessment_id,))
        return rk

    def select(self, analysis_id: str, ranking_run_id: str, selected_method_ids: list[str] | None = None,
               user_quantities: dict | None = None, cultivated_area_ha: float | None = None) -> MethodSelectionResult:
        rk: MethodRankingResponse = self.store.get(ranking_run_id, "ranking")
        if rk.analysis_id != analysis_id:
            raise ConsistencyError("Ranking pochodzi z innej analizy niż wskazana.",
                                   details={"analysis_id": analysis_id, "ranking_analysis_id": rk.analysis_id})
        snap = self.get_analysis(analysis_id)
        sel = select_methods(rk, snap.parcel, selected_method_ids, user_quantities, cultivated_area_ha)
        self.store.put(sel.selection_id, "selection", sel, parents=(ranking_run_id,))
        return sel

    # ------------------------------------------------------------------ etap 5
    def _chain(self, selection_id: str) -> tuple[MethodSelectionResult, MethodRankingResponse, AnalysisSnapshot,
                                                  CropWaterAssessment]:
        sel: MethodSelectionResult = self.store.get(selection_id, "selection")
        rk: MethodRankingResponse = self.store.get(sel.ranking_run_id, "ranking")
        snap = self.get_analysis(sel.analysis_id)
        ca: CropWaterAssessment = self.store.get(rk.crop_assessment_id, "crop_assessment")
        if not (rk.analysis_id == sel.analysis_id == ca.analysis_id == snap.analysis_id):
            raise ConsistencyError("Niespójny łańcuch identyfikatorów (analiza / uprawa / ranking / wybór).")
        return sel, rk, snap, ca

    def costs(self, selection_id: str) -> CostEstimate:
        sel, rk, snap, _ = self._chain(selection_id)
        area = next((q.quantity_value for q in sel.quantity_requirements if q.quantity_kind == "work_area_ha"
                     and q.quantity_value), snap.parcel.metrics.area_ha)
        ce = estimate_costs(sel, rk.screening.facts, area)
        self.store.put(ce.cost_estimate_id, "cost_estimate", ce, parents=(selection_id,))
        return ce

    def profitability(self, cost_estimate_id: str, inputs: EconomicInputs | None = None) -> ProfitabilityResult:
        ce: CostEstimate = self.store.get(cost_estimate_id, "cost_estimate")
        sel, _, snap, _ = self._chain(ce.selection_id)
        pr = evaluate_profitability(ce, sel, inputs or EconomicInputs(), snap.parcel.metrics.area_ha,
                                    snap.time_context.finance_horizon_years)
        self.store.put(pr.profitability_id, "profitability", pr, parents=(cost_estimate_id,))
        return pr

    def funding(self, selection_id: str, cost_estimate_id: str, profile: BeneficiaryProfile | None = None
                ) -> FundingMatchResult:
        sel, _, snap, _ = self._chain(selection_id)
        ce: CostEstimate = self.store.get(cost_estimate_id, "cost_estimate")
        if ce.selection_id != selection_id:
            raise ConsistencyError("Kosztorys dotyczy innego zestawu metod.")
        fm = match_funding(sel, ce, snap.time_context, profile or BeneficiaryProfile())
        self.store.put(fm.funding_match_id, "funding", fm, parents=(selection_id, cost_estimate_id))
        return fm

    def plan(self, selection_id: str, *, economic_inputs: EconomicInputs | None = None,
             beneficiary_profile: BeneficiaryProfile | None = None, user_quantities: dict | None = None,
             implementation_start: date | None = None) -> FinalPlan:
        sel, rk, snap, ca = self._chain(selection_id)
        economic_inputs = economic_inputs or EconomicInputs()
        if user_quantities or economic_inputs.cultivated_area_ha:
            sel = self.select(sel.analysis_id, sel.ranking_run_id, sel.selected_method_ids,
                              {**sel.user_quantities, **(user_quantities or {})}, economic_inputs.cultivated_area_ha)
        if sel.status == "empty":
            raise UnprocessableError("Nie wybrano żadnej metody — plan nie może zostać utworzony.")
        if sel.status == "blocked":
            raise UnprocessableError("Zestaw zawiera metodę z potwierdzonym przeciwwskazaniem lub twardy konflikt.",
                                     details={"warnings": sel.warnings})
        ce = self.costs(sel.selection_id)
        pr = self.profitability(ce.cost_estimate_id, economic_inputs)
        fm = self.funding(sel.selection_id, ce.cost_estimate_id, beneficiary_profile)
        screening = {s.method_id: s for s in rk.screening.methods}
        ip: ImplementationPlan = build_plan(sel, rk.screening.facts, ce, fm, screening, implementation_start)
        self.store.put(ip.implementation_plan_id, "implementation_plan", ip, parents=(ce.cost_estimate_id, fm.funding_match_id))
        fp = build_final_plan(snapshot=snap, assessment=ca, ranking=rk, selection=sel, cost=ce, profitability=pr,
                              funding=fm, implementation=ip)
        self.store.put(fp.plan_id, "plan", fp, parents=(ip.implementation_plan_id, pr.profitability_id))
        return fp

    # ------------------------------------------------------------------ wygoda
    def run_full(self, parcel_id: str, crop_id: str, *, as_of: date | None = None,
                 field_context: FieldContext | None = None, add_methods: list[str] | None = None,
                 remove_methods: list[str] | None = None, economic_inputs: EconomicInputs | None = None,
                 beneficiary_profile: BeneficiaryProfile | None = None, knowledge_cutoff: date | None = None,
                 strict_backtest: bool = False) -> dict[str, Any]:
        snap = self.analyze(parcel_id, as_of, knowledge_cutoff=knowledge_cutoff, strict_backtest=strict_backtest)
        ca = self.assess(snap.analysis_id, crop_id)
        rk = self.recommend(snap.analysis_id, ca.crop_assessment_id, field_context)
        chosen = [m.method_id for m in rk.recommended]
        chosen = [m for m in chosen if m not in (remove_methods or [])]
        chosen += [m for m in (add_methods or []) if m not in chosen]
        sel = self.select(snap.analysis_id, rk.ranking_run_id, chosen)
        fp = self.plan(sel.selection_id, economic_inputs=economic_inputs, beneficiary_profile=beneficiary_profile)
        return {"analysis": snap, "crop_assessment": ca, "ranking": rk, "selection": sel, "plan": fp}


@dataclass
class PipelineSession:
    """Stan pojedynczego kreatora (§59) z unieważnianiem wyników zależnych (§53)."""
    orch: Orchestrator
    parcel_id: str | None = None
    as_of: date | None = None
    analysis_id: str | None = None
    crop_id: str | None = None
    crop_assessment_id: str | None = None
    field_context: FieldContext = field(default_factory=FieldContext)
    ranking_run_id: str | None = None
    selected_method_ids: list[str] = field(default_factory=list)
    has_manually_edited_methods: bool = False
    selection_id: str | None = None
    plan_id: str | None = None
    stale_ids: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)

    _ORDER = ("analysis_id", "crop_assessment_id", "ranking_run_id", "selection_id", "plan_id")

    def _invalidate_from(self, level: str, reason: str) -> None:
        for name in self._ORDER[self._ORDER.index(level):]:
            old = getattr(self, name)
            if old:
                self.stale_ids.append(old)
                self.log.append(f"unieważniono {name}={old} ({reason})")
            setattr(self, name, None)

    def is_current(self, record_id: str) -> bool:
        return record_id in {getattr(self, n) for n in self._ORDER} and record_id not in self.stale_ids

    def set_parcel(self, parcel_id: str, as_of: date | None = None) -> AnalysisSnapshot:
        if (parcel_id, as_of) != (self.parcel_id, self.as_of):
            self._invalidate_from("analysis_id", "zmiana działki lub daty")
        self.parcel_id, self.as_of = parcel_id, as_of
        snap = self.orch.analyze(parcel_id, as_of)
        self.analysis_id = snap.analysis_id
        if self.crop_id:
            self._reassess()
        return snap

    def _reassess(self) -> None:
        ca = self.orch.assess(self.analysis_id, self.crop_id)
        self.crop_assessment_id = ca.crop_assessment_id
        self._rerank()

    def _rerank(self) -> None:
        rk = self.orch.recommend(self.analysis_id, self.crop_assessment_id, self.field_context)
        new_ranking = rk.ranking_run_id != self.ranking_run_id
        self.ranking_run_id = rk.ranking_run_id
        if new_ranking:
            # Domyślny zestaw "recommended" inicjalizujemy tylko dla NOWEGO rankingu (§59).
            self.selected_method_ids = [m.method_id for m in rk.recommended]
            self.has_manually_edited_methods = False
        self._reselect()

    def _reselect(self) -> None:
        sel = self.orch.select(self.analysis_id, self.ranking_run_id, list(self.selected_method_ids))
        self.selection_id = sel.selection_id

    def set_crop(self, crop_id: str) -> CropWaterAssessment:
        if crop_id != self.crop_id:
            self._invalidate_from("crop_assessment_id", "zmiana uprawy")
        self.crop_id = crop_id
        self._reassess()
        return self.orch.store.get(self.crop_assessment_id, "crop_assessment")

    def set_field_context(self, fc: FieldContext) -> MethodRankingResponse:
        if fc != self.field_context:
            self._invalidate_from("ranking_run_id", "zmiana deklaracji FieldContext")
        self.field_context = fc
        self._rerank()
        return self.orch.store.get(self.ranking_run_id, "ranking")

    def set_methods(self, method_ids: list[str]) -> MethodSelectionResult:
        if list(method_ids) != self.selected_method_ids:
            self._invalidate_from("selection_id", "ręczna zmiana zestawu metod")
            self.has_manually_edited_methods = True
        self.selected_method_ids = list(method_ids)
        self._reselect()
        return self.orch.store.get(self.selection_id, "selection")

    def build_plan(self, **kwargs) -> FinalPlan:
        fp = self.orch.plan(self.selection_id, **kwargs)
        if self.plan_id and self.plan_id != fp.plan_id:
            self.stale_ids.append(self.plan_id)
        self.plan_id = fp.plan_id
        return fp
