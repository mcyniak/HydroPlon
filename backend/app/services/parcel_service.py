"""Etap 1 — rozwiązywanie działki do kanonicznego ParcelProfile + wersjonowany cache.

Cache (korekta I12/K13): klucz = tryb + parcel_id; wpis przechowuje geometry_version,
wersję źródła, fetched_at i termin ważności. Wynik z cache ma tę samą strukturę
co wynik pobrany ze źródła (różni się tylko polem from_cache).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from app.models.parcel import ParcelProfile
from app.services.geometry_service import process_parcel_geometry
from app.services.uldk_service import RawUldkParcel, normalize_teryt_id, validate_lon_lat

NOTICES = [
    "Działka ewidencyjna nie musi odpowiadać jednemu polu uprawnemu — jej granice mogą obejmować różne "
    "uprawy, miedze, zadrzewienia, rowy lub fragmenty zabudowy.",
    "Pole powierzchni obliczono z geometrii (EPSG:2180); nie jest to powierzchnia ewidencyjna z rejestru.",
]


@dataclass
class _CacheEntry:
    profile: ParcelProfile
    stored_at: float
    ttl_s: float


class ParcelCache:
    def __init__(self, ttl_s: float = 7 * 24 * 3600):
        self.ttl_s = ttl_s
        self._d: dict[tuple[str, str], _CacheEntry] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, mode: str, parcel_id: str) -> ParcelProfile | None:
        with self._lock:
            e = self._d.get((mode, parcel_id))
            if e is None or (time.monotonic() - e.stored_at) > e.ttl_s:
                self.misses += 1
                return None
            self.hits += 1
            return e.profile.model_copy(update={"from_cache": True})

    def put(self, mode: str, profile: ParcelProfile) -> None:
        with self._lock:
            self._d[(mode, profile.parcel_id)] = _CacheEntry(profile.model_copy(update={"from_cache": False}),
                                                             time.monotonic(), self.ttl_s)

    def expire_all(self) -> None:
        with self._lock:
            for e in self._d.values():
                e.stored_at = -1e12

    def clear(self) -> None:
        with self._lock:
            self._d.clear()
            self.hits = self.misses = 0


parcel_cache = ParcelCache()


def build_parcel_profile(raw: RawUldkParcel) -> ParcelProfile:
    geo = process_parcel_geometry(raw.raw_wkt_2180)
    loc = dict(raw.administrative_location)
    region = loc.get("region")
    label_parts = [(region if region.lower().startswith("obręb") else f"obręb {region}") if region else None,
                   f"gm. {loc['commune']}" if loc.get("commune") else None,
                   f"pow. {loc['county']}" if loc.get("county") else None,
                   f"(woj. {loc['voivodeship']})" if loc.get("voivodeship") else None]
    loc["full_label"] = ", ".join(p for p in label_parts if p) or None
    notices = list(NOTICES)
    if raw.is_demonstration:
        notices.insert(0, "DANE TESTOWE: geometria i lokalizacja pochodzą ze zbioru syntetycznego (fixture).")
    else:
        notices.append(f"Źródło granic ewidencyjnych: Główny Urząd Geodezji i Kartografii — {raw.source_service}.")
    notices.extend(raw.notes)
    if geo["repaired"]:
        notices.append("Geometria źródłowa była topologicznie niepoprawna i została naprawiona (make_valid).")
    feature = {"type": "Feature",
               "properties": {"parcel_id": raw.teryt_id, "area_ha": geo["metrics"]["area_ha"]},
               "geometry": geo["geojson_4326"]}
    return ParcelProfile(
        parcel_id=raw.teryt_id,
        administrative_location=loc,
        metrics=geo["metrics"],
        coordinates=geo["coordinates"],
        geojson_feature=feature,
        geometry_version=geo["geometry_version"],
        source={"source_type": raw.source_type, "is_demonstration": raw.is_demonstration,
                "service": raw.source_service, "datasource": raw.datasource, "fetched_at": raw.fetched_at,
                "geometry_repaired": geo["repaired"]},
        notices=notices,
        from_cache=False,
    )


def resolve_by_id(raw_id: str, adapters) -> ParcelProfile:
    teryt_id = normalize_teryt_id(raw_id)
    cached = parcel_cache.get(adapters.mode, teryt_id)
    if cached is not None:
        return cached
    profile = build_parcel_profile(adapters.parcel_by_id(teryt_id))
    parcel_cache.put(adapters.mode, profile)
    return profile


def resolve_by_point(lon: float, lat: float, adapters) -> ParcelProfile:
    lon_f, lat_f = validate_lon_lat(lon, lat)
    raw = adapters.parcel_by_xy(lon_f, lat_f)
    cached = parcel_cache.get(adapters.mode, raw.teryt_id)
    if cached is not None:
        return cached
    profile = build_parcel_profile(raw)
    parcel_cache.put(adapters.mode, profile)
    return profile
