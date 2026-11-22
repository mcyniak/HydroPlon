"""LIVE — hydrologia z oficjalnych, anonimowo dostępnych usług (zweryfikowane 2026-11-22).

1. Sieć wodna: GUGiK BDOT10k WMS (kompozycja_BDOT10k_WMS) — warstwy ``row`` (rów melioracyjny), ``kan``/``kanOkr``
   (kanał), ``rz``/``rzOk`` (rzeka/strumień), ``WPow`` (woda powierzchniowa). Dla każdej grupy pobierany jest
   przezroczysty obraz w buforze 500 m wokół działki; odległość = odległość poligonu działki od najbliższego
   narysowanego piksela obiektu. Dokładność ≈ ±2 × rozmiar piksela (rendering kartograficzny, grubość symbolu).
   Dlaczego nie WFS: BDOT10k WFS (Wody) → 401; MPHP10k WFS Wód Polskich → przekierowanie na logowanie (OAM);
   Overpass (OSM) nie odpowiada z tej sieci i nie jest źródłem urzędowym.
2. Zagrożenie powodziowe: PGW Wody Polskie, INSPIRE NZ HY MZP/MRP WFS (``nz-core:HazardArea``), przecięcie z CAŁYM
   poligonem działki (korekta K09); scenariusze Q0,2% / Q1% / Q10%.
Brak odpowiedzi warstwy => status ``unavailable`` dla tej części (nigdy "brak obiektów" / "brak zagrożenia").
"""
from __future__ import annotations

import hashlib
import math
import warnings
from concurrent.futures import ThreadPoolExecutor

import httpx
import numpy as np
import shapely
from shapely import wkt as shp_wkt
from shapely.geometry import box, shape

from app.services import cache

WMS_URL = "https://mapy.geoportal.gov.pl/wss/service/pub/guest/kompozycja_BDOT10k_WMS/MapServer/WMSServer"
MZP_URL = "https://wody.isok.gov.pl/wss/INSPIRE/INSPIRE_NZ_HY_MZPMRP_WFS"
GROUPS = {  # bdot_code -> (warstwy WMS, typ obiektu)
    "SWRM": ("row", "row_melioracyjny"),
    "SWKN": ("kan,kanOkr", "kanal"),
    "SWRS": ("rz,rzOk", "ciek_naturalny"),
    "PTWP": ("WPow", "woda_powierzchniowa"),
}
BUFFER_M = 500.0
MAX_PX = 1600
VERSION = "hydro-wms-1"
_http = httpx.Client(timeout=10, headers={"User-Agent": "HydroPlon-prototype/0.1"})


def _decode_alpha(png: bytes) -> np.ndarray:
    from rasterio.io import MemoryFile
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with MemoryFile(png) as mf, mf.open() as ds:
            a = ds.read()
    if a.shape[0] == 4:
        return a[3] > 0
    return a[:3].sum(0) > 0


def _wms_group(layers: str, bbox: tuple[float, float, float, float], w: int, h: int) -> np.ndarray | None:
    minx, miny, maxx, maxy = bbox
    p = {"SERVICE": "WMS", "VERSION": "1.1.1", "REQUEST": "GetMap", "LAYERS": layers, "STYLES": "",
         "FORMAT": "image/png", "TRANSPARENT": "TRUE", "SRS": "EPSG:2180", "WIDTH": w, "HEIGHT": h,
         "BBOX": f"{minx:.1f},{miny:.1f},{maxx:.1f},{maxy:.1f}"}
    try:
        r = _http.get(WMS_URL, params=p)
    except httpx.HTTPError:
        return None
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image"):
        return None
    return _decode_alpha(r.content)


def water_features(geom) -> dict:
    minx, miny, maxx, maxy = geom.bounds
    bbox = (minx - BUFFER_M, miny - BUFFER_M, maxx + BUFFER_M, maxy + BUFFER_M)
    span = max(bbox[2] - bbox[0], bbox[3] - bbox[1])
    res = max(2.0, span / MAX_PX)
    w, h = int(math.ceil((bbox[2] - bbox[0]) / res)), int(math.ceil((bbox[3] - bbox[1]) / res))
    with ThreadPoolExecutor(max_workers=4) as pool:
        imgs = dict(zip(GROUPS, pool.map(lambda c: _wms_group(GROUPS[c][0], bbox, w, h), GROUPS)))
    feats, failed = [], []
    for code, mask in imgs.items():
        if mask is None:
            failed.append(code)
            continue
        rows, cols = np.nonzero(mask)
        if rows.size == 0:
            continue  # brak obiektu tej grupy w buforze 500 m
        xs = bbox[0] + (cols + 0.5) * (bbox[2] - bbox[0]) / mask.shape[1]
        ys = bbox[3] - (rows + 0.5) * (bbox[3] - bbox[1]) / mask.shape[0]
        if xs.size > 200_000:
            step = int(math.ceil(xs.size / 200_000))
            xs, ys = xs[::step], ys[::step]
        d = float(shapely.distance(geom, shapely.points(xs, ys)).min())
        feats.append({"bdot_code": code, "feature_type": GROUPS[code][1], "name": None,
                      "distance_m": round(d, 1), "distance_uncertainty_m": round(2 * res, 1),
                      "method": "bdot10k_wms_raster"})
    return {"features": feats, "failed_groups": failed, "pixel_size_m": round(res, 2),
            "status": "unavailable" if len(failed) == len(GROUPS) else ("partial" if failed else "ok")}


