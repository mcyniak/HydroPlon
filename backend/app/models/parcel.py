"""Etap 1 — kanoniczny ParcelProfile (wspólny dla /by-id i /by-point)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, Field

from app.models.common import DataProvenance, StrictModel


class AdministrativeLocation(StrictModel):
    voivodeship: str | None = None
    county: str | None = None
    commune: str | None = None
    region: str | None = None
    parcel_number: str | None = None
    full_label: str | None = None


class ParcelMetrics(StrictModel):
    area_m2: float = Field(gt=0, description="Pole obliczone z geometrii w EPSG:2180 (nie pole ewidencyjne)")
    area_ha: float = Field(gt=0, description="Prezentacyjne zaokrąglenie; dla < 0,05 ha 4 miejsca po przecinku")
    perimeter_m: float = Field(gt=0)
    is_multipart: bool
    part_count: int = Field(ge=1)
    area_method_note: str = (
        "Pole obliczone z geometrii w układzie EPSG:2180 (odwzorowanie Transverse Mercator); "
        "nie jest to powierzchnia ewidencyjna z rejestru."
    )


class ParcelCoordinates(StrictModel):
    wkt_2180: str
    centroid_x_2180: float
    centroid_y_2180: float
    centroid_lon: float
    centroid_lat: float
    centroid_inside_parcel: bool
    point_on_surface_x_2180: float
    point_on_surface_y_2180: float
    point_on_surface_lon: float
    point_on_surface_lat: float
    bbox_2180: list[float] = Field(min_length=4, max_length=4)
    bbox_4326: list[float] = Field(min_length=4, max_length=4)


class ParcelSourceInfo(DataProvenance):
    datasource: str | None = None
    fetched_at: str | None = None
    geometry_repaired: bool = False


class ParcelProfile(StrictModel):
    status: Literal["ok"] = "ok"
    parcel_id: str = Field(validation_alias=AliasChoices("parcel_id", "teryt_id"))
    administrative_location: AdministrativeLocation
    metrics: ParcelMetrics
    coordinates: ParcelCoordinates
    geojson_feature: dict[str, Any]
    geometry_version: str
    source: ParcelSourceInfo
    notices: list[str]
    from_cache: bool = False

    @property
    def teryt_id(self) -> str:  # alias historyczny
        return self.parcel_id
