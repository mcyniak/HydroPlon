"""Porównanie wariantów pobierania Sentinel-2 na tych samych działkach i datach (zimny cache, osobny proces).

  A0 = dotychczasowy mechanizm (app.services.adapters.live_satellite, kolekcja sentinel-2-l2a)
  A1 = nowy dostawca EarthSearchCOGProvider (sentinel-2-c1-l2a, bramka SCL, równoległość)
  B/C (CDSE Statistical API / STAC+S3) — wymagają konta i sekretu; bez nich zwracają 401 (patrz raport).

Użycie: .venv\\Scripts\\python -X utf8 scripts\\benchmark_satellite.py --variant A1 [--limit 8]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))
from benchmark_live import POINTS, pct  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", choices=["A0", "A1"], required=True)
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--rect", action="store_true", help="prostokąt 250×200 m zamiast działki z GUGiK")
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="hp_sat_"))
    from app.services import cache
    cache.configure(tmp / "c.sqlite")
    from app.services.adapters import live_satellite
    live_satellite.CACHE = tmp / "legacy.json"
    from app.services.adapters import satellite_providers as sp
    from app.services.adapters.live import LiveAdapters
    from app.services.parcel_service import resolve_by_point
    from app.services.satellite_service import compute_satellite
    ad = LiveAdapters(parcel_source="wfs")
    rows = []
    for lon, lat, d, region in POINTS[: a.limit]:
        as_of = date.fromisoformat(d)
        if a.rect:
            # Ta sama geometria dla obu wariantów (prostokąt 250 × 200 m w punkcie) — niezależnie od dostępności GUGiK.
            from datetime import datetime
            from shapely.geometry import box
            from app.services.geometry_service import lonlat_to_2180
            from app.services.parcel_service import build_parcel_profile
            from app.services.uldk_service import RawUldkParcel
            c0 = lonlat_to_2180(lon, lat)
            raw_p = RawUldkParcel("ok", f"rect-{region}", box(c0.x - 125, c0.y - 100, c0.x + 125, c0.y + 100).wkt,
                                  {}, None, "benchmark-rect", "live", False, datetime.now().isoformat())
            p = build_parcel_profile(raw_p)
        else:
            try:
                p = resolve_by_point(lon, lat, ad)
            except Exception as exc:  # noqa: BLE001
                rows.append({"region": region, "error": f"parcel: {exc}"})
                continue
        c = p.coordinates
        t = time.perf_counter()
        try:
            if a.variant == "A0":
                raw = live_satellite.satellite_raw(c.point_on_surface_lon, c.point_on_surface_lat, c.wkt_2180, as_of)
            else:
                res = sp.fetch_with_fallback(sp.Query(c.point_on_surface_lon, c.point_on_surface_lat, c.wkt_2180, as_of),
                                             budget_s=60.0, order=("earth_search_c1",))
                raw = {"source_type": "live", "is_demonstration": False, "record": {"scenes": res["scenes"]},
                       "source_label": "A1"}
            dt = time.perf_counter() - t
            out = compute_satellite(raw, p, as_of)
            lv = out.get("latest_valid_observation") or {}
            bc = out.get("baseline_comparison") or {}
            rows.append({"region": region, "parcel_id": p.parcel_id, "as_of": d, "t_s": round(dt, 2), "status": out["status"],
                         "date": lv.get("acquisition_date"), "ndvi": lv.get("ndvi_median"), "ndmi": lv.get("ndmi_median"),
                         "valid_pct": lv.get("valid_parcel_pixels_pct"), "baseline_years": bc.get("reference_years_count"),
                         "n_scenes_read": len(raw["record"]["scenes"])})
        except Exception as exc:  # noqa: BLE001
            rows.append({"region": region, "parcel_id": p.parcel_id, "t_s": round(time.perf_counter() - t, 2),
                         "error": type(exc).__name__})
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    ts = [r["t_s"] for r in rows if "t_s" in r]
    summary = {"variant": a.variant, "n": len(rows), "mean": round(statistics.mean(ts), 2) if ts else None,
               "p50": pct(ts, 50), "p95": pct(ts, 95), "max": max(ts) if ts else None,
               "with_observation": sum(1 for r in rows if r.get("ndvi") is not None)}
    out = ROOT / "benchmarks" / f"satellite_{a.variant}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