MZP_WMS_URL = "https://wody.isok.gov.pl/wss/INSPIRE/INSPIRE_NZ_HY_MZPMRP_WMS"


GFI_PROPS = "loo_qualitativelikelihood,loo_ql_returnperiod,validityperiod_start,validityperiod_end,hazardcategory"
GRID_POINTS = 12


def _gfi(pt, timeout_s: float) -> list[dict] | None:
    """GetFeatureInfo (MZP WMS) w punkcie — tylko atrybuty (bez geometrii doliny: ~1 kB zamiast ~3 MB)."""
    prm = {"SERVICE": "WMS", "VERSION": "1.1.1", "REQUEST": "GetFeatureInfo", "LAYERS": "NZ.Fluvial,NZ.SeaWater",
           "QUERY_LAYERS": "NZ.Fluvial,NZ.SeaWater", "STYLES": "", "SRS": "EPSG:2180",
           "BBOX": f"{pt.x - 5:.1f},{pt.y - 5:.1f},{pt.x + 5:.1f},{pt.y + 5:.1f}", "WIDTH": 11, "HEIGHT": 11,
           "X": 5, "Y": 5, "INFO_FORMAT": "application/json", "FEATURE_COUNT": 20, "propertyName": GFI_PROPS}
    try:
        r = _http.get(MZP_WMS_URL, params=prm, timeout=httpx.Timeout(timeout_s, connect=5.0))
        return [f.get("properties") or {} for f in r.json().get("features", [])] if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


def _grid_points(geom, n: int) -> list:
    """Równomierna siatka punktów wewnątrz poligonu (do n punktów)."""
    minx, miny, maxx, maxy = geom.bounds
    area_ratio = geom.area / max(1.0, (maxx - minx) * (maxy - miny))
    k = max(2, int(math.ceil(math.sqrt(n / max(area_ratio, 0.05)))))
    xs = minx + (np.arange(k) + 0.5) * (maxx - minx) / k
    ys = miny + (np.arange(k) + 0.5) * (maxy - miny) / k
    gx, gy = np.meshgrid(xs, ys)
    pts = shapely.points(gx.ravel(), gy.ravel())
    pts = pts[shapely.contains(geom, pts)]
    if len(pts) > n:
        pts = pts[np.linspace(0, len(pts) - 1, n).astype(int)]
    return list(pts) or [geom.representative_point()]


