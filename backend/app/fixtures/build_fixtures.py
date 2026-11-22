"""Generator SYNTETYCZNYCH danych testowych HydroPlon (tryb offline/fixture).

Uruchomienie (z katalogu głównego repozytorium):
    python backend/app/fixtures/build_fixtures.py

Wszystkie wartości są wymyślone na potrzeby testów mechanizmów; NIE opisują
żadnej rzeczywistej działki. Identyfikatory działek mają nieistniejący kod
powiatu "99". Każdy plik zawiera: source_type="fixture", is_demonstration=true.
Generator jest deterministyczny (stałe wartości, bez losowości).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))  # backend/

from pyproj import Transformer  # noqa: E402
from shapely.geometry import LineString, MultiPolygon, Polygon, box  # noqa: E402

T_4326_2180 = Transformer.from_crs("EPSG:4326", "EPSG:2180", always_xy=True)
FIXTURE_TAG = {"source_type": "fixture", "is_demonstration": True,
               "note_pl": "DANE SYNTETYCZNE do testów mechanizmów — nie opisują rzeczywistego terenu."}


def center_2180(lon: float, lat: float) -> tuple[float, float]:
    x, y = T_4326_2180.transform(lon, lat)
    return round(x, 1), round(y, 1)


def rect(cx: float, cy: float, w: float, h: float) -> Polygon:
    return box(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def uldk_text(geom, teryt: str, woj: str, pow_: str, gmi: str, obr: str, nr: str) -> str:
    wkt = geom.wkt
    return f"0\nSRID=2180;{wkt}|{teryt}|{woj}|{pow_}|{gmi}|{obr}|{nr}|EGIB (fixture)\n"


def build() -> None:
    parcels = {}
    # --- Działka A: lekka gleba, nachylenie, ryzyko niedoboru, rów przy granicy -----------------
    ax, ay = center_2180(18.62, 52.98)
    geom_a = rect(ax, ay, 300.0, 160.0)  # 4,80 ha
    parcels["049901_2.0001.101"] = {
        "label": "A", "scenario_pl": "Gleba lekka, ryzyko spływu, częste niedobory opadów, rów przy granicy",
        "uldk_response": uldk_text(geom_a, "049901_2.0001.101", "kujawsko-pomorskie",
                                   "testowy-99 (fikcyjny)", "Testowa-A", "Testowo-A", "101"),
        "weather_station_ids": {"precip": "FX-P-A", "thermal": "FX-T-A"},
    }
    # --- Działka B: gleba pyłowa, płasko, inne warunki ------------------------------------------
    bx, by = center_2180(22.55, 51.25)
    geom_b = rect(bx, by, 250.0, 200.0)  # 5,00 ha
    parcels["069901_2.0001.202/3"] = {
        "label": "B", "scenario_pl": "Gleba pyłowa o wysokiej retencji, teren płaski, część działki w strefie zagrożenia powodziowego",
        "uldk_response": uldk_text(geom_b, "069901_2.0001.202/3", "lubelskie",
                                   "testowy-99 (fikcyjny)", "Testowa-B", "Testowo-B", "202/3"),
        "weather_station_ids": {"precip": "FX-P-B", "thermal": "FX-T-B"},
    }
    # --- Działka C: niekompletne dane -----------------------------------------------------------
    cx, cy = center_2180(21.95, 52.15)
    geom_c = rect(cx, cy, 180.0, 120.0)  # 2,16 ha
    parcels["149901_2.0001.303"] = {
        "label": "C", "scenario_pl": "Niekompletne dane: brak gleby, niska kompletność pogody, niedostępna warstwa powodziowa, brak satelity",
        "uldk_response": uldk_text(geom_c, "149901_2.0001.303", "mazowieckie",
                                   "testowy-99 (fikcyjny)", "Testowa-C", "Testowo-C", "303"),
        "weather_station_ids": {"precip": "FX-P-C", "thermal": "FX-T-C"},
    }
    # --- Działka D: MultiPolygon (dwie części przedzielone drogą) -------------------------------
    dx, dy = center_2180(18.70, 53.02)
    geom_d = MultiPolygon([rect(dx - 105, dy, 190.0, 150.0), rect(dx + 115, dy, 210.0, 150.0)])
    parcels["049901_2.0002.404/1"] = {
        "label": "D", "scenario_pl": "Działka wieloczęściowa (MultiPolygon), warunki zbliżone do A",
        "uldk_response": uldk_text(geom_d, "049901_2.0002.404/1", "kujawsko-pomorskie",
                                   "testowy-99 (fikcyjny)", "Testowa-A", "Testowo-D", "404/1"),
        "weather_station_ids": {"precip": "FX-P-A", "thermal": "FX-T-A"},
    }
    # --- Specjalne przypadki parsera --------------------------------------------------------------
    special = {
        "049901_2.0001.998": {"uldk_response": "-1 brak wynikow\n"},
        "049901_2.0001.999": {"uldk_response": f"0\nSRID=2180;{geom_a.wkt}|049901_2.0001.999|tylko|trzy\n"},
    }
    write("parcels.json", {**FIXTURE_TAG, "version": "fixture-parcels-1.0", "parcels": parcels, "special_responses": special})

    # --- Gleba: surowe warstwy w JEDNOSTKACH SoilGrids (mapped units) --------------------------
    # sand/silt/clay: g/kg; soc: dg/kg; bdod: cg/cm3; cfvo: cm3/dm3. Głębokości 0-5, 5-15, 15-30 cm.
    def layers(sand, silt, clay, soc, bdod, cfvo, q_sand=None, q_clay=None, q_soc=None):
        out = {}
        for depth, k in (("0-5cm", 1.00), ("5-15cm", 0.97), ("15-30cm", 0.92)):
            out[depth] = {
                "sand": {"mean": sand}, "silt": {"mean": silt}, "clay": {"mean": clay},
                "soc": {"mean": round(soc * k)}, "bdod": {"mean": bdod}, "cfvo": {"mean": cfvo},
            }
            if q_sand:
                out[depth]["sand"].update({"Q0.05": q_sand[0], "Q0.95": q_sand[1]})
                out[depth]["clay"].update({"Q0.05": q_clay[0], "Q0.95": q_clay[1]})
                out[depth]["soc"].update({"Q0.05": round(q_soc[0] * k), "Q0.95": round(q_soc[1] * k)})
        return out

    soil = {
        "049901_2.0001.101": {"status": "ok", "layers": layers(800, 140, 60, 90, 152, 40,
                                                               (720, 860), (40, 90), (60, 130))},
        "069901_2.0001.202/3": {"status": "ok", "layers": layers(150, 700, 150, 145, 135, 10,
                                                                 (90, 230), (110, 200), (110, 190))},
        "149901_2.0001.303": {"status": "no_data", "layers": None,
                              "reason_pl": "Brak wartości w źródle dla tego obszaru (scenariusz testowy)."},
        "049901_2.0002.404/1": {"status": "ok", "layers": layers(780, 150, 70, 95, 150, 35,
                                                                 (700, 850), (45, 100), (65, 135))},
    }
    write("soil.json", {**FIXTURE_TAG, "version": "fixture-soilgrids-like-1.0",
                        "units": {"sand": "g/kg", "silt": "g/kg", "clay": "g/kg", "soc": "dg/kg",
                                  "bdod": "cg/cm3", "cfvo": "cm3/dm3"},
                        "resolution_m": 250, "parcels": soil})

    # --- Teren: metryki jak z NMT 1 m (wstępnie obliczone, syntetyczne) ---------------------------
    terrain = {
        "049901_2.0001.101": {"status": "ok", "resolution_m": 1, "elev_min": 84.2, "elev_max": 97.9, "elev_mean": 90.6,
                              "slope_mean_deg": 3.6, "slope_max_deg": 8.4, "area_over_3deg_pct": 47.5,
                              "area_over_6deg_pct": 9.2, "dominant_direction": "S", "south_facing_area_pct": 52.0,
                              "local_depressions_area_pct": 6.1, "pixel_count": 48000},
        "069901_2.0001.202/3": {"status": "ok", "resolution_m": 1, "elev_min": 201.0, "elev_max": 202.6, "elev_mean": 201.8,
                                "slope_mean_deg": 0.7, "slope_max_deg": 2.1, "area_over_3deg_pct": 1.5,
                                "area_over_6deg_pct": 0.0, "dominant_direction": "E", "south_facing_area_pct": 8.0,
                                "local_depressions_area_pct": 2.0, "pixel_count": 50000},
        "149901_2.0001.303": {"status": "ok", "resolution_m": 5, "elev_min": 150.2, "elev_max": 153.0, "elev_mean": 151.4,
                              "slope_mean_deg": 1.6, "slope_max_deg": 4.0, "area_over_3deg_pct": 11.0,
                              "area_over_6deg_pct": 0.0, "dominant_direction": "SW", "south_facing_area_pct": 30.0,
                              "local_depressions_area_pct": 1.0, "pixel_count": 864},
        "049901_2.0002.404/1": {"status": "ok", "resolution_m": 1, "elev_min": 80.0, "elev_max": 92.0, "elev_mean": 86.0,
                                "slope_mean_deg": 3.1, "slope_max_deg": 7.9, "area_over_3deg_pct": 41.0,
                                "area_over_6deg_pct": 7.0, "dominant_direction": "SE", "south_facing_area_pct": 44.0,
                                "local_depressions_area_pct": 4.0, "pixel_count": 60000},
    }
    write("terrain.json", {**FIXTURE_TAG, "version": "fixture-nmt-metrics-1.0",
                           "computed_from": "metryki wstępnie wyliczone (symulacja NMT 1 m / 5 m)", "parcels": terrain})

    # --- Hydrologia: obiekty BDOT10k-podobne w EPSG:2180 + strefy powodziowe ----------------------
    a_minx, a_miny, a_maxx, a_maxy = geom_a.bounds
    b_minx, b_miny, b_maxx, b_maxy = geom_b.bounds
    c_minx, c_miny, c_maxx, c_maxy = geom_c.bounds
    d_minx, d_miny, d_maxx, d_maxy = geom_d.bounds
    hydro = {
        "049901_2.0001.101": {
            "features_layer_status": "ok", "flood_layer_status": "ok",
            "features": [
                {"bdot_code": "SWRM", "feature_type": "row_melioracyjny", "name": None,
                 "wkt": LineString([(a_maxx + 4.0, a_miny - 50), (a_maxx + 4.0, a_maxy + 50)]).wkt},
                {"bdot_code": "SWRS", "feature_type": "ciek_naturalny", "name": "Struga Testowa",
                 "wkt": LineString([(a_minx - 300, a_maxy + 180), (a_maxx + 300, a_maxy + 180)]).wkt},
            ],
            "flood_zones": [
                {"hazard_class": "obszar szczególnego zagrożenia (1%)",
                 "wkt": box(a_minx - 900, a_maxy + 600, a_maxx + 900, a_maxy + 1200).wkt},
            ],
            "jcwp": {"code": "FX-RW-0001", "name": "JCWP testowa A"},
        },
        "069901_2.0001.202/3": {
            "features_layer_status": "ok", "flood_layer_status": "ok",
            "features": [
                {"bdot_code": "SWRM", "feature_type": "row_melioracyjny", "name": None,
                 "wkt": LineString([(b_minx - 220, b_miny - 100), (b_minx - 220, b_maxy + 100)]).wkt},
                {"bdot_code": "PTWP", "feature_type": "zbiornik_wodny", "name": "Staw Testowy",
                 "wkt": box(b_maxx + 380, b_miny, b_maxx + 440, b_miny + 60).wkt},
            ],
            # strefa przecina południowy skraj działki, ale NIE obejmuje centroidu (test K09)
            "flood_zones": [
                {"hazard_class": "obszar szczególnego zagrożenia (1%)",
                 "wkt": box(b_minx - 400, b_miny - 400, b_maxx + 400, b_miny + 30).wkt},
            ],
            "jcwp": {"code": "FX-RW-0002", "name": "JCWP testowa B"},
        },
        "149901_2.0001.303": {
            "features_layer_status": "ok", "flood_layer_status": "unavailable",
            "features": [
                {"bdot_code": "SWRM", "feature_type": "row_melioracyjny", "name": None,
                 "wkt": LineString([(c_maxx + 60, c_miny - 50), (c_maxx + 60, c_maxy + 50)]).wkt},
            ],
            "flood_zones": None, "jcwp": None,
        },
        "049901_2.0002.404/1": {
            "features_layer_status": "ok", "flood_layer_status": "ok",
            "features": [
                {"bdot_code": "SWRM", "feature_type": "row_melioracyjny", "name": None,
                 "wkt": LineString([(d_minx - 8.0, d_miny - 40), (d_minx - 8.0, d_maxy + 40)]).wkt},
            ],
            "flood_zones": [], "jcwp": {"code": "FX-RW-0001", "name": "JCWP testowa A"},
        },
    }
    write("hydrology.json", {**FIXTURE_TAG, "version": "fixture-bdot-isok-like-1.0", "search_buffer_m": 500,
                             "parcels": hydro})

    # --- Stacje meteorologiczne (parametry generatora syntetycznych serii dobowych) --------------
    stations = {
        "FX-P-A": {"name": "STACJA TESTOWA A-OPAD", "lon": 18.58, "lat": 53.02, "has_precip": True, "has_temp": False,
                   "archive_start": "2016-01-01", "archive_end": "2026-10-31", "profile": "dry",
                   "gaps": [["2018-07-10", "2018-07-14"], ["2021-05-03", "2021-05-03"]]},
        "FX-T-A": {"name": "STACJA TESTOWA A-TEMP", "lon": 18.66, "lat": 52.93, "has_precip": False, "has_temp": True,
                   "archive_start": "2016-01-01", "archive_end": "2026-10-31", "profile": "warm", "gaps": []},
        "FX-P-B": {"name": "STACJA TESTOWA B-OPAD", "lon": 22.50, "lat": 51.27, "has_precip": True, "has_temp": False,
                   "archive_start": "2016-01-01", "archive_end": "2026-10-31", "profile": "moist", "gaps": []},
        "FX-T-B": {"name": "STACJA TESTOWA B-TEMP", "lon": 22.60, "lat": 51.22, "has_precip": False, "has_temp": True,
                   "archive_start": "2016-01-01", "archive_end": "2026-10-31", "profile": "mild", "gaps": []},
        "FX-P-C": {"name": "STACJA TESTOWA C-OPAD", "lon": 21.40, "lat": 52.40, "has_precip": True, "has_temp": False,
                   "archive_start": "2016-01-01", "archive_end": "2025-12-31", "profile": "normal",
                   "gaps": "every_3rd_week"},
        "FX-T-C": {"name": "STACJA TESTOWA C-TEMP", "lon": 21.45, "lat": 52.42, "has_precip": False, "has_temp": True,
                   "archive_start": "2016-01-01", "archive_end": "2025-12-31", "profile": "mild", "gaps": []},
    }
    write("weather_stations.json", {**FIXTURE_TAG, "version": "fixture-imgw-like-1.0",
                                     "generator": "app.fixtures.weather_generator (deterministyczny)",
                                     "stations": stations})

    # --- Satelita: sceny (wartości median NDVI/NDMI na działce, odsetek czystych pikseli) ---------
    def scenes_for(base_ndvi, base_ndmi, years, cloudy_2019=False, dip_2019=0.0):
        out = []
        for y in years:
            for (m, d), (dv, dm) in {(4, 17): (-0.25, -0.05), (5, 22): (0.05, 0.03), (6, 11): (0.0, 0.0),
                                     (7, 6): (0.03, -0.01), (8, 20): (-0.30, -0.12), (10, 5): (-0.35, -0.10)}.items():
                date = f"{y}-{m:02d}-{d:02d}"
                valid = 95.0
                if cloudy_2019 and y == 2019 and m in (4, 5, 6):
                    valid = 35.0
                ndmi = base_ndmi + dm - (dip_2019 if (y == 2019 and m == 6) else 0.0) + 0.004 * (y - 2016)
                ndvi = base_ndvi + dv - (dip_2019 * 0.8 if (y == 2019 and m == 6) else 0.0)
                out.append({"acquisition_date": date, "scene_id": f"FIXTURE_S2_{date.replace('-', '')}",
                            "valid_parcel_pixels_pct": valid, "ndvi_median": round(ndvi, 3), "ndvi_iqr": 0.07,
                            "ndmi_median": round(ndmi, 3), "ndmi_iqr": 0.05})
        return out

    years = list(range(2016, 2027))
    satellite = {
        "049901_2.0001.101": {"status": "ok", "scenes": scenes_for(0.70, 0.27, years, dip_2019=0.14)},
        "069901_2.0001.202/3": {"status": "ok", "scenes": scenes_for(0.74, 0.31, years, cloudy_2019=True)},
        "149901_2.0001.303": {"status": "no_data", "scenes": []},
        "049901_2.0002.404/1": {"status": "ok", "scenes": scenes_for(0.69, 0.26, years)},
    }
    write("satellite.json", {**FIXTURE_TAG, "version": "fixture-sentinel2-like-1.0",
                             "edo_background": None, "parcels": satellite})
    print("Fixtures zapisane w", HERE)


def write(name: str, payload: dict) -> None:
    (HERE / name).write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    build()
