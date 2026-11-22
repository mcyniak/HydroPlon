"""Wspólny interfejs adapterów źródeł danych (wymienne: fixture / live)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.services.uldk_service import RawUldkParcel


@dataclass(frozen=True)
class AdapterInfo:
    name: str
    module: str
    source_type: str          # fixture | live
    implemented: bool
    verified: bool            # czy interfejs zewnętrzny został potwierdzony testem w tym środowisku
    description_pl: str
    source_version: str


class DataAdapters(Protocol):
    """Zestaw adapterów jednego trybu. Każda metoda zwraca SUROWE dane źródła."""
    mode: str

    def info(self) -> list[AdapterInfo]: ...
    def parcel_by_id(self, teryt_id: str) -> RawUldkParcel: ...
    def parcel_by_xy(self, lon: float, lat: float) -> RawUldkParcel: ...
    def soil_raw(self, parcel_id: str, parcel: Any) -> dict: ...
    def terrain_raw(self, parcel_id: str, parcel: Any) -> dict: ...
    def hydrology_raw(self, parcel_id: str, parcel: Any) -> dict: ...
    def weather_raw(self, parcel_id: str, parcel: Any) -> dict: ...
    def satellite_raw(self, parcel_id: str, parcel: Any) -> dict: ...
    def source_versions(self) -> dict[str, str]: ...
