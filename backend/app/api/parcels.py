"""Etap 1.3 — API działek. Oba endpointy zwracają ten sam model ParcelProfile."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_orchestrator
from app.engine.orchestrator import Orchestrator
from app.models.parcel import ParcelProfile

router = APIRouter(prefix="/api/parcels", tags=["Etap 1 — działka"])


@router.get("/by-id", response_model=ParcelProfile)
def parcel_by_id(id: str = Query(..., min_length=3, max_length=64, description="Identyfikator TERYT działki"),
                 orch: Orchestrator = Depends(get_orchestrator)) -> ParcelProfile:
    return orch.parcel_by_id(id)


@router.get("/by-point", response_model=ParcelProfile)
def parcel_by_point(lat: float = Query(..., ge=-90, le=90, description="Szerokość geograficzna WGS84"),
                    lon: float = Query(..., ge=-180, le=180, description="Długość geograficzna WGS84"),
                    orch: Orchestrator = Depends(get_orchestrator)) -> ParcelProfile:
    return orch.parcel_by_point(lon, lat)
