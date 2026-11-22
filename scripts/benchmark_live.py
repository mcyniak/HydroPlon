"""Benchmark trybu LIVE na działkach w całej Polsce (punkty testowe — aplikacja NIE jest do nich ograniczona).

Mierzy osobno: identyfikację działki, pobranie danych (czasy modułów), reguły (uprawa+metody+plan),
całość; potem ponowną analizę (ciepły cache) i zmianę uprawy. Zimny cache = katalog tymczasowy.

Użycie: .venv\\Scripts\\python -X utf8 scripts\\benchmark_live.py --label before [--limit 14]
Wynik: benchmarks/live_<label>.json
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

# (lon, lat, as_of, opis) — tereny rolnicze w różnych województwach
POINTS = [
    (18.45, 52.85, "2019-06-15", "kujawsko-pomorskie"),
    (17.95, 52.70, "2022-07-15", "wielkopolskie"),
    (22.55, 51.25, "2020-07-20", "lubelskie"),
    (21.00, 50.30, "2023-08-01", "małopolskie"),
    (16.80, 51.10, "2024-06-30", "dolnośląskie"),
    (20.25, 53.55, "2021-05-20", "warmińsko-mazurskie"),
    (23.00, 53.05, "2019-08-10", "podlaskie"),
    (15.45, 53.50, "2025-06-01", "zachodniopomorskie"),
    (19.50, 51.55, "2018-07-01", "łódzkie"),
    (18.05, 50.30, "2024-05-15", "opolskie"),
    (20.70, 52.30, "2026-07-10", "mazowieckie"),
    (17.80, 54.30, "2017-06-20", "pomorskie"),
    (21.20, 50.85, "2022-06-05", "świętokrzyskie"),
    (15.05, 52.30, "2023-05-25", "lubuskie"),
]


def pct(values, q):
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    k = min(len(v) - 1, max(0, int(round(q / 100 * (len(v) - 1)))))
    return round(v[k], 2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--limit", type=int, default=len(POINTS))
    ap.add_argument("--rect", action="store_true",
                    help="geometria = prostokąt 250×200 m w punkcie (gdy źródła działek GUGiK są niedostępne)")
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="hp_bench_cache_"))
    from app.services.adapters import live_satellite, live_soil, live_terrain
    for mod in (live_soil, live_terrain, live_satellite):  # zimny cache
        if hasattr(mod, "CACHE"):
            mod.CACHE = tmp / Path(mod.CACHE).name
    try:
        from app.services import cache as hp_cache  # po optymalizacji: wspólny cache
        hp_cache.configure(tmp / "cache.sqlite")
    except ImportError:
        pass
    from app.engine.orchestrator import Orchestrator
    from app.store import ResultStore
    orch = Orchestrator(mode="live", result_store=ResultStore(), today=date(2026, 11, 21))
    rows = []
    for idx, (lon, lat, d, region) in enumerate(POINTS[: a.limit], start=1):
        rec = {"region": region, "lon": lon, "lat": lat, "as_of": d, "geometry": "rect" if a.rect else "parcel"}
        try:
            t0 = time.perf_counter()
            if a.rect:
                from datetime import datetime
                from shapely.geometry import box
                from app.services.geometry_service import lonlat_to_2180
                from app.services.parcel_service import build_parcel_profile, parcel_cache
                from app.services.uldk_service import RawUldkParcel
                c0 = lonlat_to_2180(lon, lat)
                p = build_parcel_profile(RawUldkParcel(
                    "ok", f"990{idx:03d}_9.9999.{idx}", box(c0.x - 125, c0.y - 100, c0.x + 125, c0.y + 100).wkt, {},
                    None, "benchmark-rect", "live", False, datetime.now().isoformat()))
                parcel_cache.put("live", p)
            else:
                p = orch.parcel_by_point(lon, lat)
            rec["parcel_id"], rec["area_ha"] = p.parcel_id, p.metrics.area_ha
            rec["t_parcel"] = round(time.perf_counter() - t0, 2)
            t1 = time.perf_counter()
            s = orch.analyze(p.parcel_id, date.fromisoformat(d))
            rec["t_analysis"] = round(time.perf_counter() - t1, 2)
            rec["module_timings"] = s.timings_s
            rec["module_statuses"] = s.module_statuses
            rec["unavailable_reasons"] = {m: getattr(s.modules, m).metadata.unavailable_reason for m in s.module_statuses
                                          if s.module_statuses[m] in ("unavailable", "error")}
            sat = s.modules.satellite.model_dump()
            lv = sat.get("latest_valid_observation") or {}
            rec["satellite"] = {"date": lv.get("acquisition_date"), "ndvi": lv.get("ndvi_median"),
                                "ndmi": lv.get("ndmi_median"), "valid_pct": lv.get("valid_parcel_pixels_pct")}
            rec["problems"] = [p_.problem_id for p_ in s.diagnosis.problems]
            t2 = time.perf_counter()
            ca = orch.assess(s.analysis_id, "maize_grain")
            rk = orch.recommend(s.analysis_id, ca.crop_assessment_id)
            ids = [m.method_id for m in rk.recommended] or ["crop_residues"]
            sel = orch.select(s.analysis_id, rk.ranking_run_id, ids)
            orch.plan(sel.selection_id)
            rec["t_rules_plan"] = round(time.perf_counter() - t2, 2)
            rec["t_total_cold"] = round(time.perf_counter() - t0, 2)
            rec["recommended"] = [m.method_id for m in rk.recommended]
            t3 = time.perf_counter()
            orch.analyze(p.parcel_id, date.fromisoformat(d))
            rec["t_analysis_warm"] = round(time.perf_counter() - t3, 2)
            t4 = time.perf_counter()
            ca2 = orch.assess(s.analysis_id, "winter_wheat")
            orch.recommend(s.analysis_id, ca2.crop_assessment_id)
            rec["t_crop_change"] = round(time.perf_counter() - t4, 3)
        except Exception as exc:  # noqa: BLE001
            rec["error"] = f"{type(exc).__name__}: {getattr(exc, 'message_pl', exc)}"
        rows.append(rec)
        print(json.dumps({k: rec.get(k) for k in ("region", "parcel_id", "t_parcel", "t_analysis", "t_total_cold",
                                                   "t_analysis_warm", "module_timings", "error")}, ensure_ascii=False),
              flush=True)
    ok = [r for r in rows if "t_analysis" in r]
    summary = {"label": a.label, "n": len(rows), "n_ok": len(ok)}
    for key in ("t_parcel", "t_analysis", "t_total_cold", "t_analysis_warm", "t_crop_change", "t_rules_plan"):
        vals = [r[key] for r in ok if r.get(key) is not None]
        if vals:
            summary[key] = {"mean": round(statistics.mean(vals), 2), "p50": pct(vals, 50), "p95": pct(vals, 95),
                            "max": round(max(vals), 2)}
    for m in ("soil", "terrain", "hydrology", "weather", "satellite"):
        vals = [r["module_timings"].get(m) for r in ok]
        summary[f"module_{m}"] = {"p50": pct(vals, 50), "p95": pct(vals, 95),
                                  "over_budget": sum(v is None for v in vals),
                                  "ok": sum(r["module_statuses"][m] in ("ok", "partial") for r in ok)}
    out = ROOT / "benchmarks" / f"live_{a.label}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
