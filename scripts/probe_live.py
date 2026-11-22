"""Jawna sonda dostępności adapterów LIVE (wykonuje 1 zapytanie ULDK; pozostałe adaptery są niewdrożone).

Użycie: .venv/Scripts/python scripts/probe_live.py [lon lat]
Domyślny punkt: centrum Torunia (punkt publiczny, bez danych osobowych).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.services.adapters.live import LiveAdapters  # noqa: E402
from app.services.errors import HydroPlonError  # noqa: E402
from app.services.parcel_service import build_parcel_profile  # noqa: E402


def main() -> int:
    lon, lat = (float(sys.argv[1]), float(sys.argv[2])) if len(sys.argv) == 3 else (18.6047, 53.0102)
    ad = LiveAdapters()
    for info in ad.info():
        print(f"{info.name:16s} implemented={info.implemented!s:5s} verified={info.verified!s:5s} {info.description_pl}")
    t0 = time.perf_counter()
    try:
        raw = ad.parcel_by_xy(lon, lat)
        prof = build_parcel_profile(raw)
        print(f"\nULDK GetParcelByXY OK w {time.perf_counter() - t0:.2f} s: {prof.parcel_id}, "
              f"{prof.metrics.area_ha} ha, {prof.administrative_location.full_label}")
        t1 = time.perf_counter()
        again = build_parcel_profile(ad.parcel_by_id(prof.parcel_id))
        print(f"ULDK GetParcelById OK w {time.perf_counter() - t1:.2f} s; geometria zgodna: "
              f"{again.geometry_version == prof.geometry_version}")
        return 0
    except HydroPlonError as exc:
        print(f"\nULDK NIEDOSTĘPNE: {exc.error_code}: {exc.message_pl} {exc.details}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
