"""Etap 1, moduł 1.2 — geometria działki (Shapely + PyProj).

Korekty względem szkicu (R07, R08, R12; I08–I10):
  * osobne pola: rzeczywisty centroid i punkt wewnątrz działki (point_on_surface),
  * EPSG:2180 to odwzorowanie Transverse Mercator — pole jest "obliczone z geometrii",
  * make_valid może zwrócić GeometryCollection — wydzielamy części poligonowe,
  * MultiPolygon zachowany w całości, małe działki nie są zaokrąglane do 0,00 ha.
"""
from __future__ import annotations

import hashlib
from functools import lru_cache

from pyproj import Transformer
import shapely
from shapely import make_valid, wkt
from shapely.errors import ShapelyError
from shapely.geometry import GeometryCollection, MultiPolygon, Point, Polygon, mapping
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from app.config import settings
from app.services.errors import InputValidationError, SourceFormatError

# Przybliżony zasięg EPSG:2180 dla obszaru Polski (kontrola, czy WKT nie jest w innym układzie).
_PL_2180_BOUNDS = (100_000.0, 80_000.0, 900_000.0, 820_000.0)


@lru_cache(maxsize=4)
def _transformer(src: str, dst: str) -> Transformer:
    return Transformer.from_crs(src, dst, always_xy=True)


def to_4326(geom: BaseGeometry) -> BaseGeometry:
    t = _transformer("EPSG:2180", "EPSG:4326")
    return shapely.transform(geom, lambda x, y: t.transform(x, y), interleaved=False)


def lonlat_to_2180(lon: float, lat: float) -> Point:
    x, y = _transformer("EPSG:4326", "EPSG:2180").transform(lon, lat)
    return Point(x, y)


def _vertex_count(geom: BaseGeometry) -> int:
    if isinstance(geom, Polygon):
        return len(geom.exterior.coords) + sum(len(r.coords) for r in geom.interiors)
    if hasattr(geom, "geoms"):
        return sum(_vertex_count(g) for g in geom.geoms)
    return 0


def _polygonal_parts(geom: BaseGeometry) -> list[Polygon]:
    if isinstance(geom, Polygon):
        return [geom] if not geom.is_empty else []
    if isinstance(geom, (MultiPolygon, GeometryCollection)):
        out: list[Polygon] = []
        for g in geom.geoms:
            out.extend(_polygonal_parts(g))
        return out
    return []


def parse_and_repair(raw_wkt_2180: str) -> tuple[BaseGeometry, bool]:
    """Zwraca (Polygon|MultiPolygon, czy_naprawiono)."""
    if not raw_wkt_2180 or len(raw_wkt_2180) > settings.max_wkt_chars:
        raise SourceFormatError("Geometria działki jest pusta lub zbyt duża.")
    try:
        geom = wkt.loads(raw_wkt_2180)
    except (ShapelyError, ValueError, TypeError) as exc:
        raise SourceFormatError("Nieprawidłowy zapis WKT geometrii działki.") from exc
    if geom.is_empty:
        raise SourceFormatError("Geometria działki jest pusta.")
    if not isinstance(geom, (Polygon, MultiPolygon)):
        raise SourceFormatError(f"Oczekiwano Polygon/MultiPolygon, otrzymano {geom.geom_type}.")
    if _vertex_count(geom) > settings.max_vertices:
        raise SourceFormatError("Geometria działki ma zbyt wiele wierzchołków.")
    repaired = False
    if not geom.is_valid:
        geom = make_valid(geom)
        repaired = True
    parts = [p for p in _polygonal_parts(geom) if p.area > 0]
    if not parts:
        raise SourceFormatError("Po naprawie topologii geometria nie zawiera części poligonowych.")
    geom = parts[0] if len(parts) == 1 else unary_union(parts)
    if isinstance(geom, GeometryCollection) and not isinstance(geom, MultiPolygon):
        geom = MultiPolygon(_polygonal_parts(geom))
    if not geom.is_valid or geom.area <= 0:
        raise SourceFormatError("Geometria działki pozostaje niepoprawna po naprawie.")
    minx, miny, maxx, maxy = geom.bounds
    bx0, by0, bx1, by1 = _PL_2180_BOUNDS
    if not (bx0 <= minx and maxx <= bx1 and by0 <= miny and maxy <= by1):
        raise SourceFormatError("Współrzędne geometrii nie pasują do układu EPSG:2180 dla Polski.")
    return geom, repaired


def _round_area_ha(area_m2: float) -> float:
    ha = area_m2 / 10_000.0
    if ha < 0.05:
        r = round(ha, 4)
        return r if r > 0 else round(ha, 6)
    return round(ha, 2)


def geometry_version(geom: BaseGeometry) -> str:
    normalized = geom.normalize().wkt
    return "geom-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def process_parcel_geometry(raw_wkt_2180: str) -> dict:
    geom, repaired = parse_and_repair(raw_wkt_2180)
    area_m2 = float(geom.area)
    centroid = geom.centroid
    pos = geom.representative_point()
    geom_4326 = to_4326(geom)
    c_lon, c_lat = _transformer("EPSG:2180", "EPSG:4326").transform(centroid.x, centroid.y)
    p_lon, p_lat = _transformer("EPSG:2180", "EPSG:4326").transform(pos.x, pos.y)
    part_count = len(geom.geoms) if isinstance(geom, MultiPolygon) else 1
    return {
        "geometry": geom,
        "repaired": repaired,
        "metrics": {
            "area_m2": round(area_m2, 2),
            "area_ha": _round_area_ha(area_m2),
            "perimeter_m": round(float(geom.length), 1),
            "is_multipart": part_count > 1,
            "part_count": part_count,
        },
        "coordinates": {
            "wkt_2180": geom.wkt,
            "centroid_x_2180": round(centroid.x, 2),
            "centroid_y_2180": round(centroid.y, 2),
            "centroid_lon": round(c_lon, 6),
            "centroid_lat": round(c_lat, 6),
            "centroid_inside_parcel": bool(geom.covers(centroid)),
            "point_on_surface_x_2180": round(pos.x, 2),
            "point_on_surface_y_2180": round(pos.y, 2),
            "point_on_surface_lon": round(p_lon, 6),
            "point_on_surface_lat": round(p_lat, 6),
            "bbox_2180": [round(c, 2) for c in geom.bounds],
            "bbox_4326": [round(c, 6) for c in geom_4326.bounds],
        },
        "geojson_4326": mapping(geom_4326),
        "geometry_version": geometry_version(geom),
    }


def ensure_point_in_poland(lon: float, lat: float) -> None:
    from app.services.uldk_service import validate_lon_lat
    validate_lon_lat(lon, lat)


__all__ = ["process_parcel_geometry", "parse_and_repair", "lonlat_to_2180", "to_4326", "geometry_version",
           "InputValidationError"]
