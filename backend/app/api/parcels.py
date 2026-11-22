"""Wyszukiwanie adresów oraz wybór działki według identyfikatora lub współrzędnych."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_orchestrator
from app.engine.orchestrator import Orchestrator
from app.models.parcel import AddressMatch, ParcelProfile
from app.services.address_service import search_address
from app.services.errors import UnprocessableError

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


@router.get("/address-search", response_model=list[AddressMatch])
def address_search(q: str = Query(..., min_length=3, max_length=160, description="Adres lub miejscowość"),
                   orch: Orchestrator = Depends(get_orchestrator)) -> list[AddressMatch]:
    if orch.adapters.mode == "fixture":
        raise UnprocessableError("Wyszukiwanie adresów jest dostępne w trybie z danymi rzeczywistymi.")
    return search_address(q)
