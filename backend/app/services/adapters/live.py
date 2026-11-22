"""Adaptery trybu LIVE.

Stan prototypu:
  * ULDK — klient HTTP wdrożony (parser zgodny z dokumentacją GUGiK); weryfikacja
    dostępności tylko jawną sondą (`scripts/probe_live.py`).
  * SoilGrids, NMT WCS, BDOT10k WFS, ISOK WMS, IMGW, Sentinel-2 STAC, EDO —
    NIEWDROŻONE. Wywołanie zwraca AdapterNotImplementedError, a analiza oznacza
    moduł jako ``unavailable`` z powodem ``adapter_not_implemented``.
    Nigdy nie następuje ciche przełączenie na dane fixture.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

from app.services.adapters.base import AdapterInfo
from app.services.egib_wfs_service import EgibWfsClient
from app.services.errors import AdapterNotImplementedError, NotFoundError, SourceError, SourceRateLimitedError
from app.services.uldk_service import LiveUldkClient, RawUldkParcel

PARCEL_SOURCES = ("auto", "uldk", "wfs")
ULDK_BLOCK_COOLDOWN_S = 600.0

PLANNED_ENDPOINTS: dict[str, str] = {}


class LiveAdapters:
    mode = "live"

    def __init__(self, uldk_client: LiveUldkClient | None = None, wfs_client: EgibWfsClient | None = None,
                 parcel_source: str | None = None):
        # rasterio importowany współbieżnie z kilku wątków przy pierwszej analizie zgłaszał
        # "ImportError: cannot import name tastes_like_gdal" — import w wątku głównym przy starcie.
        import rasterio.features  # noqa: F401
        import rasterio.io  # noqa: F401
        import rasterio.transform  # noqa: F401
        import rasterio.windows  # noqa: F401
        self.uldk = uldk_client or LiveUldkClient()
        self.wfs = wfs_client or EgibWfsClient()
        self.parcel_source = (parcel_source or os.getenv("HYDROPLON_PARCEL_SOURCE", "auto")).strip().lower()
        if self.parcel_source not in PARCEL_SOURCES:
            raise ValueError(f"HYDROPLON_PARCEL_SOURCE musi być jednym z {PARCEL_SOURCES}")
        self._uldk_blocked_until = 0.0
        self._lock = threading.Lock()
        self.last_parcel_source: str | None = None

    def source_versions(self) -> dict[str, str]:
        from app.services.adapters import live_weather
        try:
            imgw = "imgw-klimat-" + live_weather._meta(str(live_weather.DB_PATH))["last_month"]
        except Exception:  # noqa: BLE001
            imgw = "imgw-not-imported"
        return {"parcels": "uldk+egib-wfs-live", "soil": "soilgrids-v2.0-wcs", "terrain": "gugik-nmt-wcs-grid1",
                "weather": imgw, "satellite": "earth-search-s2-c1-l2a", "hydrology": "bdot10k-wms+mzp-wfs",
                **{m: "not_implemented" for m in PLANNED_ENDPOINTS}}

    def info(self) -> list[AdapterInfo]:
        blocked = time.monotonic() < self._uldk_blocked_until
        out = [AdapterInfo("live_uldk", "parcels", "live", True, False,
                           "Klient GUGiK ULDK (GetParcelById/GetParcelByXY)."
                           + (" Obecnie pomijany: limit zapytań GUGiK dla tego IP." if blocked else ""),
                           "uldk-live"),
               AdapterInfo("live_egib_wfs", "parcels", "live", True, True,
                           "GUGiK WFS EGiB (usługa zbiorcza, ms:dzialki) — zweryfikowane 2026-11-21. "
                           f"Strategia źródła działek: {self.parcel_source}.", "egib-wfs-live")]
        out += [AdapterInfo("live_soil", "soil", "live", True, True, "ISRIC SoilGrids v2.0 WCS (punkt, 0–30 cm, 36 warstw równolegle, cache); REST opcjonalnie.",
                            "soilgrids-v2.0-wcs"),
                AdapterInfo("live_terrain", "terrain", "live", True, True,
                            "GUGiK NMT WCS (DTM_PL-EVRF2007-NH, 1 m), metryki liczone w granicach działki.",
                            "gugik-nmt-wcs-grid1"),
                AdapterInfo("live_weather", "weather", "live", True, True,
                            "IMGW-PIB archiwum dobowe stacji klimatologicznych (import scripts/import_imgw.py).",
                            "imgw-klimat"),
                AdapterInfo("live_satellite", "satellite", "live", True, True,
                            "Sentinel-2 L2A Collection 1: Earth Search STAC + COG (bez konta); opcjonalnie CDSE "
                            "Statistical API z własnym kontem użytkownika.", "earth-search-s2-c1-l2a"),
                AdapterInfo("live_hydrology", "hydrology", "live", True, True,
                            "GUGiK BDOT10k WMS (rowy, kanały, cieki, wody) + Wody Polskie MZP/MRP WFS (zagrożenie "
                            "powodziowe).", "bdot10k-wms+mzp-wfs")]
        for m, ep in PLANNED_ENDPOINTS.items():
            out.append(AdapterInfo(f"live_{m}", m, "live", False, False, f"NIEWDROŻONY. Planowane źródło: {ep}",
                                   "not_implemented"))
        return out

    def _resolve(self, uldk_call, wfs_call, *, wfs_on_not_found: bool = False) -> RawUldkParcel:
        """W trybie auto używa WFS EGiB przy awarii ULDK lub braku działki po kliknięciu mapy."""
        if self.parcel_source == "wfs":
            self.last_parcel_source = "wfs"
            return wfs_call(None)
        if self.parcel_source == "uldk" or time.monotonic() >= self._uldk_blocked_until:
            try:
                raw = uldk_call()
                self.last_parcel_source = "uldk"
                return raw
            except SourceRateLimitedError as exc:
                with self._lock:
                    self._uldk_blocked_until = time.monotonic() + ULDK_BLOCK_COOLDOWN_S
                if self.parcel_source == "uldk":
                    raise
                reason = exc.message_pl
            except NotFoundError:
                if self.parcel_source == "uldk" or not wfs_on_not_found:
                    raise
                self.last_parcel_source = "wfs"
                return wfs_call("ULDK nie zwróciło działki w tym punkcie. Sprawdzono WFS EGiB.")
            except SourceError as exc:
                if self.parcel_source == "uldk":
                    raise
                reason = exc.message_pl
        else:
            reason = "Usługa ULDK jest czasowo pomijana po wykryciu limitu zapytań dla tego adresu IP."
        self.last_parcel_source = "wfs"
        return wfs_call("ULDK niedostępne: " + reason)

    def parcel_by_id(self, teryt_id: str) -> RawUldkParcel:
        return self._resolve(lambda: self.uldk.fetch_by_id(teryt_id), lambda note: self.wfs.fetch_by_id(teryt_id, note))

    def parcel_by_xy(self, lon: float, lat: float) -> RawUldkParcel:
        return self._resolve(lambda: self.uldk.fetch_by_xy(lon, lat),
                             lambda note: self.wfs.fetch_by_xy(lon, lat, note), wfs_on_not_found=True)

    def _not_implemented(self, module: str) -> dict:
        raise AdapterNotImplementedError(
            f"Adapter live dla modułu '{module}' nie jest jeszcze wdrożony.",
            details={"module": module, "planned_source": PLANNED_ENDPOINTS[module]})

    def soil_raw(self, parcel_id: str, parcel: Any) -> dict:
        from app.services.adapters import live_soil
        c = parcel.coordinates
        res = live_soil.fetch_point(c.point_on_surface_lon, c.point_on_surface_lat)
        via = "WCS" if "access" in res else "REST"
        return {"source_type": "live", "is_demonstration": False, "version": f"soilgrids-v2.0-{via.lower()}",
                "source_label": f"ISRIC SoilGrids v2.0 ({via}, CC BY 4.0)" + (" — z cache" if res["from_cache"] else ""),
                **{k: res[k] for k in ("record", "units", "resolution_m")}}

    def terrain_raw(self, parcel_id: str, parcel: Any) -> dict:
        from app.services.adapters import live_terrain
        m = live_terrain.terrain_metrics(parcel.coordinates.wkt_2180)
        return {"source_type": "live", "is_demonstration": False, "version": "gugik-nmt-wcs-grid1",
                "record": m, "source_label": "GUGiK Numeryczny Model Terenu (WCS, PL-EVRF2007-NH)"
                + (" — z cache" if m.get("from_cache") else ""),
                "computed_from": f"siatka NMT {m['resolution_m']:.0f} m pobrana z WCS, maska poligonu działki"}

    def hydrology_raw(self, parcel_id: str, parcel: Any) -> dict:
        from app.services.adapters import live_hydrology
        return live_hydrology.hydrology_raw(parcel.coordinates.wkt_2180)

    def weather_raw(self, parcel_id: str, parcel: Any, as_of=None) -> dict:
        from datetime import date as _date
        from app.services.adapters import live_weather
        c = parcel.coordinates
        return live_weather.weather_raw(c.point_on_surface_lon, c.point_on_surface_lat, as_of or _date.today())

    def satellite_raw(self, parcel_id: str, parcel: Any, as_of=None) -> dict:
        from datetime import date as _date
        from app.services.adapters import satellite_providers as sp
        c = parcel.coordinates
        res = sp.fetch_with_fallback(sp.Query(c.point_on_surface_lon, c.point_on_surface_lat, c.wkt_2180,
                                              as_of or _date.today()), budget_s=20.0)
        label = {"earth_search_c1": "Sentinel-2 L2A Collection 1 (Earth Search STAC + COG, AWS Open Data)",
                 "cdse_statistical": "Sentinel-2 L2A (Copernicus Data Space, Statistical API — konto użytkownika)"}
        return {"source_type": "live", "is_demonstration": False,
                "version": f"{res['provider'] or 'none'}|{sp.ALGO_VERSION}",
                "record": {"status": "ok" if res["scenes"] else "no_data", "scenes": res["scenes"],
                           "provider_attempts": res["attempts"], "read_errors": res["errors"]},
                "edo_background": None, "source_label": label.get(res["provider"], "Sentinel-2 — brak dostawcy")}


def get_adapters(mode: str):
    from app.services.adapters.fixture import FixtureAdapters
    if mode == "fixture":
        return FixtureAdapters()
    if mode == "live":
        return LiveAdapters()
    raise ValueError(f"Nieznany tryb: {mode}")
