"""LIVE — teren z GUGiK NMT (WCS 2.0.1, pokrycie DTM_PL-EVRF2007-NH, siatka 1 m, eksport x-aaigrid).

Zweryfikowane 2026-11-21: osie pokrycia "y x" (x = północ, y = wschód, jak w EPSG:2180); w nagłówku
siatki xllcorner = N, yllcorner = E; kolumny biegną wzdłuż N, wiersze wzdłuż E. Kolejność wierszy jest
sprawdzana w teście integracyjnym (porównanie z wycinkiem kontrolnym).

Obliczenia (Aneks B2 §4): nachylenie z gradientu, % powierzchni > 3° i > 6°, ekspozycja, TPI (okno ~25 m),
wszystko wyłącznie w granicach działki (maska poligonu). Rozdzielczość dobierana tak, by siatka miała
≤ ~250 tys. komórek (1 m dla działek do kilkunastu ha, większa dla dużych).
"""
from __future__ import annotations

import hashlib
import json
import math
import threading

import httpx
import numpy as np
import shapely
from shapely import wkt as shp_wkt

from app.config import APP_DIR
from app.services import cache as hp_cache
from app.services.errors import SourceError, SourceFormatError, SourceTimeoutError

URL = "https://mapy.geoportal.gov.pl/wss/service/PZGIK/NMT/GRID1/WCS/DigitalTerrainModel"
COVERAGE = "DTM_PL-EVRF2007-NH"
BUFFER_M = 15.0  # okno TPI 25 m wymaga ≥12,5 m marginesu; mniej danych do pobrania
MAX_CELLS = 200_000  # 1 m dla działek do ~15 ha (2 m zaniżało nachylenie: 2,22° → 1,82°, >3°: 18,6% → 13,4%,
                     # zmieniając klasę spływu); GeoTIFF z tej usługi to 8-bitowy obraz RGB — tylko ASCII ma wysokości
CACHE = APP_DIR / "data_cache" / "terrain_cache.json"
MAIN_TIMEOUT_S = 15.0
_lock = threading.Lock()


def parse_aaigrid(payload: bytes) -> tuple[dict, np.ndarray]:
    text = payload.decode("ascii", errors="ignore")
    start = text.find("ncols")
    if start < 0:
        raise SourceFormatError("Odpowiedź NMT WCS nie zawiera siatki ASCII.")
    body = text[start:]
    end = body.find("--wcs")
    if end > 0:
        body = body[:end]
    lines = body.strip().splitlines()
    header = {}
    i = 0
    while i < len(lines) and lines[i].split()[0].lower() in ("ncols", "nrows", "xllcorner", "yllcorner", "cellsize",
                                                             "nodata_value", "xllcenter", "yllcenter", "dx", "dy"):
        k, v = lines[i].split()[:2]
        header[k.lower()] = float(v)
        i += 1
    vals = np.array(" ".join(lines[i:]).split(), dtype=float)
    nr, nc = int(header["nrows"]), int(header["ncols"])
    if vals.size != nr * nc:
        raise SourceFormatError("Niepełna siatka NMT (liczba wartości ≠ ncols × nrows).")
    if "cellsize" not in header:  # po skalowaniu serwer podaje dx (wzdłuż kolumn = N) i dy (wzdłuż wierszy = E)
        header["cellsize"] = header.get("dx")
    header.setdefault("dx", header["cellsize"])
    header.setdefault("dy", header["cellsize"])
    grid = vals.reshape(nr, nc)
    if "nodata_value" in header:
        grid[grid == header["nodata_value"]] = np.nan
    return header, grid


