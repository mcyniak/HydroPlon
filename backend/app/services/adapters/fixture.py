"""Adaptery trybu FIXTURE — wyłącznie syntetyczne dane offline (source_type=fixture, is_demonstration=true)."""
from __future__ import annotations

import json
import math
from functools import lru_cache
from typing import Any

from shapely import wkt as shp_wkt

from app.config import FIXTURES_DIR
from app.fixtures.weather_generator import generate_station_series
from app.services.adapters.base import AdapterInfo
from app.services.errors import AmbiguousResultError, NotFoundError
from app.services.geometry_service import lonlat_to_2180
from app.services.uldk_service import RawUldkParcel, parse_uldk_response


@lru_cache(maxsize=16)
def _load(name: str) -> dict:
    data = json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))
    if data.get("source_type") != "fixture" or data.get("is_demonstration") is not True:
        raise RuntimeError(f"Plik fixture {name} nie jest jawnie oznaczony jako demonstracyjny")
    return data


def _haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


_TAG = {"source_type": "fixture", "is_demonstration": True}


class FixtureAdapters:
    mode = "fixture"

    # ---------------------------------------------------------------- metadane
    def source_versions(self) -> dict[str, str]:
        return {
            "parcels": _load("parcels.json")["version"],
            "soil": _load("soil.json")["version"],
            "terrain": _load("terrain.json")["version"],
            "hydrology": _load("hydrology.json")["version"],
            "weather": _load("weather_stations.json")["version"],
            "satellite": _load("satellite.json")["version"],
        }

    def info(self) -> list[AdapterInfo]:
        v = self.source_versions()
        desc = "Syntetyczne dane offline do testów mechanizmów (nie są pomiarami)."
        return [AdapterInfo(f"fixture_{m}", m, "fixture", True, True, desc, v[m])
                for m in ("parcels", "soil", "terrain", "hydrology", "weather", "satellite")]

    # ---------------------------------------------------------------- etap 1
    def _parcel_text(self, teryt_id: str) -> str:
        data = _load("parcels.json")
        if teryt_id in data["parcels"]:
            return data["parcels"][teryt_id]["uldk_response"]
        if teryt_id in data["special_responses"]:
            return data["special_responses"][teryt_id]["uldk_response"]
        return "-1 brak wyników (fixture)\n"

    def parcel_by_id(self, teryt_id: str) -> RawUldkParcel:
        return parse_uldk_response(self._parcel_text(teryt_id), source_type="fixture", is_demonstration=True)

    def parcel_by_xy(self, lon: float, lat: float) -> RawUldkParcel:
        pt = lonlat_to_2180(lon, lat)
        hits = []
        for teryt_id, rec in _load("parcels.json")["parcels"].items():
            raw = parse_uldk_response(rec["uldk_response"], source_type="fixture", is_demonstration=True)
            if shp_wkt.loads(raw.raw_wkt_2180).covers(pt):  # covers: punkt na granicy też trafia
                hits.append(raw)
        if not hits:
            raise NotFoundError("We wskazanym miejscu nie znaleziono działki (zbiór testowy fixture).")
        if len(hits) > 1:
            raise AmbiguousResultError("Punkt leży na granicy kilku działek; wskaż działkę dokładniej.",
                                       details={"candidates": sorted(h.teryt_id for h in hits)})
        return hits[0]

    # ---------------------------------------------------------------- etap 2
    def _parcel_rec(self, name: str, parcel_id: str) -> dict | None:
        return _load(name)["parcels"].get(parcel_id)

    def soil_raw(self, parcel_id: str, parcel: Any) -> dict:
        data = _load("soil.json")
        rec = data["parcels"].get(parcel_id)
        return {**_TAG, "record": rec, "units": data["units"], "resolution_m": data["resolution_m"],
                "source_label": "SoilGrids-podobne warstwy syntetyczne (fixture)", "version": data["version"]}

    def terrain_raw(self, parcel_id: str, parcel: Any) -> dict:
        data = _load("terrain.json")
        return {**_TAG, "record": data["parcels"].get(parcel_id), "computed_from": data["computed_from"],
                "source_label": "NMT-podobne metryki syntetyczne (fixture)", "version": data["version"]}

    def hydrology_raw(self, parcel_id: str, parcel: Any) -> dict:
        data = _load("hydrology.json")
        return {**_TAG, "record": data["parcels"].get(parcel_id), "search_buffer_m": data["search_buffer_m"],
                "source_label": "BDOT10k/ISOK-podobne obiekty syntetyczne (fixture)", "version": data["version"]}

    def weather_raw(self, parcel_id: str, parcel: Any, as_of=None) -> dict:
        data = _load("weather_stations.json")
        lon = parcel.coordinates.point_on_surface_lon
        lat = parcel.coordinates.point_on_surface_lat

        def nearest(flag: str) -> tuple[str, dict, float] | None:
            cands = [(sid, s, _haversine_km(lon, lat, s["lon"], s["lat"]))
                     for sid, s in data["stations"].items() if s[flag]]
            return min(cands, key=lambda c: (c[2], c[0])) if cands else None

        out: dict[str, Any] = {**_TAG, "version": data["version"], "assignment_method": "nearest_station_pair",
                               "source_label": "Serie dobowe syntetyczne (fixture, generator deterministyczny)"}
        for role, flag in (("precip", "has_precip"), ("thermal", "has_temp")):
            hit = nearest(flag)
            if hit is None:
                out[role] = None
                continue
            sid, st, dist = hit
            series = generate_station_series(sid, st["profile"], st["archive_start"], st["archive_end"],
                                             json.dumps(st["gaps"]))
            out[role] = {"station_id": sid, "name": st["name"], "distance_km": round(dist, 1),
                         "archive_end": st["archive_end"], "series": series}
        return out

    def satellite_raw(self, parcel_id: str, parcel: Any, as_of=None) -> dict:
        data = _load("satellite.json")
        return {**_TAG, "record": data["parcels"].get(parcel_id), "edo_background": data.get("edo_background"),
                "source_label": "Sentinel-2-podobne mediany syntetyczne (fixture)", "version": data["version"]}