def flood_zones(geom, timeout_s: float = 6.0) -> dict:
    """Obszary zagrożenia powodziowego MZP (PGW Wody Polskie, INSPIRE NZ: NZ.Fluvial, NZ.SeaWater).

    1) obraz WMS (~2 m) jako sito — czy strefa w ogóle dotyka działki (symbolizacja zaniża pokrycie, więc obraz
       nie służy do liczenia udziału);
    2) GetFeatureInfo (atrybuty, bez geometrii) w siatce do 12 punktów wewnątrz działki + punkty strefy z obrazu;
       udział scenariusza = odsetek punktów siatki w strefie (szacunek próbkowy, niepewność ~1/√n).
    Zweryfikowano z wektorowym WFS HazardArea na 4 działkach w dolinie Wisły (zgodność scenariuszy); WFS nie jest
    używany w analizie, bo zwraca poligony całych dolin (22–60 s)."""
    from rasterio.features import geometry_mask
    from rasterio.transform import from_bounds as tr_from_bounds
    from shapely.geometry import Point
    minx, miny, maxx, maxy = geom.bounds
    pad = 20.0
    bbox = (minx - pad, miny - pad, maxx + pad, maxy + pad)
    res = max(2.0, max(bbox[2] - bbox[0], bbox[3] - bbox[1]) / 1000)
    w, h = int(math.ceil((bbox[2] - bbox[0]) / res)), int(math.ceil((bbox[3] - bbox[1]) / res))
    p = {"SERVICE": "WMS", "VERSION": "1.1.1", "REQUEST": "GetMap", "LAYERS": "NZ.Fluvial,NZ.SeaWater", "STYLES": "",
         "FORMAT": "image/png", "TRANSPARENT": "TRUE", "SRS": "EPSG:2180", "WIDTH": w, "HEIGHT": h,
         "BBOX": f"{bbox[0]:.1f},{bbox[1]:.1f},{bbox[2]:.1f},{bbox[3]:.1f}"}
    screen_hit = None
    extra_pts: list = []
    try:
        r = _http.get(MZP_WMS_URL, params=p, timeout=httpx.Timeout(timeout_s, connect=5.0))
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
            hazard = _decode_alpha(r.content)
            inside = ~geometry_mask([geom.__geo_interface__], out_shape=hazard.shape,
                                    transform=tr_from_bounds(*bbox, hazard.shape[1], hazard.shape[0]))
            rows, cols = np.nonzero(hazard & inside)
            screen_hit = bool(rows.size)
            if rows.size:
                idx = np.linspace(0, rows.size - 1, min(4, rows.size)).astype(int)
                extra_pts = [Point(bbox[0] + (cols[i] + 0.5) * res, bbox[3] - (rows[i] + 0.5) * res) for i in idx]
    except httpx.HTTPError:
        pass
    grid = _grid_points(geom, GRID_POINTS) if screen_hit else [geom.representative_point()]
    pts = grid + extra_pts
    with ThreadPoolExecutor(max_workers=6) as pool:  # ograniczona współbieżność wobec usługi publicznej
        answers = list(pool.map(lambda pt: _gfi(pt, timeout_s), pts))
    if screen_hit is None and all(a is None for a in answers):
        return {"status": "unavailable", "zones": []}
    grid_answers = answers[:len(grid)]
    n_ok = sum(a is not None for a in grid_answers)
    scen: dict[str, dict] = {}
    for i, a in enumerate(answers):
        for pr in a or []:
            label = pr.get("loo_qualitativelikelihood") or pr.get("hazardcategory") or "obszar zagrożenia powodziowego"
            d = scen.setdefault(label, {"grid_hits": set(), "rp": pr.get("loo_ql_returnperiod"),
                                        "vf": pr.get("validityperiod_start"), "vt": pr.get("validityperiod_end"),
                                        "cat": pr.get("hazardcategory"), "pts": []})
            if i < len(grid):
                d["grid_hits"].add(i)
            d["pts"].append(pts[i])
    zones = []
    for label, d in sorted(scen.items(), key=lambda kv: -float(kv[1]["rp"] or 0)):
        share = round(100.0 * len(d["grid_hits"]) / n_ok, 1) if (screen_hit and n_ok) else None
        zones.append({"hazard_class": label, "share_of_parcel_pct": share, "share_method": f"próbka {n_ok} pkt",
                      "return_period_years": d["rp"], "map_valid_from": d["vf"], "map_valid_to": d["vt"],
                      "hazard_category": d["cat"],
                      "wkt": shapely.union_all([pt.buffer(res * 2) for pt in d["pts"]]).intersection(geom.buffer(1)).wkt})
    if screen_hit and not zones:
        zones.append({"hazard_class": "obszar zagrożenia powodziowego wg MZP (scenariusz nieustalony)",
                      "share_of_parcel_pct": None, "share_method": "obraz WMS", "return_period_years": None,
                      "map_valid_from": None, "map_valid_to": None, "hazard_category": None,
                      "wkt": geom.representative_point().buffer(1).wkt})
    status = "ok" if all(a is not None for a in answers) and screen_hit is not None else "partial"
    return {"status": status, "zones": zones, "pixel_size_m": round(res, 2), "query_points": len(pts)}


def hydrology_raw(wkt_2180: str) -> dict:
    key = hashlib.sha256(wkt_2180.encode()).hexdigest()[:24]
    hit = cache.get("hydro", key, VERSION)
    if hit is None:
        geom = shp_wkt.loads(wkt_2180)
        with ThreadPoolExecutor(max_workers=2) as pool:
            fw, ff = pool.submit(water_features, geom), pool.submit(flood_zones, geom)
            wf, fz = fw.result(), ff.result()
        hit = {"wf": wf, "fz": fz}
        if wf["status"] == "ok" and fz["status"] == "ok":
            cache.put("hydro", key, VERSION, hit)
    wf, fz = hit["wf"], hit["fz"]
    record = {"features_layer_status": "ok" if wf["status"] in ("ok", "partial") else "unavailable",
              "features": wf["features"], "failed_groups": wf.get("failed_groups", []),
              "flood_layer_status": fz["status"], "flood_zones": fz["zones"] if fz["status"] == "ok" else None,
              "jcwp": None, "pixel_size_m": wf.get("pixel_size_m")}
    return {"source_type": "live", "is_demonstration": False, "version": VERSION, "record": record,
            "search_buffer_m": BUFFER_M,
            "source_label": "GUGiK BDOT10k WMS (sieć wodna, odległości z obrazu) + PGW Wody Polskie MZP/MRP WFS (HazardArea)"}
