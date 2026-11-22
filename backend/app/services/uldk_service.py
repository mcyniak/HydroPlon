"""Etap 1, moduł 1.1 — identyfikatory TERYT i parser odpowiedzi GUGiK ULDK.

Korekty względem szkicu kolegi (rejestr R09–R11, I05–I07):
  * ścisła składnia identyfikatora (zamiast ``[\\w/-]+``),
  * dokładny status ``0`` (zamiast ``startswith('0')``),
  * dokładna liczba kolumn równa liczbie pól ``result``,
  * prefiks ``SRID=`` odcinany tylko po sprawdzeniu, że to 2180,
  * kolejność ``lon,lat,4326`` w GetParcelByXY,
  * rozróżnienie: błąd wejścia / brak działki / timeout / błąd usługi / zły format.

Pełna odpowiedź usługi nie trafia do komunikatów dla rolnika ani do logów.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime

import httpx

from app.config import TZ, settings
from app.services.errors import (
    AmbiguousResultError,
    InputValidationError,
    NotFoundError,
    SourceError,
    SourceFormatError,
    SourceRateLimitedError,
    SourceTimeoutError,
)

log = logging.getLogger("hydroplon.uldk")

ULDK_RESULT_FIELDS = ("geom_wkt", "teryt", "voivodeship", "county", "commune", "region", "parcel", "datasource")

# WWPPGG_R.OOOO.[AR_N.]NR[/NR] — województwo, powiat, gmina, rodzaj gminy, obręb, opcjonalny arkusz, numer.
TERYT_PATTERN = re.compile(
    r"^(?P<woj>\d{2})(?P<pow>\d{2})(?P<gmi>\d{2})_(?P<rodz>\d)\."
    r"(?P<obr>\d{4})\."
    r"(?:AR_(?P<ark>\d{1,4})\.)?"
    r"(?P<nr>\d{1,6}(?:/\d{1,6})?)$"
)

# Wstępna obwiednia Polski (z marginesem). Najdalej na północ wysunięty punkt kraju leży
# ok. 54°50'N, więc 55,1°N nie wymaga dalszego poszerzania (rejestr R09).
# Ostateczną weryfikację położenia daje ULDK.
POLAND_BBOX = {"lon_min": 13.9, "lon_max": 24.3, "lat_min": 48.9, "lat_max": 55.1}


def normalize_teryt_id(raw_id: str) -> str:
    if raw_id is None:
        raise InputValidationError("Nie podano identyfikatora działki.")
    cleaned = "".join(str(raw_id).split())  # usunięcie wszystkich białych znaków
    cleaned = re.sub(r"(?i)\.ar_", ".AR_", cleaned)
    if len(cleaned) > 40 or not TERYT_PATTERN.match(cleaned):
        raise InputValidationError(
            "Nieprawidłowy format identyfikatora działki. Oczekiwany format: "
            "WWPPGG_R.OOOO.NR lub WWPPGG_R.OOOO.AR_N.NR, np. 040301_1.0005.124/2.",
            details={"received_length": len(cleaned)})
    return cleaned


def validate_lon_lat(lon: float, lat: float) -> tuple[float, float]:
    try:
        lon_f, lat_f = float(lon), float(lat)
    except (TypeError, ValueError) as exc:
        raise InputValidationError("Współrzędne muszą być liczbami.") from exc
    if not (POLAND_BBOX["lon_min"] <= lon_f <= POLAND_BBOX["lon_max"]
            and POLAND_BBOX["lat_min"] <= lat_f <= POLAND_BBOX["lat_max"]):
        hint = ""
        if (POLAND_BBOX["lon_min"] <= lat_f <= POLAND_BBOX["lon_max"]
                and POLAND_BBOX["lat_min"] <= lon_f <= POLAND_BBOX["lat_max"]):
            hint = " Wygląda na zamienioną kolejność: oczekiwane lon (długość) i lat (szerokość)."
        raise InputValidationError("Współrzędne znajdują się poza obszarem Polski." + hint)
    return lon_f, lat_f


@dataclass(frozen=True)
class RawUldkParcel:
    status: str
    teryt_id: str
    raw_wkt_2180: str
    administrative_location: dict
    datasource: str | None
    source_service: str
    source_type: str
    is_demonstration: bool
    fetched_at: str
    notes: tuple[str, ...] = ()


def parse_uldk_response(raw_text: str, *, source_type: str, is_demonstration: bool,
                        expected_fields: tuple[str, ...] = ULDK_RESULT_FIELDS) -> RawUldkParcel:
    """Parsuje tekstową odpowiedź ULDK: wiersz statusu, potem wiersze danych rozdzielone '|'."""
    if raw_text is None:
        raise SourceFormatError("Pusta odpowiedź usługi ULDK.")
    lines = [ln.strip() for ln in str(raw_text).strip().splitlines() if ln.strip()]
    if not lines:
        raise SourceFormatError("Pusta odpowiedź usługi ULDK.")
    status_line = lines[0]
    status_code = status_line.split()[0] if status_line.split() else ""
    if status_code == "-1":
        raise NotFoundError("Nie znaleziono działki ewidencyjnej w usłudze ULDK.")
    if status_code != "0":
        raise SourceFormatError("Nieoczekiwany status odpowiedzi ULDK.", details={"status_token": status_code[:8]})
    data = lines[1:]
    if not data:
        raise SourceFormatError("Odpowiedź ULDK ze statusem 0 nie zawiera danych działki.")
    if len(data) > 1:
        raise AmbiguousResultError("Usługa ULDK zwróciła więcej niż jedną działkę; wskaż działkę jednoznacznie.",
                                   details={"count": len(data)})
    parts = data[0].split("|")
    if len(parts) != len(expected_fields):
        raise SourceFormatError("Nieoczekiwana liczba pól w odpowiedzi ULDK (zmiana schematu usługi?).",
                                details={"expected": len(expected_fields), "received": len(parts)})
    rec = dict(zip(expected_fields, (p.strip() for p in parts)))
    geom = rec["geom_wkt"]
    if geom.upper().startswith("SRID="):
        prefix, _, rest = geom.partition(";")
        if prefix.upper() != "SRID=2180" or not rest:
            raise SourceFormatError("Geometria ULDK w nieoczekiwanym układzie współrzędnych.",
                                    details={"srid_prefix": prefix[:16]})
        geom = rest
    if not re.match(r"^(MULTI)?POLYGON\s*\(", geom.upper()):
        raise SourceFormatError("Odpowiedź ULDK nie zawiera geometrii poligonowej WKT.")
    teryt = rec["teryt"]
    if not TERYT_PATTERN.match(teryt):
        raise SourceFormatError("Identyfikator działki w odpowiedzi ULDK ma nieoczekiwany format.")
    return RawUldkParcel(
        status="ok",
        teryt_id=teryt,
        raw_wkt_2180=geom,
        administrative_location={
            "voivodeship": rec["voivodeship"] or None,
            "county": rec["county"] or None,
            "commune": rec["commune"] or None,
            "region": rec["region"] or None,
            "parcel_number": rec["parcel"] or None,
        },
        datasource=rec["datasource"] or None,
        source_service="GUGiK ULDK" if source_type == "live" else "ULDK (fixture offline — dane syntetyczne)",
        source_type=source_type,
        is_demonstration=is_demonstration,
        fetched_at=datetime.now(TZ).isoformat(timespec="seconds"),
    )


class LiveUldkClient:
    """Klient HTTP ULDK. Transport można wstrzyknąć (testy z httpx.MockTransport)."""

    def __init__(self, base_url: str | None = None, timeout_s: float | None = None,
                 max_retries: int | None = None, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url or settings.uldk_url
        self.timeout_s = timeout_s or settings.uldk_timeout_s
        self.max_retries = settings.uldk_max_retries if max_retries is None else max_retries
        self._transport = transport

    def _request(self, params: dict) -> str:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with httpx.Client(timeout=self.timeout_s, transport=self._transport,
                                  headers={"User-Agent": "HydroPlon-prototype/0.1"}) as client:
                    resp = client.get(self.base_url, params=params)
                if resp.status_code >= 500:
                    last_exc = SourceError("Usługa ULDK zwróciła błąd serwera.",
                                           details={"http_status": resp.status_code})
                    time.sleep(0.2 * (attempt + 1))
                    continue
                if 300 <= resp.status_code < 400:
                    # Nie podążamy za przekierowaniem: w teście 2026-11-21 ULDK kierowało klienta automatycznego
                    # do adresu zwracającego pozorną odpowiedź (status "0" + niepoprawna geometria).
                    location = resp.headers.get("location", "")
                    if "/resp/" in location:
                        # Zgodnie z dokumentacją ULDK (opis.html) limity zapytań na IP: 200/min (6–18), 40/min,
                        # 10 000/dobę; zablokowany klient jest przekierowywany na adres z pozorną odpowiedzią.
                        raise SourceRateLimitedError(
                            "Usługa ULDK odrzuciła zapytanie mechanizmem limitu zapytań dla tego adresu IP "
                            "(przekierowanie na stronę ochrony). Możliwe zgłoszenie IP na białą listę GUGiK.",
                            details={"http_status": resp.status_code,
                                     "redirect_host": httpx.URL(location).host})
                    raise SourceError("Usługa ULDK przekierowała zapytanie (możliwe ograniczenie dostępu dla tego "
                                      "klienta/sieci); wynik nie został użyty.",
                                      details={"http_status": resp.status_code,
                                               "redirect_host": httpx.URL(location).host if location else None})
                if resp.status_code != 200:
                    raise SourceError("Usługa ULDK odrzuciła zapytanie.", details={"http_status": resp.status_code})
                return resp.text
            except httpx.TimeoutException as exc:
                last_exc = SourceTimeoutError("Przekroczono czas oczekiwania na usługę ULDK.")
                log.warning("ULDK timeout (attempt %s)", attempt + 1)
            except httpx.HTTPError as exc:
                last_exc = SourceError("Brak połączenia z usługą ULDK.", details={"error_type": type(exc).__name__})
        assert last_exc is not None
        raise last_exc

    def fetch_by_id(self, teryt_id: str) -> RawUldkParcel:
        text = self._request({"request": "GetParcelById", "id": teryt_id,
                              "result": ",".join(ULDK_RESULT_FIELDS), "srid": "2180"})
        return parse_uldk_response(text, source_type="live", is_demonstration=False)

    def fetch_by_xy(self, lon: float, lat: float) -> RawUldkParcel:
        # Kolejność bezwzględnie: LON,LAT,4326.
        text = self._request({"request": "GetParcelByXY", "xy": f"{lon:.7f},{lat:.7f},4326",
                              "result": ",".join(ULDK_RESULT_FIELDS), "srid": "2180"})
        return parse_uldk_response(text, source_type="live", is_demonstration=False)