def fetch_dem(bounds_2180: tuple[float, float, float, float], cell_m: float, transport=None,
              timeout_s: float = 40.0) -> tuple[dict, np.ndarray]:
    minx, miny, maxx, maxy = bounds_2180  # (E_min, N_min, E_max, N_max)
    params = {"SERVICE": "WCS", "VERSION": "2.0.1", "REQUEST": "GetCoverage", "COVERAGEID": COVERAGE,
              "FORMAT": "image/x-aaigrid"}
    query = list(params.items()) + [("SUBSET", f"x({miny:.0f},{maxy:.0f})"), ("SUBSET", f"y({minx:.0f},{maxx:.0f})")]
    if cell_m > 1:
        query.append(("SCALEFACTOR", f"{1.0 / cell_m:.4f}"))
    resp = None
    for attempt in range(2):  # jedno ponowienie przy zerwanym połączeniu (nie przy timeoucie)
        try:
            with httpx.Client(timeout=timeout_s, transport=transport,
                              headers={"User-Agent": "HydroPlon-prototype/0.1"}) as c:
                resp = c.get(URL, params=query)
            break
        except httpx.TimeoutException as exc:
            raise SourceTimeoutError("Przekroczono czas oczekiwania na NMT (WCS GUGiK).") from exc
        except httpx.HTTPError as exc:
            if attempt == 1:
                raise SourceError("Brak połączenia z NMT (WCS GUGiK).",
                                  details={"error_type": type(exc).__name__}) from exc
    if resp.status_code != 200:
        raise SourceError("Usługa NMT odrzuciła zapytanie.", details={"http_status": resp.status_code})
    return parse_aaigrid(resp.content)


def cell_centers(header: dict, shape: tuple[int, int]) -> tuple[np.ndarray, np.ndarray]:
    """Współrzędne (E, N) środków komórek. Kolumny wzdłuż N (xll = N), wiersze wzdłuż E, wiersz 0 = max E."""
    nr, nc = shape
    n = header["xllcorner"] + (np.arange(nc) + 0.5) * header["dx"]
    e = header["yllcorner"] + (nr - np.arange(nr) - 0.5) * header["dy"]
    return np.meshgrid(e, n, indexing="ij")  # (E[nr,nc], N[nr,nc])


def _uniform_mean(a: np.ndarray, size: int) -> np.ndarray:
    """Średnia w oknie size×size (całka obrazu), krawędzie — okno przycięte."""
    filled = np.where(np.isnan(a), np.nanmean(a), a)
    pad = size // 2
    p = np.pad(filled, pad, mode="edge")
    s = p.cumsum(0).cumsum(1)
    s = np.pad(s, ((1, 0), (1, 0)))
    h, w = a.shape
    tot = s[size:size + h, size:size + w] - s[:h, size:size + w] - s[size:size + h, :w] + s[:h, :w]
    return tot / (size * size)


def _cache_get(key: str) -> dict | None:
    try:
        return json.loads(CACHE.read_text(encoding="utf-8")).get(key)
    except (OSError, ValueError):
        return None


def _cache_put(key: str, value: dict) -> None:
    with _lock:
        try:
            data = json.loads(CACHE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data[key] = value
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(data), encoding="utf-8")


def terrain_metrics(wkt_2180: str, transport=None, use_cache: bool = True) -> dict:
    key = hashlib.sha256(wkt_2180.encode()).hexdigest()[:24]
    version = f"{COVERAGE}|{MAX_CELLS}|{BUFFER_M}|terrain-algo-2"
    if use_cache and (hit := hp_cache.get("terrain", key, version)):
        return {**hit, "from_cache": True}
    result = _compute_metrics(wkt_2180, transport)
    if not result.get("is_fallback_resolution"):  # wynik zastępczy 5 m nie trafia do cache — następnym razem znów 1 m
        hp_cache.put("terrain", key, version, result)
    return {**result, "from_cache": False}


