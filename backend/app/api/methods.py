"""Etap 4 — rekomendacje (filtr + ranking) i wybór metod."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_orchestrator
from app.engine.method_catalog import load_methods
from app.engine.orchestrator import Orchestrator
from app.models.methods import MethodRankingResponse, MethodSelectionResult, RecommendationRequest, SelectionRequest

router = APIRouter(tags=["Etap 4 — metody"])


@router.get("/api/methods")
def list_methods() -> dict:
    data = load_methods()
    return {"version": data["version"], "status": data["status"], "count": len(data["methods"]),
            "methods": [{k: m[k] for k in ("method_id", "name_pl", "category", "description_pl", "target_problems",
                                           "mvp_scope")} for m in data["methods"]]}


@router.post("/api/recommendations", response_model=MethodRankingResponse)
def recommendations(req: RecommendationRequest, orch: Orchestrator = Depends(get_orchestrator)) -> MethodRankingResponse:
    """Filtr TRUE/FALSE/UNKNOWN + ranking. `field_context` domyślnie: wszystko `unknown`."""
    return orch.recommend(req.analysis_id, req.crop_assessment_id, req.field_context)


@router.post("/api/selections", response_model=MethodSelectionResult)
def selections(req: SelectionRequest, orch: Orchestrator = Depends(get_orchestrator)) -> MethodSelectionResult:
    """Brak `selected_method_ids` = domyślnie wszystkie `recommended` z danego rankingu."""
    return orch.select(req.analysis_id, req.ranking_run_id, req.selected_method_ids, req.user_quantities)
