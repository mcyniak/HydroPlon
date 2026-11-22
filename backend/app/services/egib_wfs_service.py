"""Etap 1 — drugie oficjalne źródło granic działek: GUGiK WFS EGiB (usługa zbiorcza).

Dokumentacja ULDK (https://uldk.gugik.gov.pl/opis.html) wskazuje tę usługę jako źródło danych EGiB w skali
kraju: https://mapy.geoportal.gov.pl/wss/service/PZGIK/EGIB/WFS/UslugaZbiorcza (warstwa ``ms:dzialki``).
Zweryfikowano 2026-11-21: GetCapabilities, GetFeature z BBOX i z filtrem ID_DZIALKI, wynik GeoJSON.

Pułapka osi (sprawdzona empirycznie): przy krótkim zapisie ``EPSG:2180`` serwer interpretuje BBOX jako
(północ, wschód) i potrafi zwrócić działkę z innego miejsca Polski. Dlatego:
  * BBOX zawsze w formie ``urn:ogc:def:crs:EPSG::2180`` z kolejnością (N, E),
  * każdy wynik jest weryfikowany: geometria musi zawierać punkt zapytania (z ewentualną zamianą osi),
  * dla wyszukiwania po ID orientacja jest potwierdzana drugim zapytaniem punktowym.
"""
from __future__ import annotations

import logging
from datetime import datetime

import httpx
import shapely
from shapely import wkt as shp_wkt
from shapely.geometry import MultiPolygon, Point, Polygon, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from app.config import TZ
from app.services.errors import (
    AmbiguousResultError,
    NotFoundError,
    SourceError,
    SourceFormatError,
    SourceTimeoutError,
)
from app.services.uldk_service import RawUldkParcel, TERYT_PATTERN

log = logging.getLogger("hydroplon.egib_wfs")
WFS_URL = "https://mapy.geoportal.gov.pl/wss/service/PZGIK/EGIB/WFS/UslugaZbiorcza"
CRS_URN = "urn:ogc:def:crs:EPSG::2180"
SERVICE_LABEL = "GUGiK WFS EGiB (usługa zbiorcza)"

# Oficjalne kody województw TERYT (dwie pierwsze cyfry identyfikatora działki).
VOIVODESHIPS = {"02": "dolnośląskie", "04": "kujawsko-pomorskie", "06": "lubelskie", "08": "lubuskie",
                "10": "łódzkie", "12": "małopolskie", "14": "mazowieckie", "16": "opolskie", "18": "podkarpackie",
                "20": "podlaskie", "22": "pomorskie", "24": "śląskie", "26": "świętokrzyskie",
                "28": "warmińsko-mazurskie", "30": "wielkopolskie", "32": "zachodniopomorskie"}


def _swap(geom: BaseGeometry) -> BaseGeometry:
    return shapely.transform(geom, lambda x, y: (y, x), interleaved=False)