def _compute_metrics(wkt_2180: str, transport=None) -> dict:
    geom = shp_wkt.loads(wkt_2180)
    minx, miny, maxx, maxy = geom.bounds
    minx, miny, maxx, maxy = minx - BUFFER_M, miny - BUFFER_M, maxx + BUFFER_M, maxy + BUFFER_M
    cell = max(1.0, math.ceil(math.sqrt((maxx - minx) * (maxy - miny) / MAX_CELLS)))
    bounds = (minx, miny, maxx, maxy)
    fallback = False
    if cell >= 5:
        header, z = fetch_dem(bounds, cell, transport, timeout_s=15.0)
    else:
        # Usługa GUGiK bywa wolna (pomiar: 2–8 s, okresowo >25 s). Równolegle z siatką docelową pobieramy małą
        # siatkę 5 m; gdy docelowa nie zdąży w 15 s, używamy 5 m (oznaczone, bez zapisu do cache).
        from concurrent.futures import ThreadPoolExecutor, wait
        pool = ThreadPoolExecutor(max_workers=2)
        f_main = pool.submit(fetch_dem, bounds, cell, transport, MAIN_TIMEOUT_S)
        f_coarse = pool.submit(fetch_dem, bounds, 5.0, transport, MAIN_TIMEOUT_S)
        wait([f_main], timeout=MAIN_TIMEOUT_S + 0.5)
        try:
            if not f_main.done():
                raise SourceTimeoutError("NMT: siatka docelowa nie zdążyła")
            header, z = f_main.result()
        except SourceError:
            wait([f_coarse], timeout=2.0)
            if not f_coarse.done():
                pool.shutdown(wait=False, cancel_futures=True)
                raise SourceTimeoutError("Przekroczono czas oczekiwania na NMT (WCS GUGiK).")
            header, z = f_coarse.result()
            fallback = True
        pool.shutdown(wait=False, cancel_futures=True)
    cs = header["cellsize"]
    E, N = cell_centers(header, z.shape)
    # gradient: oś 0 = wiersze (E malejące), oś 1 = kolumny (N rosnące)
    dz_drow, dz_dcol = np.gradient(z, header["dy"], header["dx"])
    dz_de, dz_dn = -dz_drow, dz_dcol
    slope = np.degrees(np.arctan(np.hypot(dz_de, dz_dn)))
    aspect = (np.degrees(np.arctan2(-dz_de, -dz_dn)) + 360.0) % 360.0  # kierunek spadku: 0=N, 90=E, 180=S
    tpi = z - _uniform_mean(z, max(3, int(round(25.0 / cs)) | 1))
    mask = shapely.contains_xy(geom, E, N) & ~np.isnan(z) & ~np.isnan(slope)
    n = int(mask.sum())
    if n == 0:
        raise SourceFormatError("Siatka NMT nie pokrywa działki (brak komórek w granicach).")
    zs, ss, asp, tp = z[mask], slope[mask], aspect[mask], tpi[mask]
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    steep = ss > 2.0
    dominant = dirs[int(np.bincount(((asp[steep] + 22.5) // 45 % 8).astype(int), minlength=8).argmax())] if steep.any() else None
    return {
        "status": "ok", "resolution_m": cs, "pixel_count": n,
        "elev_min": round(float(zs.min()), 1), "elev_max": round(float(zs.max()), 1), "elev_mean": round(float(zs.mean()), 1),
        "slope_mean_deg": round(float(ss.mean()), 2), "slope_max_deg": round(float(ss.max()), 1),
        "area_over_3deg_pct": round(float((ss > 3.0).mean() * 100), 1),
        "area_over_6deg_pct": round(float((ss > 6.0).mean() * 100), 1),
        "dominant_direction": dominant,
        "south_facing_area_pct": round(float((steep & (asp >= 135) & (asp <= 245)).mean() * 100), 1),
        "local_depressions_area_pct": round(float((tp < -0.25).mean() * 100), 1),
        "grid_header": {k: header[k] for k in ("ncols", "nrows", "xllcorner", "yllcorner", "dx", "dy")},
        "is_fallback_resolution": fallback,
    }
