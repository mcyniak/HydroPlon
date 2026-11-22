"""Źródła live — parsery i logika bez sieci (atrapy odpowiedzi w formatach zweryfikowanych 2026-11-21)."""
from __future__ import annotations

import json
import sqlite3
from datetime import date

import httpx
import numpy as np

from app.services.adapters import live_soil, live_terrain, live_weather
from conftest import PARCEL_A


def test_parse_wcs_aaigrid_with_mime_and_dx_dy():
    payload = (b"\r\n--wcs\r\nContent-Type: image/x-aaigrid\r\n\r\nncols 3\nnrows 2\nxllcorner 553869\n"
               b"yllcorner 462873\ndx 2\ndy 2\n 1 2 3\n 4 5 6\n--wcs--\r\n")
    h, z = live_terrain.parse_aaigrid(payload)
    assert z.shape == (2, 3) and h["cellsize"] == 2 and z[1, 2] == 6
    E, N = live_terrain.cell_centers(h, z.shape)
    assert N[0, 0] == 553870 and E[0, 0] == 462873 + 3  # wiersz 0 = wschodni skraj (zweryfikowane z WCS)


def test_terrain_metrics_on_synthetic_plane(monkeypatch):
    def fake_fetch(bounds, cell, transport=None, timeout_s=0):
        minx, miny, maxx, maxy = bounds
        nc, nr = int((maxy - miny) / cell), int((maxx - minx) / cell)
        e = maxx - (np.arange(nr) + 0.5) * cell
        z = np.tile((0.05 * (e - minx))[:, None], (1, nc))  # 5% spadku w kierunku zachodnim
        return {"ncols": nc, "nrows": nr, "xllcorner": miny, "yllcorner": minx, "cellsize": cell,
                "dx": cell, "dy": cell}, z
    monkeypatch.setattr(live_terrain, "fetch_dem", fake_fetch)
    wkt = "POLYGON((500000 500000, 500200 500000, 500200 500200, 500000 500200, 500000 500000))"
    m = live_terrain.terrain_metrics(wkt, use_cache=False)
    assert 2.7 < m["slope_mean_deg"] < 3.0 and m["dominant_direction"] == "W"


def test_live_weather_reads_imgw_db_and_keeps_missing_as_none(tmp_path):
    db = tmp_path / "imgw.sqlite"
    con = sqlite3.connect(db)
    con.executescript("""CREATE TABLE stations(station_id TEXT, name TEXT, lon REAL, lat REAL, coord_source TEXT);
      CREATE TABLE daily(station_id TEXT, obs_date TEXT, tmax REAL, tmin REAL, tmean REAL, precip REAL);
      CREATE TABLE meta(key TEXT, value TEXT);""")
    con.execute("INSERT INTO stations VALUES ('1','BLISKA',18.6,53.0,'api'), ('2','DALEKA',22.0,51.0,'api')")
    rows = [("1", f"2016-01-{d:02d}", 3.0, -1.0, 1.0, None if d == 5 else 0.0) for d in range(1, 32)]
    con.executemany("INSERT INTO daily VALUES (?,?,?,?,?,?)", rows)
    con.executemany("INSERT INTO meta VALUES (?,?)", [("last_observation_date", "2016-01-31"), ("last_month", "2016-01"),
                                                      ("imported_at", "2026-11-21T23:00:00")])
    con.commit()
    con.close()
    live_weather._meta.cache_clear()
    raw = live_weather.weather_raw(18.61, 53.01, date(2016, 1, 31), db=db)
    assert raw["precip"]["station_id"] == "1" and raw["source_type"] == "live" and not raw["is_demonstration"]
    jan5 = next(r for r in raw["precip"]["series"] if r["obs_date"] == date(2016, 1, 5))
    assert jan5["precip_mm"] is None  # status 8 = brak pomiaru, nie 0 mm
    live_weather._meta.cache_clear()


