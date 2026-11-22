"""Wymienne źródła obserwacji Sentinel-2 (SatelliteProvider) i łańcuch z kontrolowanym fallbackiem.

Każdy dostawca zwraca listę ``SceneIndices`` (wspólny, walidowany model). Reguły okna 45 dni, progu ≥70%
czystych pikseli, baseline ±15 dni i flagi anomalii stosuje ``satellite_service`` — niezależnie od dostawcy.

Dostawcy (decyzje: docs/DATA_PROVIDER_COMPARISON.md):
  * ``earth_search_c1`` (domyślny) — Earth Search STAC + COG Sentinel-2 Collection 1 L2A (AWS Open Data,
    bez konta i bez klucza). Optymalizacje: odczyt najpierw SCL (bramka zachmurzenia), dopiero potem
    B04/B08/B11 dla sceny, która przeszła; sceny i lata odniesienia równolegle; HTTP/2 + nagłówek COG
    w jednym żądaniu; cache wyników scen.
  * ``cdse_statistical`` (OPCJONALNY) — Copernicus Data Space Ecosystem Statistical API. Wymaga własnego
    konta i klienta OAuth (``CDSE_CLIENT_ID`` / ``CDSE_CLIENT_SECRET`` w środowisku użytkownika — nigdy
    w repozytorium). Darmowy limit: 10 000 żądań i 10 000 PU / miesiąc na konto; po przekroczeniu HTTP 429
    (blokada, nie opłata). Bez zmiennych środowiskowych adapter jest pomijany.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol

import httpx
import numpy as np
from pydantic import BaseModel, Field

from app.services import cache
from app.services.errors import SourceError, SourceRateLimitedError, SourceTimeoutError

CLEAR_SCL = (4, 5, 6, 7)
ALGO_VERSION = "s2-indices-3"


class SceneIndices(BaseModel):
    acquisition_date: str
    scene_id: str
    provider: str
    processing_version: str | None = None
    valid_parcel_pixels_pct: float = Field(ge=0, le=100)
    ndvi_median: float | None = Field(default=None, ge=-1, le=1)
    ndvi_iqr: float | None = None
    ndmi_median: float | None = Field(default=None, ge=-1, le=1)
    ndmi_iqr: float | None = None
    scene_cloud_cover_pct: float | None = None
    pixel_count_inside: int = 0
    uncertainty_pl: str = "Mediana pikseli działki; IQR opisuje zróżnicowanie wewnątrz działki."


@dataclass
class Query:
    lon: float
    lat: float
    wkt_2180: str
    as_of: date
    window_days: int = 45
    baseline_days: int = 15
    baseline_years: int = 3
    min_valid: float = 70.0


class SatelliteProvider(Protocol):
    name: str
    requires_auth: bool

    def available(self) -> tuple[bool, str]: ...
    def scenes(self, q: Query, deadline: float) -> tuple[list[SceneIndices], list[str]]: ...


def _geom_key(wkt: str) -> str:
    return hashlib.sha256(wkt.encode()).hexdigest()[:16]


# ------------------------------------------------------------------------------------------- Earth Search
class EarthSearchCOGProvider:
    name = "earth_search_c1"
    requires_auth = False
    STAC = "https://earth-search.aws.element84.com/v1/search"
    GDAL_ENV = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", AWS_NO_SIGN_REQUEST="YES", GDAL_HTTP_TIMEOUT="10",
                    GDAL_HTTP_MAX_RETRY="2", GDAL_HTTP_RETRY_DELAY="0.5", GDAL_HTTP_MULTIPLEX="YES",
                    GDAL_HTTP_VERSION="2", GDAL_INGESTED_BYTES_AT_OPEN="32768",
                    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif", VSI_CACHE="TRUE")

    # Collection 1 ma spójne przetworzenie, ale (pomiar 22.11.2026) luki: punkt 17,95°E 52,70°N w 2022 r. — 12 scen
    # w C1 wobec 146 w sentinel-2-l2a. Dlatego obie kolekcje są przeszukiwane równolegle; dla danej daty wygrywa C1.
    COLLECTIONS = ("sentinel-2-c1-l2a", "sentinel-2-l2a")

    def __init__(self, collection: str | None = None, max_workers: int = 8):
        self.collections = (collection,) if collection else self.COLLECTIONS
        self.collection = self.collections[0]
        self.max_workers = max_workers
        self._http = httpx.Client(timeout=12, headers={"User-Agent": "HydroPlon-prototype/0.1"})

    def available(self) -> tuple[bool, str]:
        return True, "publiczny katalog STAC i COG (AWS Open Data), bez konta"

    # --- katalog
    def search(self, lon: float, lat: float, start: date, end: date, max_cloud: float) -> list[dict]:
        with ThreadPoolExecutor(max_workers=len(self.collections)) as pool:
            parts = list(pool.map(lambda c: self._search_one(c, lon, lat, start, end, max_cloud), self.collections))
        best: dict[str, dict] = {}
        for rank, items in enumerate(parts):          # kolejność kolekcji = priorytet
            for f in items:
                d = f["properties"]["datetime"][:10]
                if d not in best:
                    best[d] = {**f, "collection": self.collections[rank]}
        return sorted(best.values(), key=lambda f: f["properties"]["datetime"], reverse=True)

    def _search_one(self, collection: str, lon: float, lat: float, start: date, end: date, max_cloud: float) -> list[dict]:
        key = f"{collection}|{lon:.4f},{lat:.4f}|{start}|{end}|{max_cloud}"
        hit = cache.get("s2search", key, "1")
        if hit is not None and end < date.today() - timedelta(days=7):  # dla świeżych dat katalog może się zmienić
            return hit
        body = {"collections": [collection], "intersects": {"type": "Point", "coordinates": [lon, lat]},
                "datetime": f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z", "limit": 100,
                "query": {"eo:cloud_cover": {"lt": max_cloud}}, "fields": {"include": ["assets", "properties"]}}
        try:
            r = self._http.post(self.STAC, json=body)
        except httpx.TimeoutException as exc:
            raise SourceTimeoutError("Przekroczono czas oczekiwania na katalog STAC.") from exc
        except httpx.HTTPError as exc:
            raise SourceError("Brak połączenia z katalogiem STAC.") from exc
        if r.status_code == 429:
            raise SourceRateLimitedError("Katalog STAC ograniczył liczbę zapytań (429).")
        if r.status_code != 200:
            raise SourceError("Katalog STAC odrzucił zapytanie.", details={"http_status": r.status_code})
        best: dict[str, dict] = {}
        for f in r.json().get("features", []):
            d = f["properties"]["datetime"][:10]
            if date.fromisoformat(d) > end:
                continue  # izolacja czasu niezależna od filtra katalogu
            slim = {"id": f["id"], "properties": {k: f["properties"].get(k) for k in
                                                  ("datetime", "eo:cloud_cover", "s2:processing_baseline")},
                    "assets": {b: {"href": f["assets"][b]["href"], "raster:bands": f["assets"][b].get("raster:bands")}
                               for b in ("red", "nir", "swir16", "scl") if b in f["assets"]}}
            if len(slim["assets"]) < 4:
                continue
            if d not in best or (slim["properties"]["eo:cloud_cover"] or 100) < (best[d]["properties"]["eo:cloud_cover"] or 100):
                best[d] = slim
        out = sorted(best.values(), key=lambda f: f["properties"]["datetime"], reverse=True)
        cache.put("s2search", key, "1", out)
        return out

    # --- odczyt rastrów
    def _grid(self, feature: dict, wkt: str):
        import rasterio
        import shapely
        from pyproj import Transformer
        from rasterio.features import geometry_mask
        from rasterio.windows import from_bounds
        from shapely import wkt as shp_wkt
        with rasterio.Env(**self.GDAL_ENV), rasterio.open(feature["assets"]["red"]["href"]) as ref:
            t = Transformer.from_crs("EPSG:2180", ref.crs, always_xy=True)
            g = shapely.transform(shp_wkt.loads(wkt), lambda x, y: t.transform(x, y), interleaved=False)
            minx, miny, maxx, maxy = g.bounds
            win = from_bounds(minx - 10, miny - 10, maxx + 10, maxy + 10, ref.transform).round_offsets().round_lengths()
            shape = (max(1, int(win.height)), max(1, int(win.width)))
            bounds = rasterio.windows.bounds(win, ref.transform)
            inside = ~geometry_mask([g.__geo_interface__], out_shape=shape, transform=ref.window_transform(win))
        return shape, bounds, inside

    def _read(self, href: str, bounds, shape) -> np.ndarray:
        import rasterio
        from rasterio.enums import Resampling
        from rasterio.windows import from_bounds
        last = None
        for _ in range(2):
            try:
                with rasterio.Env(**self.GDAL_ENV), rasterio.open(href) as src:
                    return src.read(1, window=from_bounds(*bounds, src.transform), out_shape=shape,
                                    resampling=Resampling.nearest, boundless=True, fill_value=0)
            except rasterio.errors.RasterioIOError as exc:
                last = exc
        raise last

    def _scene(self, f: dict, wkt: str, min_valid: float, pool: ThreadPoolExecutor) -> SceneIndices:
        key = f"{f['id']}|{_geom_key(wkt)}"
        hit = cache.get("s2scene", key, ALGO_VERSION)
        if hit:
            return SceneIndices(**hit)
        shape, bounds, inside = self._grid(f, wkt)
        n_in = int(inside.sum())
        coll = "c1" if f.get("collection", self.collection) == "sentinel-2-c1-l2a" else "l2a"
        base = dict(acquisition_date=f["properties"]["datetime"][:10], scene_id=f["id"], provider=self.name,
                    processing_version=f"{coll}:{f['properties'].get('s2:processing_baseline')}",
                    scene_cloud_cover_pct=f["properties"].get("eo:cloud_cover"), pixel_count_inside=n_in)
        scl = self._read(f["assets"]["scl"]["href"], bounds, shape)  # bramka zachmurzenia: tylko SCL (20 m)
        clear_scl = inside & np.isin(scl, CLEAR_SCL)
        pct_scl = 100.0 * clear_scl.sum() / n_in if n_in else 0.0
        if n_in == 0 or pct_scl < min_valid:
            res = SceneIndices(**base, valid_parcel_pixels_pct=round(pct_scl, 1))
            cache.put("s2scene", key, ALGO_VERSION, res.model_dump())
            return res
        bands = dict(zip(("red", "nir", "swir16"),
                         pool.map(lambda b: self._read(f["assets"][b]["href"], bounds, shape), ("red", "nir", "swir16"))))
        refl = {}
        for b, arr in bands.items():
            rb = (f["assets"][b].get("raster:bands") or [{}])[0]
            refl[b] = arr.astype(float) * float(rb.get("scale", 1.0) or 1.0) + float(rb.get("offset", 0.0) or 0.0)
        # Kontrola spójności offsetu (dotyczy kolekcji sentinel-2-l2a, R34): offset z metadanych dający ujemną
        # medianę reflektancji czerwieni na działce oznacza dane już skorygowane — offset nie jest stosowany.
        rb_red = (f["assets"]["red"].get("raster:bands") or [{}])[0]
        if float(rb_red.get("offset", 0.0) or 0.0) != 0 and (bands["red"][clear_scl] > 0).any()                 and np.median(refl["red"][clear_scl & (bands["red"] > 0)]) < 0:
            for b, arr in bands.items():
                rb = (f["assets"][b].get("raster:bands") or [{}])[0]
                refl[b] = arr.astype(float) * float(rb.get("scale", 1.0) or 1.0)
            base["processing_version"] += "|offset_metadata_inconsistent_not_applied"
        clear = clear_scl & (bands["red"] > 0) & (refl["red"] > 0) & (refl["nir"] > 0) & (refl["swir16"] > 0)
        out = dict(base, valid_parcel_pixels_pct=round(100.0 * clear.sum() / n_in, 1))
        if clear.sum() >= 3:
            r_, n_, s_ = refl["red"][clear], refl["nir"][clear], refl["swir16"][clear]
            for name, v in (("ndvi", (n_ - r_) / (n_ + r_)), ("ndmi", (n_ - s_) / (n_ + s_))):
                v = v[np.isfinite(v) & (v >= -1) & (v <= 1)]
                if v.size:
                    q25, q50, q75 = np.percentile(v, [25, 50, 75])
                    out[f"{name}_median"], out[f"{name}_iqr"] = round(float(q50), 3), round(float(q75 - q25), 3)
        res = SceneIndices(**out)
        cache.put("s2scene", key, ALGO_VERSION, res.model_dump())
        return res

    def scenes(self, q: Query, deadline: float) -> tuple[list[SceneIndices], list[str]]:
        errors: list[str] = []
        pool = ThreadPoolExecutor(max_workers=self.max_workers)
        band_pool = ThreadPoolExecutor(max_workers=self.max_workers)

        def safe(f):
            try:
                return self._scene(f, q.wkt_2180, q.min_valid, band_pool)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{f['id']}: {type(exc).__name__}")
                return None
        try:
            cands = self.search(q.lon, q.lat, q.as_of - timedelta(days=q.window_days), q.as_of, 60)[:4]
            futs = [pool.submit(safe, f) for f in cands]  # 4 najnowsze kandydatki równolegle (bramka SCL)
            done, _ = wait(futs, timeout=max(0.5, deadline - time.monotonic()))
            got = [f.result() for f in futs if f in done and f.result()]
            out = list(got)
            latest = next((s for s in got if s.valid_parcel_pixels_pct >= q.min_valid and s.ndmi_median is not None),
                          None)
            if latest is not None:
                ld = date.fromisoformat(latest.acquisition_date)

                def year_best(year: int):
                    try:
                        c = ld.replace(year=year)
                    except ValueError:
                        c = ld.replace(year=year, day=28)
                    cs = self.search(q.lon, q.lat, c - timedelta(days=q.baseline_days),
                                     c + timedelta(days=q.baseline_days), 30)
                    cs.sort(key=lambda f: ((f["properties"]["eo:cloud_cover"] or 100),
                                           abs((date.fromisoformat(f["properties"]["datetime"][:10]) - c).days)))
                    for f in cs[:2]:
                        s = safe(f)
                        if s and s.valid_parcel_pixels_pct >= q.min_valid and s.ndmi_median is not None:
                            return s
                    return None
                years = list(range(ld.year - 1, max(2016, ld.year - q.baseline_years) - 1, -1))
                yf = [pool.submit(year_best, y) for y in years]
                done, not_done = wait(yf, timeout=max(0.5, deadline - time.monotonic()))
                out.extend(f.result() for f in done if f.result())
                if not_done:
                    errors.append(f"time_budget_baseline:{len(not_done)}")
            return out, errors
        finally:
            pool.shutdown(wait=False, cancel_futures=False)
            band_pool.shutdown(wait=False, cancel_futures=False)


# ------------------------------------------------------------------------------------------- CDSE Statistical
class CdseStatisticalProvider:
    """Opcjonalny: wymaga WŁASNEGO konta CDSE użytkownika (zmienne środowiskowe), nigdy sekretu w repozytorium."""
    name = "cdse_statistical"
    requires_auth = True
    TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
    STAT_URL = "https://sh.dataspace.copernicus.eu/api/v1/statistics"
    EVALSCRIPT = """//VERSION=3