class EgibWfsClient:
    def __init__(self, url: str = WFS_URL, timeout_s: float = 15.0, max_retries: int = 1,
                 transport: httpx.BaseTransport | None = None):
        self.url, self.timeout_s, self.max_retries, self._transport = url, timeout_s, max_retries, transport

    # ------------------------------------------------------------------ HTTP
    def _get_features(self, extra: dict) -> list[dict]:
        params = {"SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature", "TYPENAMES": "ms:dzialki",
                  "COUNT": "10", "OUTPUTFORMAT": "application/json", **extra}
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with httpx.Client(timeout=self.timeout_s, transport=self._transport,
                                  headers={"User-Agent": "HydroPlon-prototype/0.1"}) as c:
                    resp = c.get(self.url, params=params)
            except httpx.TimeoutException:
                last = SourceTimeoutError("Przekroczono czas oczekiwania na usługę WFS EGiB.")
                continue
            except httpx.HTTPError as exc:
                last = SourceError("Brak połączenia z usługą WFS EGiB.", details={"error_type": type(exc).__name__})
                continue
            if resp.status_code >= 500:
                last = SourceError("Usługa WFS EGiB zwróciła błąd serwera.", details={"http_status": resp.status_code})
                continue
            if resp.status_code != 200:
                raise SourceError("Usługa WFS EGiB odrzuciła zapytanie.", details={"http_status": resp.status_code})
            try:
                data = resp.json()
            except ValueError as exc:  # np. wyjątek OGC w XML
                raise SourceFormatError("Usługa WFS EGiB zwróciła odpowiedź inną niż GeoJSON.") from exc
            if not isinstance(data, dict) or not isinstance(data.get("features"), list):
                raise SourceFormatError("Nieoczekiwana struktura odpowiedzi WFS EGiB.")
            return data["features"]
        assert last is not None
        raise last

    def _features_at(self, pt: Point) -> list[dict]:
        e, n = pt.x, pt.y
        d = 0.5
        return self._get_features({"BBOX": f"{n - d},{e - d},{n + d},{e + d},{CRS_URN}", "SRSNAME": CRS_URN})

    # ------------------------------------------------------------------ parsowanie
    @staticmethod
    def _geom(feature: dict) -> BaseGeometry:
        try:
            g = shape(feature["geometry"])
        except Exception as exc:  # noqa: BLE001
            raise SourceFormatError("Niepoprawna geometria w odpowiedzi WFS EGiB.") from exc
        if not isinstance(g, (Polygon, MultiPolygon)) or g.is_empty:
            raise SourceFormatError("Odpowiedź WFS EGiB nie zawiera geometrii poligonowej.")
        return g

    @staticmethod
    def _oriented_covering(geom: BaseGeometry, pt: Point) -> BaseGeometry | None:
        if geom.buffer(0.01).covers(pt):
            return geom
        swapped = _swap(geom)
        if swapped.buffer(0.01).covers(pt):
            return swapped
        return None

    def _to_raw(self, parcel_id: str, props: dict, geom: BaseGeometry, note: str | None = None) -> RawUldkParcel:
        if not TERYT_PATTERN.match(parcel_id):
            raise SourceFormatError("Identyfikator działki w odpowiedzi WFS EGiB ma nieoczekiwany format.")
        notes = (f"Granice działki pobrano z usługi {SERVICE_LABEL}"
                 + (f" (stan danych: {props.get('DATA')})" if props.get("DATA") else "") + ".",)
        if note:
            notes = (note,) + notes
        return RawUldkParcel(
            status="ok", teryt_id=parcel_id, raw_wkt_2180=geom.wkt,
            administrative_location={"voivodeship": VOIVODESHIPS.get(parcel_id[:2]), "county": None,
                                     "commune": props.get("NAZWA_GMINY") or None,
                                     "region": props.get("NAZWA_OBREBU") or None,
                                     "parcel_number": props.get("NUMER_DZIALKI") or None},
            datasource=f"EGiB (WFS, stan {props.get('DATA')})" if props.get("DATA") else "EGiB (WFS)",
            source_service=SERVICE_LABEL, source_type="live", is_demonstration=False,
            fetched_at=datetime.now(TZ).isoformat(timespec="seconds"), notes=notes)

    # ------------------------------------------------------------------ API
    def fetch_by_xy_2180(self, pt: Point, note: str | None = None) -> RawUldkParcel:
        hits: dict[str, tuple[dict, list[BaseGeometry]]] = {}
        for f in self._features_at(pt):
            props = f.get("properties") or {}
            pid = str(props.get("ID_DZIALKI") or "").strip()
            g = self._oriented_covering(self._geom(f), pt)
            if pid and g is not None:
                hits.setdefault(pid, (props, []))[1].append(g)
        if not hits:
            raise NotFoundError("W wskazanym miejscu nie znaleziono działki ewidencyjnej (WFS EGiB).")
        if len(hits) > 1:
            raise AmbiguousResultError("Punkt leży na granicy kilku działek; wskaż działkę dokładniej.",
                                       details={"candidates": sorted(hits)})
        pid, (props, geoms) = next(iter(hits.items()))
        return self._to_raw(pid, props, unary_union(geoms) if len(geoms) > 1 else geoms[0], note)

    def fetch_by_xy(self, lon: float, lat: float, note: str | None = None) -> RawUldkParcel:
        from app.services.geometry_service import lonlat_to_2180
        return self.fetch_by_xy_2180(lonlat_to_2180(lon, lat), note)

    def fetch_by_id(self, teryt_id: str, note: str | None = None) -> RawUldkParcel:
        flt = ('<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0"><fes:PropertyIsEqualTo>'
               f'<fes:ValueReference>ID_DZIALKI</fes:ValueReference><fes:Literal>{teryt_id}</fes:Literal>'
               '</fes:PropertyIsEqualTo></fes:Filter>')
        feats = [f for f in self._get_features({"FILTER": flt, "SRSNAME": CRS_URN})
                 if str((f.get("properties") or {}).get("ID_DZIALKI", "")).strip() == teryt_id]
        if not feats:
            raise NotFoundError("Nie znaleziono działki o tym identyfikatorze (WFS EGiB).")
        geoms = [self._geom(f) for f in feats]
        geom = unary_union(geoms) if len(geoms) > 1 else geoms[0]
        # Potwierdzenie orientacji osi: punkt wewnątrz działki musi zwrócić TĘ SAMĄ działkę.
        for candidate in (geom, _swap(geom)):
            probe = candidate.representative_point()
            try:
                raw = self.fetch_by_xy_2180(probe, note)
            except (NotFoundError, AmbiguousResultError):
                continue
            if raw.teryt_id == teryt_id:
                return raw
        raise SourceFormatError("Nie udało się potwierdzić położenia geometrii z WFS EGiB (kolejność osi).")


__all__ = ["EgibWfsClient", "WFS_URL", "shp_wkt"]