def _imgw_db(path, stations, rows, last_obs):
    con = sqlite3.connect(path)
    con.executescript("""CREATE TABLE stations(station_id TEXT, name TEXT, lon REAL, lat REAL, coord_source TEXT);
      CREATE TABLE daily(station_id TEXT, obs_date TEXT, tmax REAL, tmin REAL, tmean REAL, precip REAL);
      CREATE TABLE meta(key TEXT, value TEXT);""")
    con.executemany("INSERT INTO stations VALUES (?,?,?,?,'api')", stations)
    con.executemany("INSERT INTO daily VALUES (?,?,?,?,?,?)", rows)
    con.executemany("INSERT INTO meta VALUES (?,?)", [("last_observation_date", last_obs), ("last_month", last_obs[:7]),
                                                      ("imported_at", "2026-11-21T23:00:00")])
    con.commit()
    con.close()
    live_weather._meta.cache_clear()


def _days(sid, a, b):
    from datetime import timedelta
    d, out = a, []
    while d <= b:
        out.append((sid, d.isoformat(), 20.0, 10.0, 15.0, 1.0))
        d += timedelta(days=1)
    return out


def test_live_weather_skips_station_closed_before_as_of(tmp_path):
    # BLISKA ma >60% dni dekady, ale przestała mierzyć w 2023 — dla daty z 2026 trzeba wziąć DALSZĄ, ale czynną.
    db = tmp_path / "imgw.sqlite"
    _imgw_db(db, [("1", "BLISKA", 18.6, 53.0), ("2", "DALSZA", 18.9, 53.1)],
             _days("1", date(2016, 1, 1), date(2023, 10, 18)) + _days("2", date(2016, 1, 1), date(2026, 9, 30)),
             "2026-09-30")
    raw = live_weather.weather_raw(18.61, 53.01, date(2026, 11, 22), db=db)
    assert raw["precip"]["name"] == "DALSZA"
    hist = live_weather.weather_raw(18.61, 53.01, date(2023, 8, 1), db=db)
    assert hist["precip"]["name"] == "BLISKA"  # dla daty, gdy stacja działała, nadal najbliższa
    live_weather._meta.cache_clear()


def test_weather_status_depends_on_analysis_windows_not_publication_lag(tmp_path):
    from types import SimpleNamespace as NS
    from app.services.weather_service import compute_weather
    db = tmp_path / "imgw.sqlite"
    _imgw_db(db, [("1", "STACJA", 18.6, 53.0)], _days("1", date(2016, 1, 1), date(2026, 9, 30)), "2026-09-30")
    parcel = NS(coordinates=NS(point_on_surface_lat=53.0, point_on_surface_lon=18.6))
    nov = compute_weather(live_weather.weather_raw(18.6, 53.0, date(2026, 11, 22), db=db), parcel,
                          date(2026, 11, 22), "current")
    assert nov["status"] == "ok"  # sezon IV–IX w pełni pokryty, opóźnienie IMGW tylko opisane
    assert any("2026-09-30" in x for x in nov["metadata"]["limitations"])
    aug = compute_weather(live_weather.weather_raw(18.6, 53.0, date(2026, 8, 20), db=db), parcel,
                          date(2026, 8, 20), "current")
    assert aug["status"] == "ok"
    # luty: bieżący sezon kończy się w dniu analizy, a danych brak od 30.09 => uczciwie częściowe
    db2 = tmp_path / "imgw2.sqlite"
    _imgw_db(db2, [("1", "STACJA", 18.6, 53.0)], _days("1", date(2016, 1, 1), date(2027, 1, 15)), "2027-01-15")
    feb = compute_weather(live_weather.weather_raw(18.6, 53.0, date(2027, 2, 20), db=db2), parcel,
                          date(2027, 2, 20), "current")
    assert feb["status"] == "partial"
    live_weather._meta.cache_clear()