function setup(){return{input:[{bands:["B04","B08","B11","SCL","dataMask"]}],
 output:[{id:"ndvi",bands:1,sampleType:"FLOAT32"},{id:"ndmi",bands:1,sampleType:"FLOAT32"},{id:"dataMask",bands:1}]};}
function evaluatePixel(s){var clear=[4,5,6,7].indexOf(s.SCL)>=0;
 return{ndvi:[(s.B08-s.B04)/(s.B08+s.B04)],ndmi:[(s.B08-s.B11)/(s.B08+s.B11)],dataMask:[s.dataMask&&clear?1:0]};}"""

    def __init__(self, transport: httpx.BaseTransport | None = None):
        self._transport = transport
        self._token: tuple[str, float] | None = None
        self._blocked_until = 0.0
        self._lock = threading.Lock()

    def available(self) -> tuple[bool, str]:
        if not (os.getenv("CDSE_CLIENT_ID") and os.getenv("CDSE_CLIENT_SECRET")):
            return False, "brak własnych danych OAuth CDSE w środowisku (adapter opcjonalny)"
        if time.monotonic() < self._blocked_until:
            return False, "limit zapytań/PU CDSE wyczerpany (429) — adapter wstrzymany"
        return True, "OAuth CDSE z konta użytkownika"

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=15, transport=self._transport)

    def _get_token(self) -> str:
        with self._lock:
            if self._token and self._token[1] > time.monotonic() + 30:
                return self._token[0]
            with self._client() as c:
                r = c.post(self.TOKEN_URL, data={"grant_type": "client_credentials",
                                                 "client_id": os.environ["CDSE_CLIENT_ID"],
                                                 "client_secret": os.environ["CDSE_CLIENT_SECRET"]})
            if r.status_code != 200:
                raise SourceError("Nie uzyskano tokenu CDSE.", details={"http_status": r.status_code})
            d = r.json()
            self._token = (d["access_token"], time.monotonic() + float(d.get("expires_in", 300)))
            return self._token[0]

    def scenes(self, q: Query, deadline: float) -> tuple[list[SceneIndices], list[str]]:
        from shapely import wkt as shp_wkt
        body = {"input": {"bounds": {"geometry": shp_wkt.loads(q.wkt_2180).__geo_interface__,
                                     "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/2180"}},
                          "data": [{"type": "sentinel-2-l2a", "dataFilter": {"maxCloudCoverage": 60}}]},
                "aggregation": {"timeRange": {"from": f"{q.as_of - timedelta(days=q.window_days)}T00:00:00Z",
                                              "to": f"{q.as_of}T23:59:59Z"},
                                "aggregationInterval": {"of": "P1D"}, "resx": 10, "resy": 10,
                                "evalscript": self.EVALSCRIPT},
                "calculations": {"default": {"statistics": {"default": {"percentiles": {"k": [25, 50, 75]}}}}}}
        with self._client() as c:
            r = c.post(self.STAT_URL, json=body, headers={"Authorization": f"Bearer {self._get_token()}"},
                       timeout=max(1.0, deadline - time.monotonic()))
        if r.status_code == 429:
            self._blocked_until = time.monotonic() + float(r.headers.get("Retry-After", 3600))
            raise SourceRateLimitedError("CDSE Statistical API: limit zapytań/PU (429).")
        if r.status_code != 200:
            raise SourceError("CDSE Statistical API odrzuciło zapytanie.", details={"http_status": r.status_code})
        out = []
        for it in r.json().get("data", []):
            o = it.get("outputs", {})
            st_v = o.get("ndvi", {}).get("bands", {}).get("B0", {}).get("stats", {})
            st_m = o.get("ndmi", {}).get("bands", {}).get("B0", {}).get("stats", {})
            if not st_v or not st_v.get("sampleCount"):
                continue
            n_all = st_v.get("sampleCount", 0)
            valid = 100.0 * (n_all - st_v.get("noDataCount", 0)) / n_all if n_all else 0.0
            p = lambda st, k: st.get("percentiles", {}).get(k)  # noqa: E731
            out.append(SceneIndices(acquisition_date=it["interval"]["from"][:10], scene_id=f"cdse:{it['interval']['from'][:10]}",
                                    provider=self.name, valid_parcel_pixels_pct=round(valid, 1),
                                    ndvi_median=p(st_v, "50.0"), ndvi_iqr=(p(st_v, "75.0") - p(st_v, "25.0"))
                                    if p(st_v, "75.0") is not None else None,
                                    ndmi_median=p(st_m, "50.0"), ndmi_iqr=(p(st_m, "75.0") - p(st_m, "25.0"))
                                    if p(st_m, "75.0") is not None else None, pixel_count_inside=n_all,
                                    uncertainty_pl="Statystyki CDSE (dataMask z SCL); baseline nie jest pobierany."))
        return out, []


# ------------------------------------------------------------------------------------------- łańcuch
DEFAULT_ORDER = ("earth_search_c1", "cdse_statistical")
PROVIDERS = {"earth_search_c1": EarthSearchCOGProvider, "cdse_statistical": CdseStatisticalProvider}
_instances: dict[str, SatelliteProvider] = {}


def get_provider(name: str) -> SatelliteProvider:
    if name not in _instances:
        _instances[name] = PROVIDERS[name]()
    return _instances[name]


def fetch_with_fallback(q: Query, budget_s: float = 20.0, order: tuple[str, ...] | None = None,
                        providers: list[SatelliteProvider] | None = None) -> dict:
    """Kolejni dostawcy tylko gdy poprzedni zawiódł, w ramach JEDNEGO budżetu czasu, bez ponawiania pętli."""
    order = order or tuple(x.strip() for x in os.getenv("HYDROPLON_SATELLITE_PROVIDERS", ",".join(DEFAULT_ORDER)).split(",")
                           if x.strip() in PROVIDERS)
    chain = providers or [get_provider(n) for n in order]
    deadline = time.monotonic() + budget_s
    attempts: list[dict] = []
    for p in chain:
        ok, why = p.available()
        if not ok:
            attempts.append({"provider": p.name, "status": "skipped", "reason": why})
            continue
        if time.monotonic() > deadline - 1:
            attempts.append({"provider": p.name, "status": "skipped", "reason": "budżet czasu wyczerpany"})
            break
        try:
            scenes, errors = p.scenes(q, deadline)
            attempts.append({"provider": p.name, "status": "ok", "scenes": len(scenes), "errors": errors})
            return {"provider": p.name, "scenes": [s.model_dump() for s in scenes], "attempts": attempts,
                    "errors": errors}
        except (SourceError, Exception) as exc:  # noqa: BLE001
            attempts.append({"provider": p.name, "status": "error",
                             "reason": getattr(exc, "error_code", type(exc).__name__)})
    return {"provider": None, "scenes": [], "attempts": attempts, "errors": []}
