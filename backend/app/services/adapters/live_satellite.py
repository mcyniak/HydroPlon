"""LIVE — Sentinel-2 L2A z publicznego katalogu Earth Search (STAC, bez konta) i plików COG (AWS Open Data).

Dla sceny: odczyt okien B04 (red, 10 m), B08 (nir, 10 m), B11 (swir16, 20 m) i SCL (20 m) z tego samego
zasięgu geograficznego (B11/SCL próbkowane do siatki 10 m tego zasięgu — korekta K16), maska poligonu
działki, czyste piksele SCL ∈ {4,5,6,7}; reflektancja = DN × scale + offset z metadanych STAC
(raster:bands — od 2022 r. obowiązuje offset −0,1, bez niego NDVI/NDMI byłyby zafałszowane).
Wynik sceny: mediana i IQR NDVI/NDMI oraz % czystych pikseli; reguły okna 45 dni / ≥70% / baseline ±15 dni
stosuje satellite_service (wspólna logika z trybem fixture).
"""
from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import httpx
import numpy as np

from app.config import APP_DIR
from app.services.errors import SourceError, SourceTimeoutError

STAC_URL = "https://earth-search.aws.element84.com/v1/search"
CACHE = APP_DIR / "data_cache" / "sentinel_cache.json"
BANDS = ("red", "nir", "swir16", "scl")
CLEAR_SCL = (4, 5, 6, 7)
_lock = threading.Lock()


def _cache() -> dict:
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _cache_put(key: str, value: dict) -> None:
    with _lock:
        data = _cache()
        data[key] = value
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(data), encoding="utf-8")


def stac_search(lon: float, lat: float, start: date, end: date, max_cloud: float, limit: int = 100) -> list[dict]:
    body = {"collections": ["sentinel-2-l2a"], "intersects": {"type": "Point", "coordinates": [lon, lat]},
            "datetime": f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z", "limit": limit,
            "query": {"eo:cloud_cover": {"lt": max_cloud}}}
    try:
        r = httpx.post(STAC_URL, json=body, timeout=12, headers={"User-Agent": "HydroPlon-prototype/0.1"})
    except httpx.TimeoutException as exc:
        raise SourceTimeoutError("Przekroczono czas oczekiwania na katalog STAC Sentinel-2.") from exc
    except httpx.HTTPError as exc:
        raise SourceError("Brak połączenia z katalogiem STAC Sentinel-2.") from exc
    if r.status_code != 200:
        raise SourceError("Katalog STAC odrzucił zapytanie.", details={"http_status": r.status_code})
    feats = r.json().get("features", [])
    best: dict[str, dict] = {}  # jedna scena na dzień (działka może leżeć na styku kafli MGRS)
    for f in feats:
        d = f["properties"]["datetime"][:10]
        if date.fromisoformat(d) > end:
            continue  # izolacja czasu (zabezpieczenie niezależne od filtra katalogu)
        if d not in best or f["properties"].get("eo:cloud_cover", 100) < best[d]["properties"].get("eo:cloud_cover", 100):
            best[d] = f
    return sorted(best.values(), key=lambda f: f["properties"]["datetime"], reverse=True)


def _scale_offset(asset: dict) -> tuple[float, float]:
    rb = (asset.get("raster:bands") or [{}])[0]
    return float(rb.get("scale", 1.0) or 1.0), float(rb.get("offset", 0.0) or 0.0)


