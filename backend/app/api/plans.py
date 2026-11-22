"""Etap 5 — koszty, opłacalność, dotacje (endpointy testowe) i plan końcowy z raportem."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse, Response
from pydantic import Field

from app.api.deps import get_orchestrator
from app.engine.orchestrator import Orchestrator
from app.models.common import StrictModel
from app.models.economics import (
    BeneficiaryProfile,
    CostEstimate,
    EconomicInputs,
    FinalPlan,
    FundingMatchResult,
    PlanRequest,
    ProfitabilityResult,
)
from app.reports.report_builder import render_pdf, render_text

router = APIRouter(tags=["Etap 5 — plan inwestycyjny"])


class CostRequest(StrictModel):
    selection_id: str


class ProfitabilityRequest(StrictModel):
    cost_estimate_id: str
    economic_inputs: EconomicInputs = Field(default_factory=EconomicInputs)


class FundingRequest(StrictModel):
    selection_id: str
    cost_estimate_id: str
    beneficiary_profile: BeneficiaryProfile = Field(default_factory=BeneficiaryProfile)


@router.post("/api/costs/estimate", response_model=CostEstimate)
def costs(req: CostRequest, orch: Orchestrator = Depends(get_orchestrator)) -> CostEstimate:
    return orch.costs(req.selection_id)


@router.post("/api/profitability/evaluate", response_model=ProfitabilityResult)
def profitability(req: ProfitabilityRequest, orch: Orchestrator = Depends(get_orchestrator)) -> ProfitabilityResult:
    return orch.profitability(req.cost_estimate_id, req.economic_inputs)


@router.post("/api/funding/match", response_model=FundingMatchResult)
def funding(req: FundingRequest, orch: Orchestrator = Depends(get_orchestrator)) -> FundingMatchResult:
    return orch.funding(req.selection_id, req.cost_estimate_id, req.beneficiary_profile)


@router.post("/api/plans", response_model=FinalPlan)
def create_plan(req: PlanRequest, orch: Orchestrator = Depends(get_orchestrator)) -> FinalPlan:
    return orch.plan(req.selection_id, economic_inputs=req.economic_inputs, beneficiary_profile=req.beneficiary_profile,
                     user_quantities=req.user_quantities, implementation_start=req.implementation_start_date)


@router.get("/api/plans/{plan_id}", response_model=FinalPlan)
def get_plan(plan_id: str, orch: Orchestrator = Depends(get_orchestrator)) -> FinalPlan:
    return orch.store.get(plan_id, "plan")


@router.get("/api/plans/{plan_id}/report", response_class=PlainTextResponse)
def plan_report(plan_id: str, orch: Orchestrator = Depends(get_orchestrator)) -> str:
    return render_text(orch.store.get(plan_id, "plan"))


@router.get("/api/plans/{plan_id}/pdf")
def plan_pdf(plan_id: str, orch: Orchestrator = Depends(get_orchestrator)) -> Response:
    pdf = render_pdf(orch.store.get(plan_id, "plan"))
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="hydroplon_{plan_id}.pdf"'})
