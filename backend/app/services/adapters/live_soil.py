"""LIVE — gleba z ISRIC SoilGrids v2.0 (REST properties/query), licencja CC BY 4.0.

Jedno zapytanie zwraca sand/silt/clay/soc/bdod/cfvo dla 0–5, 5–15, 15–30 cm (mean, Q0.05, Q0.95)
w jednostkach "mapped" (np. g/kg, dg/kg, cg/cm³) — przeliczenie wykonuje soil_service.
Odpowiedź trwa ~10–12 s, dlatego wynik jest zapisywany w trwałym cache (klucz: punkt zaokrąglony do
0,001°, ok. 100 m — mniej niż piksel SoilGrids 250 m).
"""
from __future__ import annotations

import threading

import httpx

from app.config import APP_DIR
from app.services import cache as hp_cache
from app.services.errors import SourceError, SourceFormatError, SourceTimeoutError

URL = "https://rest.isric.org/soilgrids/v2.0/properties/query"
CACHE = APP_DIR / "data_cache" / "soilgrids_cache.json"
PROPS = ("sand", "silt", "clay", "soc", "bdod", "cfvo")
DEPTHS = ("0-5cm", "5-15cm", "15-30cm")
_lock = threading.Lock()
CACHE_VERSION = "soilgrids-v2.0-rest|1"


def _norm_unit(u: str) -> str:
    return (u or "").replace("³", "3").strip()


def fetch_point(lon: float, lat: float, timeout_s: float = 30.0, transport=None) -> dict:
    """Źródło zależne od HYDROPLON_SOIL_SOURCE: 'wcs' (domyślnie, ~2–5 s) lub 'rest' (~20 s)."""
    import os
    if transport is None and os.getenv("HYDROPLON_SOIL_SOURCE", "wcs").lower() == "wcs":
        return fetch_point_wcs(lon, lat)
    return fetch_point_rest(lon, lat, timeout_s, transport)


WCS_URL = "https://maps.isric.org/mapserv"
WCS_UNITS = {"sand": "g/kg", "silt": "g/kg", "clay": "g/kg", "soc": "dg/kg", "bdod": "cg/cm3", "cfvo": "cm3/dm3"}
WCS_VERSION = "soilgrids-v2.0-wcs|1"
_wcs_http = httpx.Client(timeout=httpx.Timeout(5.0, connect=3.0), headers={"User-Agent": "HydroPlon-prototype/0.1"})


def _wcs_value(prop: str, cov: str, lon: float, lat: float) -> float | None:
    import warnings
    from rasterio.io import MemoryFile
    q = [("map", f"/map/{prop}.map"), ("SERVICE", "WCS"), ("VERSION", "2.0.1"), ("REQUEST", "GetCoverage"),
         ("COVERAGEID", cov), ("FORMAT", "image/tiff"), ("SUBSET", f"long({lon - 0.003:.5f},{lon + 0.003:.5f})"),
         ("SUBSET", f"lat({lat - 0.003:.5f},{lat + 0.003:.5f})"),
         ("SUBSETTINGCRS", "http://www.opengis.net/def/crs/EPSG/0/4326")]
    for _ in range(2):
        try:
            r = _wcs_http.get(WCS_URL, params=q)
        except httpx.HTTPError:
            continue
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image"):
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with MemoryFile(r.content) as mf, mf.open() as ds:
                v = list(ds.sample([(lon, lat)]))[0][0]
                nodata = ds.nodata
        if nodata is not None and v == nodata:
            return None
        return float(v) if v != 0 or prop in ("cfvo",) else None  # 0 = brak danych (poza cfvo)
    raise SourceError(f"SoilGrids WCS: brak odpowiedzi dla {cov}")


