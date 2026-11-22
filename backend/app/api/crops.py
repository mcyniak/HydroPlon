"""Etap 3.3 — API upraw (wersja prototypu, bez Postgres).

Oryginalny router zależny od `app.db.session` i tabel `analysis_runs`/`crop_assessments`
zachowano bez zmian w `app/api/_legacy/crops_postgres.py` (rejestr R05). Ten router
zachowuje kontrakt i klucz wersjonowania (analysis_id, crop_id, catalog_version,
engine_version) — identyfikator oceny jest wyprowadzany z tego klucza.
`as_of_date` dziedziczy po analizie; nie jest przyjmowany z przeglądarki.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_orchestrator
from app.engine.crop_catalog import crop_profiles, load_catalog
from app.engine.orchestrator import Orchestrator
from app.models.crop import CropAssessRequest, CropWaterAssessment

router = APIRouter(prefix="/api/crops", tags=["Etap 3 — uprawa"])


@router.get("")
def list_crops() -> dict:
    cat = load_catalog()
    crops = crop_profiles()
    return {"status": "ok", "catalog_version": cat["version"], "count": len(crops),
            "assessable_count": sum(c.available_for_assessment for c in crops),
            "note_pl": "Wszystkie 8 upraw można wybrać; obliczenia ETc tylko dla profili z parametrami roboczymi.",
            "crops": [c.model_dump() for c in crops]}


@router.post("/assess", response_model=CropWaterAssessment)
def assess(req: CropAssessRequest, orch: Orchestrator = Depends(get_orchestrator)) -> CropWaterAssessment:
    return orch.assess(req.analysis_id, req.crop_id)
