"""Deterministyczny generator SYNTETYCZNYCH dobowych serii meteorologicznych.

Służy wyłącznie testom mechanizmów (izolacja czasu, luki, ciągi bezopadowe,
ET0, agregaty). Nie odtwarza klimatu żadnej rzeczywistej stacji IMGW.
Brakujący pomiar to ``None`` (odpowiednik statusu "8" w archiwum IMGW), nigdy 0.
"""
from __future__ import annotations

import math
import random
from datetime import date, timedelta
from functools import lru_cache

# Parametry profili: prawdopodobieństwo dnia z opadem w miesiącach IV–IX i poza nim,
# średnia suma dnia opadowego (mm), średnia roczna temperatura i amplituda.
PROFILES = {
    "dry":    {"p_wet_veg": 0.33, "p_wet_off": 0.42, "mean_wet_mm": 4.6},
    "moist":  {"p_wet_veg": 0.45, "p_wet_off": 0.48, "mean_wet_mm": 4.9},
    "normal": {"p_wet_veg": 0.36, "p_wet_off": 0.45, "mean_wet_mm": 4.2},
    "warm":   {"t_mean": 9.0, "t_amp": 10.3, "dtr": 9.5},
    "mild":   {"t_mean": 8.6, "t_amp": 10.0, "dtr": 9.0},
}


def _in_gaps(day: date, gaps) -> bool:
    if gaps == "every_3rd_week":
        return (day.toordinal() // 7) % 3 == 0
    for start, end in gaps or []:
        if date.fromisoformat(start) <= day <= date.fromisoformat(end):
            return True
    return False


@lru_cache(maxsize=16)
def generate_station_series(station_id: str, profile: str, archive_start: str, archive_end: str,
                            gaps_key: str) -> tuple[dict, ...]:
    """Zwraca krotkę słowników {obs_date, precip_mm, tmin_c, tmax_c, tmean_c} (None = brak pomiaru)."""
    import json
    gaps = json.loads(gaps_key)
    rng = random.Random(f"hydroplon-fixture::{station_id}")
    p = PROFILES[profile]
    start, end = date.fromisoformat(archive_start), date.fromisoformat(archive_end)
    rows = []
    day = start
    while day <= end:
        doy = day.timetuple().tm_yday
        veg = 4 <= day.month <= 9
        precip = tmin = tmax = tmean = None
        if "p_wet_veg" in p:
            wet = rng.random() < (p["p_wet_veg"] if veg else p["p_wet_off"])
            amount = round(rng.expovariate(1.0 / p["mean_wet_mm"]), 1) if wet else 0.0
            precip = amount
        if "t_mean" in p:
            seasonal = p["t_mean"] + p["t_amp"] * math.sin(2 * math.pi * (doy - 105) / 365.0)
            tmean = round(seasonal + rng.gauss(0, 2.2), 1)
            half = p["dtr"] / 2.0 + rng.gauss(0, 1.0)
            tmax = round(tmean + abs(half), 1)
            tmin = round(tmean - abs(half), 1)
        if _in_gaps(day, gaps):
            precip = None
            if "t_mean" in p:
                tmin = tmax = tmean = None
        rows.append({"obs_date": day, "precip_mm": precip, "tmin_c": tmin, "tmax_c": tmax, "tmean_c": tmean})
        day += timedelta(days=1)
    return tuple(rows)
