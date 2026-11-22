"""ETL: archiwum dobowe IMGW-PIB (stacje klimatologiczne) 2016–bieżący rok → lokalna baza SQLite.

Źródło: https://danepubliczne.imgw.pl/data/dane_pomiarowo_obserwacyjne/dane_meteorologiczne/dobowe/klimat/
Format (k_d_format.txt): NSP, POST, ROK, MC, DZ, TMAX, WTMAX, TMIN, WTMIN, STD, WSTD, TMNG, WTMNG, SMDB, WSMDB,
ROOP, PKSN, WPKSN. Status "8" = brak pomiaru (→ NULL), status "9" = brak zjawiska (dla opadu → 0 mm).
Współrzędne stacji: API IMGW https://danepubliczne.imgw.pl/api/data/meteo (pola lon/lat).

Użycie: .venv\\Scripts\\python -X utf8 scripts\\import_imgw.py [--from 2016] [--to 2026]
Wynik: backend/app/data_cache/imgw_klimat.sqlite (data pobrania zapisana w tabeli meta).
"""
from __future__ import annotations

import argparse
import csv
import io
import sqlite3
import sys
import time
import zipfile
from datetime import date, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "backend" / "app" / "data_cache" / "imgw_klimat.sqlite"
BASE = "https://danepubliczne.imgw.pl/data/dane_pomiarowo_obserwacyjne/dane_meteorologiczne/dobowe/klimat"
API_METEO = "https://danepubliczne.imgw.pl/api/data/meteo"
UA = {"User-Agent": "HydroPlon-prototype/0.1 (hackathon KOWR)"}


def _num(value: str, status: str, *, nothing_is_zero: bool = False) -> float | None:
    if status == "8":
        return None
    if status == "9":
        return 0.0 if nothing_is_zero else None
    value = (value or "").strip()
    if value == "":
        return None
    return float(value)


def parse_klimat_csv(text: str) -> list[tuple]:
    rows = []
    for r in csv.reader(io.StringIO(text)):
        if len(r) < 15:
            continue
        try:
            d = date(int(r[2]), int(r[3]), int(r[4]))
        except ValueError:
            continue
        tmax, tmin, tmean = _num(r[5], r[6]), _num(r[7], r[8]), _num(r[9], r[10])
        precip = _num(r[13], r[14], nothing_is_zero=True)
        rows.append((r[0].strip(), r[1].strip(), d.isoformat(), tmax, tmin, tmean, precip))
    return rows


def init_db(con: sqlite3.Connection) -> None:
    con.executescript("""
    CREATE TABLE IF NOT EXISTS stations(station_id TEXT PRIMARY KEY, name TEXT, lon REAL, lat REAL, coord_source TEXT);
    CREATE TABLE IF NOT EXISTS daily(station_id TEXT, obs_date TEXT, tmax REAL, tmin REAL, tmean REAL, precip REAL,
                                     PRIMARY KEY(station_id, obs_date));
    CREATE INDEX IF NOT EXISTS idx_daily_date ON daily(obs_date);
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
    """)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="y0", type=int, default=2016)
    ap.add_argument("--to", dest="y1", type=int, default=date.today().year)
    a = ap.parse_args()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    init_db(con)
    client = httpx.Client(timeout=60, headers=UA)
    names: dict[str, str] = {}
    months_ok, months_missing = [], []
    t0 = time.perf_counter()
    for year in range(a.y0, a.y1 + 1):
        for month in range(1, 13):
            url = f"{BASE}/{year}/{year}_{month:02d}_k.zip"
            resp = client.get(url)
            if resp.status_code == 404:
                months_missing.append(f"{year}-{month:02d}")
                continue
            resp.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
                for name in z.namelist():
                    if not name.startswith("k_d_") or name.startswith("k_d_t_"):
                        continue
                    rows = parse_klimat_csv(z.read(name).decode("cp1250"))
                    for sid, sname, *_ in rows:
                        names[sid] = sname
                    con.executemany("INSERT OR REPLACE INTO daily VALUES (?,?,?,?,?,?)",
                                    [(r[0], r[2], r[3], r[4], r[5], r[6]) for r in rows])
            months_ok.append(f"{year}-{month:02d}")
            print(f"\r{year}-{month:02d} OK ({len(months_ok)} mies.)", end="", flush=True)
        con.commit()
    print()
    coords = {s["kod_stacji"]: s for s in client.get(API_METEO).json()}
    matched = 0
    for sid, sname in names.items():
        c = coords.get(sid)
        if c and c.get("lon") and c.get("lat"):
            matched += 1
            con.execute("INSERT OR REPLACE INTO stations VALUES (?,?,?,?,?)",
                        (sid, sname, float(c["lon"]), float(c["lat"]), "IMGW API meteo"))
        else:
            con.execute("INSERT OR REPLACE INTO stations VALUES (?,?,?,?,?)", (sid, sname, None, None, "brak"))
    last = con.execute("SELECT max(obs_date) FROM daily").fetchone()[0]
    meta = {"source": BASE, "coords_source": API_METEO, "imported_at": datetime.now().isoformat(timespec="seconds"),
            "months_imported": str(len(months_ok)), "first_month": months_ok[0] if months_ok else "",
            "last_month": months_ok[-1] if months_ok else "", "last_observation_date": last or "",
            "stations_total": str(len(names)), "stations_with_coords": str(matched),
            "license_pl": "Źródłem danych jest Instytut Meteorologii i Gospodarki Wodnej – Państwowy Instytut Badawczy."}
    con.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", list(meta.items()))
    con.commit()
    print(f"Zaimportowano {len(months_ok)} miesięcy ({meta['first_month']}..{meta['last_month']}), stacji {len(names)}, "
          f"ze współrzędnymi {matched}, ostatnia obserwacja {last}, czas {time.perf_counter() - t0:.0f} s → {DB_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