def fetch_point_wcs(lon: float, lat: float) -> dict:
    """ISRIC SoilGrids v2.0 przez WCS (maps.isric.org): wartości pikseli w punkcie, jednostki 'mapped'.
    Średnie dla 6 właściwości × 3 głębokości + kwantyle Q0.05/Q0.95 dla sand/clay/soc (przedział AWC)."""
    from concurrent.futures import ThreadPoolExecutor
    key = f"{lon:.3f},{lat:.3f}"
    cached = hp_cache.get("soilgrids", key, WCS_VERSION)
    if cached:
        return {**cached, "from_cache": True}
    jobs = [(p, d, "mean") for p in PROPS for d in DEPTHS] +            [(p, d, q) for p in ("sand", "clay", "soc") for d in DEPTHS for q in ("Q0.05", "Q0.95")]
    errors = []

    def one(job):
        p, d, st = job
        try:
            return job, _wcs_value(p, f"{p}_{d}_{st}", lon, lat)
        except SourceError:
            errors.append(f"{p}_{d}_{st}")
            return job, None
    from concurrent.futures import wait
    pool = ThreadPoolExecutor(max_workers=8)
    futs = [pool.submit(one, j) for j in jobs]
    done, not_done = wait(futs, timeout=9.0)  # twardy limit modułu gleby
    pool.shutdown(wait=False, cancel_futures=True)
    results = [f.result() for f in done]
    for f, j in zip(futs, jobs):
        if f in not_done:
            errors.append(f"{j[0]}_{j[1]}_{j[2]}:timeout")
            results.append((j, None))
    if len(errors) == len(jobs):
        raise SourceError("SoilGrids WCS nie odpowiada.")
    layers: dict[str, dict] = {d: {p: {} for p in PROPS} for d in DEPTHS}
    for (p, d, st), v in results:
        layers[d][p][st] = v
    all_null = all(layers[d][p].get("mean") is None for d in DEPTHS for p in PROPS)
    record = {"status": "no_data" if all_null else "ok", "layers": None if all_null else layers,
              "reason_pl": "SoilGrids nie zwrócił wartości dla tego punktu (np. woda, zabudowa)." if all_null else None}
    result = {"record": record, "units": dict(WCS_UNITS), "resolution_m": 250, "query_point": [round(lon, 5), round(lat, 5)],
              "access": "WCS (maps.isric.org), próbkowanie piksela w punkcie (siatka 0,002° po reprojekcji serwera)",
              "missing_coverages": errors}
    if not errors:
        hp_cache.put("soilgrids", key, WCS_VERSION, result)
    return {**result, "from_cache": False}


def fetch_point_rest(lon: float, lat: float, timeout_s: float = 30.0, transport=None) -> dict:
    key = f"{lon:.3f},{lat:.3f}"
    cached = hp_cache.get("soilgrids", key, CACHE_VERSION)
    if cached:
        return {**cached, "from_cache": True}
    params = [("lon", f"{lon:.5f}"), ("lat", f"{lat:.5f}")] + [("property", p) for p in PROPS] \
        + [("depth", d) for d in DEPTHS] + [("value", v) for v in ("mean", "Q0.05", "Q0.95")]
    try:
        with httpx.Client(timeout=timeout_s, transport=transport,
                          headers={"User-Agent": "HydroPlon-prototype/0.1"}) as c:
            resp = c.get(URL, params=params)
    except httpx.TimeoutException as exc:
        raise SourceTimeoutError("Przekroczono czas oczekiwania na SoilGrids.") from exc
    except httpx.HTTPError as exc:
        raise SourceError("Brak połączenia z SoilGrids.", details={"error_type": type(exc).__name__}) from exc
    if resp.status_code != 200:
        raise SourceError("SoilGrids odrzucił zapytanie.", details={"http_status": resp.status_code})
    try:
        layers = resp.json()["properties"]["layers"]
    except (ValueError, KeyError, TypeError) as exc:
        raise SourceFormatError("Nieoczekiwana odpowiedź SoilGrids.") from exc
    units: dict[str, str] = {}
    out_layers: dict[str, dict] = {d: {} for d in DEPTHS}
    for layer in layers:
        name = layer.get("name")
        if name not in PROPS:
            continue
        units[name] = _norm_unit(layer.get("unit_measure", {}).get("mapped_units"))
        for dep in layer.get("depths", []):
            if dep.get("label") in out_layers:
                vals = dep.get("values") or {}
                out_layers[dep["label"]][name] = {k: vals.get(k) for k in ("mean", "Q0.05", "Q0.95")}
    all_null = all(v.get("mean") is None for d in out_layers.values() for v in d.values()) or not units
    record = {"status": "no_data" if all_null else "ok", "layers": None if all_null else out_layers,
              "reason_pl": "SoilGrids nie zwrócił wartości dla tego punktu (np. woda, zabudowa)." if all_null else None}
    result = {"record": record, "units": units, "resolution_m": 250,
              "query_point": [round(lon, 5), round(lat, 5)]}
    hp_cache.put("soilgrids", key, CACHE_VERSION, result)
    return {**result, "from_cache": False}