def test_imgw_status_codes_parsing():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("imp", Path(__file__).parents[1] / "scripts" / "import_imgw.py")
    imp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(imp)
    line = '"249180010","PSZCZYNA","2019","06","01",23.9,"",10.2,"",17.0,"",8.8,"",.0,"9","",0,"9"\n' \
           '"249180010","PSZCZYNA","2019","06","02",25.3,"",12.3,"",19.8,"",10.8,"",,"8","W",0,"9"\n'
    r1, r2 = imp.parse_klimat_csv(line)
    assert r1[6] == 0.0   # status 9 (brak zjawiska) => 0 mm
    assert r2[6] is None  # status 8 (brak pomiaru) => NULL


def test_soilgrids_response_mapped_to_soil_record(tmp_path, monkeypatch):
    from app.services import cache as hp_cache
    old = hp_cache._path
    hp_cache.configure(tmp_path / "c.sqlite")
    layers = [{"name": p, "unit_measure": {"mapped_units": u},
               "depths": [{"label": d, "values": {"mean": v, "Q0.05": v - 10, "Q0.95": v + 10}}
                          for d in live_soil.DEPTHS]}
              for p, u, v in (("sand", "g/kg", 680), ("silt", "g/kg", 210), ("clay", "g/kg", 110),
                              ("soc", "dg/kg", 120), ("bdod", "cg/cm³", 140), ("cfvo", "cm³/dm³", 50))]
    tr = httpx.MockTransport(lambda r: httpx.Response(200, text=json.dumps({"properties": {"layers": layers}})))
    res = live_soil.fetch_point(18.45, 52.85, transport=tr)
    assert res["units"]["bdod"] == "cg/cm3" and res["record"]["layers"]["0-5cm"]["sand"]["mean"] == 680
    assert live_soil.fetch_point(18.45, 52.85, transport=tr)["from_cache"] is True
    from app.services.soil_service import compute_soil
    from app.services.adapters.fixture import FixtureAdapters
    from app.services.parcel_service import build_parcel_profile
    parcel = build_parcel_profile(FixtureAdapters().parcel_by_id(PARCEL_A))
    soil = compute_soil({"source_type": "live", "is_demonstration": False, **res}, parcel, "current")
    assert soil["status"] == "ok" and soil["granulometry"]["sand_pct"] == 68.0
    hp_cache.configure(old)


def test_terrain_uses_5m_fallback_when_1m_too_slow(monkeypatch, tmp_path):
    import time as _t
    from app.services import cache as hp_cache
    old = hp_cache._path
    hp_cache.configure(tmp_path / "c.sqlite")
    monkeypatch.setattr(live_terrain, "MAIN_TIMEOUT_S", 0.3)

    def fake_fetch(bounds, cell, transport=None, timeout_s=0):
        if cell < 5:
            _t.sleep(1.0)  # siatka docelowa nie zdąży
        minx, miny, maxx, maxy = bounds
        nc, nr = max(3, int((maxy - miny) / cell)), max(3, int((maxx - minx) / cell))
        e = maxx - (np.arange(nr) + 0.5) * cell
        z = np.tile((0.05 * (e - minx))[:, None], (1, nc))
        return {"ncols": nc, "nrows": nr, "xllcorner": miny, "yllcorner": minx, "cellsize": cell, "dx": cell, "dy": cell}, z
    monkeypatch.setattr(live_terrain, "fetch_dem", fake_fetch)
    wkt = "POLYGON((500000 500000, 500200 500000, 500200 500200, 500000 500200, 500000 500200, 500000 500000))"
    t0 = _t.perf_counter()
    m = live_terrain.terrain_metrics(wkt)
    assert _t.perf_counter() - t0 < 0.9 and m["is_fallback_resolution"] is True and m["resolution_m"] == 5
    assert live_terrain.terrain_metrics(wkt)["from_cache"] is False  # zastępczy wynik nie jest cache'owany
    hp_cache.configure(old)
