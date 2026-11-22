"""LIVE — pogoda z archiwum dobowego IMGW-PIB (stacje klimatologiczne), zaimportowanego skryptem
``scripts/import_imgw.py`` do ``backend/app/data_cache/imgw_klimat.sqlite``.

Dlaczego nie sieć opadowa: pliki posterunków opadowych zawierają wyłącznie dni z opadem, więc brak
wiersza nie odróżnia dnia suchego od braku pomiaru — nie wolno go zamieniać na 0 mm.
Stacje klimatologiczne mają pełne wiersze dobowe ze statusem (8 = brak pomiaru, 9 = brak zjawiska).
"""
from __future__ import annotations

import math
import sqlite3
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

from app.config import APP_DIR
from app.services.errors import SourceError

DB_PATH = APP_DIR / "data_cache" / "imgw_klimat.sqlite"
MIN_COVERAGE = 0.6  # stacja musi mieć dane dla ≥60% dni okresu, inaczej bierzemy kolejną najbliższą
RECENT_WINDOW_DAYS = 365  # ...oraz ≥60% dni w ostatnim roku przed datą analizy (odrzuca stacje zamknięte)
MAX_CANDIDATES = 20


def _km(lon1, lat1, lon2, lat2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(a))


@lru_cache(maxsize=1)
def _meta(db: str) -> dict:
    with sqlite3.connect(db) as con:
        return dict(con.execute("SELECT key, value FROM meta").fetchall())


def db_available(db: Path = DB_PATH) -> bool:
    return db.exists() and db.stat().st_size > 0


def weather_raw(lon: float, lat: float, as_of: date, db: Path = DB_PATH) -> dict:
    if not db_available(db):
        raise SourceError("Brak lokalnego archiwum IMGW — uruchom scripts/import_imgw.py.",
                          details={"db": str(db)})
    meta = _meta(str(db))
    start = date(2016, 1, 1)
    end = min(as_of, date.fromisoformat(meta["last_observation_date"]))
    n_days = max((end - start).days + 1, 1)
    # Stacja zamknięta w trakcie dekady może mieć ≥60% danych w całym okresie, a zero w ostatnich latach
    # (np. BORUSOWA kończy się 2023-10-18). Wymagamy pokrycia także w ostatnich 365 dniach przed datą analizy.
    recent_start = max(start, end - timedelta(days=RECENT_WINDOW_DAYS - 1))
    n_recent = max((end - recent_start).days + 1, 1)
    with sqlite3.connect(db) as con:
        stations = con.execute("SELECT station_id, name, lon, lat FROM stations WHERE lon IS NOT NULL").fetchall()
        ranked = sorted(stations, key=lambda s: (_km(lon, lat, s[2], s[3]), s[0]))

        def coverage(sid: str, column: str, a: date, b: date) -> int:
            return con.execute(f"SELECT count(*) FROM daily WHERE station_id=? AND obs_date BETWEEN ? AND ? "
                               f"AND {column} IS NOT NULL", (sid, a.isoformat(), b.isoformat())).fetchone()[0]

        def pick(column: str):
            for sid, name, slon, slat in ranked[:MAX_CANDIDATES]:
                if (coverage(sid, column, start, end) / n_days >= MIN_COVERAGE
                        and coverage(sid, column, recent_start, end) / n_recent >= MIN_COVERAGE):
                    return sid, name, round(_km(lon, lat, slon, slat), 1)
            return None

        def series(sid: str) -> tuple:
            rows = {r[0]: r for r in con.execute(
                "SELECT obs_date, tmax, tmin, tmean, precip FROM daily WHERE station_id=? AND obs_date<=? "
                "ORDER BY obs_date", (sid, as_of.isoformat()))}
            out, d = [], start
            last = date.fromisoformat(meta["last_observation_date"])
            while d <= min(as_of, last):
                r = rows.get(d.isoformat())
                out.append({"obs_date": d, "precip_mm": r[4] if r else None, "tmax_c": r[1] if r else None,
                            "tmin_c": r[2] if r else None, "tmean_c": r[3] if r else None})
                d += timedelta(days=1)
            return tuple(out)

        p, t = pick("precip"), pick("tmean")
        label = (f"IMGW-PIB, archiwum dobowe stacji klimatologicznych (pobrane {meta['imported_at'][:10]}, "
                 f"dane do {meta['last_observation_date']})")
        out = {"source_type": "live", "is_demonstration": False, "version": f"imgw-klimat-{meta['last_month']}",
               "assignment_method": "nearest_station_with_coverage", "source_label": label}
        for role, hit in (("precip", p), ("thermal", t)):
            if hit is None:
                out[role] = None
                continue
            sid, name, dist = hit
            out[role] = {"station_id": sid, "name": name, "distance_km": dist,
                         "archive_end": meta["last_observation_date"], "series": series(sid)}
    return out
