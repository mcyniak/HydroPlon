"""Etap 2, usługa 2 — teren i NMT.

W trybie fixture metryki są wstępnie wyliczone (symulacja NMT); usługa wykonuje
klasyfikację ryzyka spływu i flagi wg ``analysis_rules.json``. Funkcja
``slope_stats_from_dem`` pokazuje mechanizm obliczenia nachylenia z siatki
wysokości (bez zależności od numpy) i jest testowana jednostkowo.

Zakazy (Aneks B2 §6, §19.2): obniżenia terenu NIE są przeliczane na objętość
ani projekt zbiornika; NMT nie zastępuje projektu hydrotechnicznego.
"""
from __future__ import annotations

import math
from typing import Any

from app.services.common import REFERENCE_NOTE_HISTORICAL, analysis_rules, base_metadata, unavailable


def slope_stats_from_dem(dem: list[list[float | None]], mask: list[list[bool]], res_m: float) -> dict:
    """Nachylenie [°] różnicami centralnymi; komórki brzegowe i z brakami pomijane (bez imputacji)."""
    rows, cols = len(dem), len(dem[0]) if dem else 0
    slopes: list[float] = []
    for i in range(1, rows - 1):
        for j in range(1, cols - 1):
            if not mask[i][j]:
                continue
            n = (dem[i - 1][j], dem[i + 1][j], dem[i][j - 1], dem[i][j + 1])
            if any(v is None for v in n):
                continue
            dzdy = (n[1] - n[0]) / (2 * res_m)
            dzdx = (n[3] - n[2]) / (2 * res_m)
            slopes.append(math.degrees(math.atan(math.hypot(dzdx, dzdy))))
    if not slopes:
        return {"pixel_count": 0, "slope_mean_deg": None, "slope_max_deg": None,
                "area_over_3deg_pct": None, "area_over_6deg_pct": None}
    n = len(slopes)
    return {"pixel_count": n, "slope_mean_deg": round(sum(slopes) / n, 2), "slope_max_deg": round(max(slopes), 1),
            "area_over_3deg_pct": round(100.0 * sum(s > 3.0 for s in slopes) / n, 1),
            "area_over_6deg_pct": round(100.0 * sum(s > 6.0 for s in slopes) / n, 1)}


def classify_runoff_risk(m: dict, rules: dict) -> str | None:
    over3, over6, mean = m.get("area_over_3deg_pct"), m.get("area_over_6deg_pct"), m.get("slope_mean_deg")
    if over3 is None or mean is None:
        return None
    rr = rules["runoff_risk"]
    if over3 >= rr["high_if_area_over_3deg_pct_gte"] or (over6 or 0) >= rr["high_if_area_over_6deg_pct_gte"]:
        return "wysokie"
    if over3 >= rr["moderate_if_area_over_3deg_pct_gte"] or mean >= rr["moderate_if_slope_mean_deg_gte"]:
        return "umiarkowane"
    return "niskie"


def _slope_class(mean_deg: float) -> str:
    if mean_deg < 1.5:
        return "teren płaski (<1,5°)"
    if mean_deg < 3.0:
        return "teren lekko falisty (1,5–3°)"
    if mean_deg < 6.0:
        return "teren falisty (3–6°)"
    return "teren pagórkowaty (≥6°)"


def compute_terrain(raw: dict, parcel: Any, as_of_mode: str) -> dict:
    rules = analysis_rules()["terrain"]
    st, demo = raw.get("source_type", "unknown"), bool(raw.get("is_demonstration"))
    m = raw.get("record")
    if not m or m.get("status") != "ok":
        return unavailable("terrain", reason_code="no_data_in_source", source_type=st, is_demonstration=demo,
                           message_pl="Brak danych NMT dla działki.", source_version=raw.get("version"),
                           runoff_erosion_risk=None)
    risk = classify_runoff_risk(m, rules)
    limitations = [
        "Analiza NMT wskazuje kierunki spływu i lokalne obniżenia, ale nie zastępuje projektu hydrotechnicznego "
        "ani wizji lokalnej.",
        "Model terenu nie uwzględnia drożności przepustów, zastawek ani aktualnego stanu rowów melioracyjnych.",
        "Lokalne obniżenia są wskazówką do oceny terenowej — nie oznaczają możliwości budowy zbiornika ani jego objętości.",
    ]
    confidence = "wysoka" if m.get("resolution_m", 99) <= 1 else "srednia"
    if (m.get("pixel_count") or 0) < rules["min_pixels_for_confident_slope"]:
        confidence = "orientacyjna"
        limitations.append("Zbyt mało punktów siatki NMT w granicach działki do precyzyjnego wyznaczenia spadku.")
    if m.get("is_fallback_resolution"):
        limitations.append("Usługa NMT nie odpowiedziała w czasie dla siatki docelowej — użyto siatki 5 m; nachylenia "
                           "mogą być zaniżone (wygładzenie). Ponowna analiza spróbuje ponownie siatki 1 m.")
    if as_of_mode == "historical":
        limitations.append(REFERENCE_NOTE_HISTORICAL["terrain"])
    depressions = m.get("local_depressions_area_pct")
    return {
        "status": "ok",
        "module": "terrain",
        "runoff_erosion_risk": risk,
        "elevation_m": {"min": m.get("elev_min"), "max": m.get("elev_max"), "mean": m.get("elev_mean"),
                        "denivelation_m": (round(m["elev_max"] - m["elev_min"], 1)
                                           if m.get("elev_max") is not None and m.get("elev_min") is not None else None)},
        "slope": {"mean_deg": m.get("slope_mean_deg"), "max_deg": m.get("slope_max_deg"),
                  "mean_pct": (round(math.tan(math.radians(m["slope_mean_deg"])) * 100, 2)
                               if m.get("slope_mean_deg") is not None else None),
                  "area_over_3deg_pct": m.get("area_over_3deg_pct"),
                  "area_over_6deg_pct": m.get("area_over_6deg_pct"),
                  "dominant_slope_class": _slope_class(m["slope_mean_deg"]) if m.get("slope_mean_deg") is not None else None},
        "aspect": {"dominant_direction": m.get("dominant_direction"),
                   "south_facing_area_pct": m.get("south_facing_area_pct"),
                   "high_insolation_drying_flag": (m["south_facing_area_pct"] > rules["high_insolation_south_facing_pct_gt"])
                   if m.get("south_facing_area_pct") is not None else None},
        "microrelief": {"local_depressions_area_pct": depressions,
                        "has_potential_retention_basins": (depressions > rules["depressions_flag_pct_gt"])
                        if depressions is not None else None,
                        "interpretation_pl": "Sygnał do oceny terenowej; nie jest projektem ani objętością zbiornika."},
        "metadata": base_metadata(
            sources=[raw.get("source_label", "NMT")], source_type=st, is_demonstration=demo,
            confidence=confidence, data_is_reference=True, source_version=raw.get("version"),
            limitations=limitations, resolution_m=m.get("resolution_m"), computed_from=raw.get("computed_from"),
            thresholds_status=analysis_rules()["status"]),
    }
