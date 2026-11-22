"""Etap 2 — uruchomienie pięciu usług i diagnozy; zwraca AnalysisSnapshot."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_orchestrator
from app.engine.orchestrator import Orchestrator
from app.models.analysis import AnalysisRequest, AnalysisSnapshot

router = APIRouter(prefix="/api/analysis", tags=["Etap 2 — analiza i diagnoza"])


@router.post("", response_model=AnalysisSnapshot)
def create_analysis(req: AnalysisRequest, orch: Orchestrator = Depends(get_orchestrator)) -> AnalysisSnapshot:
    """`as_of_date` (alias `target_date`) domyślnie = dzisiejsza data w Polsce. Geometria jest pobierana przez
    backend — nie przyjmujemy geometrii z przeglądarki."""
    return orch.analyze(req.parcel_id, req.as_of_date, knowledge_cutoff=req.knowledge_cutoff_date,
                        strict_backtest=req.strict_backtest, time_budget_s=req.time_budget_s)


@router.get("/{analysis_id}", response_model=AnalysisSnapshot)
def get_analysis(analysis_id: str, orch: Orchestrator = Depends(get_orchestrator)) -> AnalysisSnapshot:
    return orch.get_analysis(analysis_id)
