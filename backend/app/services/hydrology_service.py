"""Etap 2, usługa 3 — hydrologia i infrastruktura wodna.

Mechanizmy: odległości poligonu działki do obiektów (Shapely, EPSG:2180),
sąsiedztwo ≤ 15 m, przecięcie stref zagrożenia powodziowego z CAŁYM poligonem
(korekta K09 — nie tylko centroid).

Zakazy: rów ≠ drenaż podziemny; bliskość cieku ≠ prawo do poboru/piętrzenia;
niedostępna warstwa ≠ brak zagrożenia (unknown, nie false).
"""
from __future__ import annotations

from typing import Any

from shapely import wkt as shp_wkt
from shapely.geometry import Point

from app.services.common import REFERENCE_NOTE_HISTORICAL, analysis_rules, base_metadata, unavailable

DITCH_CODES = {"SWRM", "SWKN"}
STREAM_CODES = {"SWRS"}
RESERVOIR_CODES = {"PTWP"}


def compute_hydrology(raw: dict, parcel: Any, as_of_mode: str) -> dict:
    rules = analysis_rules()["hydrology"]
    st, demo = raw.get("source_type", "unknown"), bool(raw.get("is_demonstration"))
    rec = raw.get("record")
    if not rec:
        return unavailable("hydrology", reason_code="no_data_in_source", source_type=st, is_demonstration=demo,
                           message_pl="Brak danych hydrologicznych dla działki.", source_version=raw.get("version"))
    geom = shp_wkt.loads(parcel.coordinates.wkt_2180)
    centroid = Point(parcel.coordinates.centroid_x_2180, parcel.coordinates.centroid_y_2180)
    limitations = [
        "Obecność rowu lub cieku na mapie nie oznacza prawa do poboru wody ani wykonywania prac piętrzących bez "
        "wymaganych zgód wodnoprawnych.",
        "Publiczne rejestry nie zawierają mapy sprawnego drenażu podziemnego — wymaga to potwierdzenia rolnika "
        "i oceny technicznej.",
    ]
    status = "ok"
    features_out: list[dict] = []
    nearest = {"ditch": None, "stream": None, "reservoir": None}
    has_adjacent = None
    if rec.get("features_layer_status") == "ok":
        buffer_m = rules["search_buffer_m"]
        for f in rec.get("features") or []:
            if f.get("distance_m") is not None:  # odległość wyliczona przez adapter (np. z obrazu WMS)
                d, unc = float(f["distance_m"]), float(f.get("distance_uncertainty_m") or 0.0)
            else:
                d, unc = round(geom.distance(shp_wkt.loads(f["wkt"])), 1), 0.0
            if d > buffer_m:
                continue
            features_out.append({"feature_type": f["feature_type"], "bdot_code": f["bdot_code"], "name": f.get("name"),
                                 "distance_m": d, "distance_uncertainty_m": unc or None,
                                 "intersects_or_borders_parcel": d <= max(1.0, unc)})
            key = ("ditch" if f["bdot_code"] in DITCH_CODES else "stream" if f["bdot_code"] in STREAM_CODES
                   else "reservoir" if f["bdot_code"] in RESERVOIR_CODES else None)
            if key and (nearest[key] is None or d < nearest[key]):
                nearest[key] = d
        features_out.sort(key=lambda x: (x["distance_m"], x["bdot_code"]))
        lim = rules["adjacent_distance_m_lte"]
        has_adjacent = any(v is not None and v <= lim for v in (nearest["ditch"], nearest["stream"]))
        if rec.get("failed_groups"):
            status = "partial"
            limitations.append("Nie pobrano części warstw sieci wodnej: " + ", ".join(rec["failed_groups"])
                               + " — brak tych obiektów w wyniku NIE oznacza ich nieobecności.")
            if has_adjacent is False and any(g in rec["failed_groups"] for g in ("SWRM", "SWKN", "SWRS")):
                has_adjacent = None
        if rec.get("pixel_size_m"):
            limitations.append(f"Odległości wyznaczone z obrazu mapy BDOT10k (piksel {rec['pixel_size_m']} m); "
                               f"niepewność ok. ±{2 * rec['pixel_size_m']:.0f} m.")
    else:
        status = "partial"
        limitations.append("Warstwa sieci wodnej niedostępna — odległości do obiektów wodnych nieznane.")

    flood_layer = rec.get("flood_layer_status")
    in_zone = centroid_in_zone = None
    flood_class = None
    if flood_layer == "ok":
        zones = rec.get("flood_zones") or []
        hits = [z for z in zones if shp_wkt.loads(z["wkt"]).intersects(geom)]
        in_zone = bool(hits)
        centroid_in_zone = any(shp_wkt.loads(z["wkt"]).covers(centroid) for z in zones)
        flood_class = "; ".join(f"{z['hazard_class']}" + (f" ({z['share_of_parcel_pct']}% działki)"
                                                          if z.get("share_of_parcel_pct") is not None else "")
                                for z in hits) if hits else None
        if in_zone and not centroid_in_zone:
            limitations.append("Strefa zagrożenia powodziowego przecina fragment działki, choć nie obejmuje jej "
                               "środka — kontrola punktowa w centroidzie przeoczyłaby to ograniczenie.")
    else:
        status = "partial"
        limitations.append("Warstwa map zagrożenia powodziowego niedostępna — status zagrożenia NIEZNANY (nie 'brak').")
    if as_of_mode == "historical":
        limitations.append(REFERENCE_NOTE_HISTORICAL["hydrology"])
    jcwp = rec.get("jcwp") or {}
    return {
        "status": status,
        "module": "hydrology",
        "nearby_water_features": {
            "has_adjacent_ditch_or_stream": has_adjacent,
            "nearest_ditch_m": nearest["ditch"],
            "nearest_watercourse_m": nearest["stream"],
            "nearest_reservoir_m": nearest["reservoir"],
            "features_within_500m": features_out,
        },
        "flood_and_basin_context": {
            "in_flood_hazard_zone": in_zone,
            "flood_check_method": "full_polygon_intersection" if flood_layer == "ok" else "not_checked_layer_unavailable",
            "centroid_in_flood_zone": centroid_in_zone,
            "flood_hazard_class": flood_class,
            "jcwp_code": jcwp.get("code"),
            "jcwp_name": jcwp.get("name"),
        },
        "farmer_confirmation_flags": {
            "subsurface_drainage_status": "unknown_requires_confirmation",
            "water_abstraction_permit_status": "unknown_requires_confirmation",
        },
        "metadata": base_metadata(
            sources=[raw.get("source_label", "BDOT10k/ISOK")], source_type=st, is_demonstration=demo,
            confidence="wysoka" if status == "ok" else "srednia", data_is_reference=True,
            source_version=raw.get("version"), limitations=limitations, search_buffer_m=rules["search_buffer_m"],
            layer_status={"water_features": rec.get("features_layer_status"), "flood_zones": flood_layer}),
    }