def scene_indices(feature: dict, parcel_wkt_2180: str, geom_key: str) -> dict:
    key = f"{feature['id']}|{geom_key}"
    hit = _cache().get(key)
    if hit:
        return hit
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.features import geometry_mask
    from rasterio.windows import from_bounds
    from pyproj import Transformer
    from shapely import wkt as shp_wkt
    import shapely

    geom2180 = shp_wkt.loads(parcel_wkt_2180)
    assets = feature["assets"]
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", AWS_NO_SIGN_REQUEST="YES", GDAL_HTTP_TIMEOUT="10",
                      GDAL_HTTP_MAX_RETRY="1", GDAL_HTTP_RETRY_DELAY="0.5",
                      CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif"):
        with rasterio.open(assets["red"]["href"]) as ref:
            t = Transformer.from_crs("EPSG:2180", ref.crs, always_xy=True)
            g = shapely.transform(geom2180, lambda x, y: t.transform(x, y), interleaved=False)
            minx, miny, maxx, maxy = g.bounds
            win = from_bounds(minx - 20, miny - 20, maxx + 20, maxy + 20, ref.transform).round_offsets().round_lengths()
            wtrans = ref.window_transform(win)
            shape = (int(win.height), int(win.width))
            bounds = rasterio.windows.bounds(win, ref.transform)

        def read(band: str) -> np.ndarray:
            last = None
            for _ in range(2):  # ponowienie przy urwanym odczycie kafla COG
                try:
                    with rasterio.open(assets[band]["href"]) as src:
                        w = from_bounds(*bounds, src.transform)
                        return src.read(1, window=w, out_shape=shape, resampling=Resampling.nearest,
                                        boundless=True, fill_value=0)
                except rasterio.errors.RasterioIOError as exc:
                    last = exc
            raise last

        with ThreadPoolExecutor(max_workers=4) as pool:
            arr = dict(zip(BANDS, pool.map(read, BANDS)))
    inside = ~geometry_mask([g.__geo_interface__], out_shape=shape, transform=wtrans, all_touched=False)
    n_inside = int(inside.sum())
    if n_inside == 0:
        res = {"valid_parcel_pixels_pct": 0.0, "ndvi_median": None, "ndvi_iqr": None, "ndmi_median": None, "ndmi_iqr": None}
    else:
        refl = {}
        offset_note = None
        for b in ("red", "nir", "swir16"):
            s, o = _scale_offset(assets[b])
            refl[b] = arr[b].astype(float) * s + o
        # Kontrola spójności: jeśli offset z metadanych daje ujemną medianę reflektancji czerwieni na działce,
        # dane są już skorygowane (niespójność metadanych katalogu) — offset nie jest stosowany.
        s_r, o_r = _scale_offset(assets["red"])
        if o_r != 0 and inside.any() and np.median(arr["red"][inside & (arr["red"] > 0)] * s_r + o_r) < 0:
            offset_note = "offset_from_metadata_inconsistent_not_applied"
            for b in ("red", "nir", "swir16"):
                s, _ = _scale_offset(assets[b])
                refl[b] = arr[b].astype(float) * s
        # Reflektancja ≤ 0 po korekcie offsetu (ciemne piksele, szum) jest fizycznie niewiarygodna — odrzucamy,
        # inaczej iloraz NDVI/NDMI wychodzi poza [-1, 1].
        positive = (refl["red"] > 0) & (refl["nir"] > 0) & (refl["swir16"] > 0)
        clear = inside & np.isin(arr["scl"], CLEAR_SCL) & positive
        valid_pct = round(float(100.0 * clear.sum() / n_inside), 1)
        res = {"valid_parcel_pixels_pct": valid_pct, "ndvi_median": None, "ndvi_iqr": None,
               "ndmi_median": None, "ndmi_iqr": None}
        if clear.sum() >= 3:
            r_, n_, s_ = refl["red"][clear], refl["nir"][clear], refl["swir16"][clear]
            ndvi = (n_ - r_) / np.where(np.abs(n_ + r_) < 1e-6, np.nan, n_ + r_)
            ndmi = (n_ - s_) / np.where(np.abs(n_ + s_) < 1e-6, np.nan, n_ + s_)
            for name, v in (("ndvi", ndvi), ("ndmi", ndmi)):
                v = v[~np.isnan(v) & (v >= -1.0) & (v <= 1.0)]
                if v.size:
                    q25, q50, q75 = np.percentile(v, [25, 50, 75])
                    res[f"{name}_median"], res[f"{name}_iqr"] = round(float(q50), 3), round(float(q75 - q25), 3)
    out = {"acquisition_date": feature["properties"]["datetime"][:10], "scene_id": feature["id"],
           "scene_cloud_cover_pct": feature["properties"].get("eo:cloud_cover"),
           "processing_baseline": feature["properties"].get("s2:processing_baseline"),
           "radiometric_note": locals().get("offset_note"), **res}
    _cache_put(key, out)
    return out


def _safe_scene(f: dict, parcel_wkt_2180: str, geom_key: str, errors: list) -> dict | None:
    try:
        return scene_indices(f, parcel_wkt_2180, geom_key)
    except Exception as exc:  # noqa: BLE001 — pojedyncza scena nie może wywrócić modułu
        errors.append(f"{f.get('id')}: {type(exc).__name__}")
        return None


def satellite_raw(lon: float, lat: float, parcel_wkt_2180: str, as_of: date, window_days: int = 45,
                  baseline_days: int = 15, baseline_years: int = 3, min_valid: float = 70.0,
                  budget_s: float = 22.0) -> dict:
    import time
    from concurrent.futures import wait
    deadline = time.monotonic() + budget_s
    geom_key = hashlib.sha256(parcel_wkt_2180.encode()).hexdigest()[:16]
    scenes: list[dict] = []
    errors: list[str] = []
    latest = None
    for f in stac_search(lon, lat, as_of - timedelta(days=window_days), as_of, max_cloud=60)[:4]:
        if time.monotonic() > deadline:
            errors.append("time_budget_exceeded_latest")
            break
        sc = _safe_scene(f, parcel_wkt_2180, geom_key, errors)
        if sc is None:
            continue
        scenes.append(sc)
        if sc["valid_parcel_pixels_pct"] >= min_valid and sc["ndmi_median"] is not None:
            latest = date.fromisoformat(sc["acquisition_date"])
            break
    if latest is not None:
        def best_of_year(year: int):
            try:
                center = latest.replace(year=year)
            except ValueError:
                center = latest.replace(year=year, day=28)
            cands = stac_search(lon, lat, center - timedelta(days=baseline_days), center + timedelta(days=baseline_days),
                                max_cloud=30)
            # jedna scena na rok: najmniejsze zachmurzenie, potem najbliżej dnia odniesienia; do 2 prób
            cands.sort(key=lambda f: (f["properties"].get("eo:cloud_cover", 100),
                                      abs((date.fromisoformat(f["properties"]["datetime"][:10]) - center).days)))
            for f in cands[:2]:
                sc = _safe_scene(f, parcel_wkt_2180, geom_key, errors)
                if sc and sc["valid_parcel_pixels_pct"] >= min_valid and sc["ndmi_median"] is not None:
                    return sc
            return None
        years = list(range(latest.year - 1, max(2016, latest.year - baseline_years) - 1, -1))
        pool = ThreadPoolExecutor(max_workers=len(years) or 1)
        futs = [pool.submit(best_of_year, y) for y in years]
        done, not_done = wait(futs, timeout=max(1.0, deadline - time.monotonic()))
        scenes.extend(f.result() for f in done if f.result())
        if not_done:
            errors.append(f"time_budget_exceeded_baseline:{len(not_done)}")
        pool.shutdown(wait=False, cancel_futures=True)
    return {"source_type": "live", "is_demonstration": False, "version": "earth-search-v1-s2l2a",
            "record": {"status": "ok" if scenes else "no_data", "scenes": scenes, "read_errors": errors},
            "edo_background": None,
            "source_label": "Sentinel-2 L2A (Earth Search STAC / AWS Open Data, COG)"}
