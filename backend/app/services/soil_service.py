"""Etap 2, usługa 1 — właściwości gleby (warstwa 0–30 cm).

Obliczenia (Aneks B1 §4): średnia ważona grubością warstw, OM = SOC × 1,724,
pojemność wodna wg Saxton & Rawls (2006), przedział niepewności z kwantyli,
klasa retencji z progów ``analysis_rules.json`` (heurystyki do walidacji).

Ograniczenia celowe: wynik dotyczy wyłącznie 0–30 cm — NIE jest ekstrapolowany
na całą strefę korzeniową; brak warstwy nie jest zastępowany zerem ani średnią.
"""
from __future__ import annotations

from typing import Any

from app.services.common import REFERENCE_NOTE_HISTORICAL, analysis_rules, base_metadata, r, unavailable

# Jawne przeliczniki jednostek SoilGrids (mapped units -> jednostki konwencjonalne).
UNIT_CONVERSIONS = {
    "sand": ("g/kg", "%", 0.1),
    "silt": ("g/kg", "%", 0.1),
    "clay": ("g/kg", "%", 0.1),
    "soc": ("dg/kg", "%", 0.01),
    "bdod": ("cg/cm3", "g/cm3", 0.01),
    "cfvo": ("cm3/dm3", "%", 0.1),
}
REQUIRED = ("sand", "silt", "clay", "soc", "cfvo")
LAYER_THICKNESS_MM = 300.0


def convert(prop: str, value: float | None, source_unit: str) -> float | None:
    if value is None:
        return None
    expected_unit, _, factor = UNIT_CONVERSIONS[prop]
    if source_unit != expected_unit:
        raise ValueError(f"Nieoczekiwana jednostka {prop}: {source_unit} (oczekiwano {expected_unit})")
    return float(value) * factor


def saxton_rawls_theta(sand_pct: float, clay_pct: float, om_pct: float) -> tuple[float, float]:
    """Zwraca (θ33, θ1500) [m3/m3] — Saxton & Rawls (2006), równania 1–2 (bez korekty gęstości)."""
    s, c, om = sand_pct / 100.0, clay_pct / 100.0, om_pct
    t1500t = -0.024 * s + 0.487 * c + 0.006 * om + 0.005 * s * om - 0.013 * c * om + 0.068 * s * c + 0.031
    t1500 = t1500t + (0.14 * t1500t - 0.02)
    t33t = -0.251 * s + 0.195 * c + 0.011 * om + 0.006 * s * om - 0.027 * c * om + 0.452 * s * c + 0.299
    t33 = t33t + (1.283 * t33t ** 2 - 0.374 * t33t - 0.015)
    return t33, t1500


def water_capacity_mm(sand_pct: float, clay_pct: float, om_pct: float, cfvo_pct: float) -> dict[str, float]:
    t33, t1500 = saxton_rawls_theta(sand_pct, clay_pct, om_pct)
    fine_fraction = max(0.0, 1.0 - cfvo_pct / 100.0)
    fc = t33 * LAYER_THICKNESS_MM * fine_fraction
    pwp = t1500 * LAYER_THICKNESS_MM * fine_fraction
    return {"field_capacity_mm": fc, "wilting_point_mm": pwp, "awc_mm": max(0.0, fc - pwp)}


def classify_retention(awc_mm: float | None, rules: dict) -> str:
    if awc_mm is None:
        return "nieznana"
    th = rules["retention_class_awc_mm"]
    if awc_mm < th["niska_lt"]:
        return "niska"
    if awc_mm >= th["wysoka_gte"]:
        return "wysoka"
    return "umiarkowana"


def _weighted(layers: dict, prop: str, stat: str, weights: dict, unit: str) -> float | None:
    total = 0.0
    for depth, w in weights.items():
        layer = layers.get(depth) or {}
        v = (layer.get(prop) or {}).get(stat)
        if v is None:
            return None  # brak warstwy => brak wyniku, bez imputacji
        total += w * convert(prop, v, unit)
    return total


def compute_soil(raw: dict, parcel: Any, as_of_mode: str) -> dict:
    rules = analysis_rules()["soil"]
    st, demo = raw.get("source_type", "unknown"), bool(raw.get("is_demonstration"))
    rec = raw.get("record")
    if not rec or rec.get("status") == "no_data" or not rec.get("layers"):
        return unavailable("soil", reason_code="no_data_in_source", status="unavailable",
                           message_pl="Brak danych glebowych w źródle dla tej działki — nie podstawiono wartości "
                                      "domyślnych ani średnich krajowych.",
                           source_type=st, is_demonstration=demo, source_version=raw.get("version"),
                           retention_class="nieznana")
    layers, units, weights = rec["layers"], raw["units"], rules["profile_depth_weights"]
    mean = {p: _weighted(layers, p, "mean", weights, units[p]) for p in UNIT_CONVERSIONS}
    limitations = [
        "Szacunkowe właściwości gleby na podstawie danych przestrzennych. Przed wykonaniem prac wymagających "
        "szczegółowej znajomości gleby zaleca się weryfikację terenową (próbki).",
        "Model nie zawiera informacji o podeszwie płużnej, zagęszczeniu ani rzeczywistej infiltracji.",
        "Wynik dotyczy warstwy 0–30 cm i nie opisuje zasobu wody w całej strefie korzeniowej.",
    ]
    area_ha = parcel.metrics.area_ha
    if area_ha < rules["pixel_area_ha"]:
        limitations.append(f"Powierzchnia działki ({area_ha} ha) jest mniejsza niż pojedynczy piksel modelu "
                           f"glebowego ({rules['pixel_area_ha']} ha) — wynik uśrednia szersze otoczenie.")
    if as_of_mode == "historical":
        limitations.append(REFERENCE_NOTE_HISTORICAL["soil"])
    missing = [p for p in REQUIRED if mean[p] is None]
    status = "ok" if not missing else "partial"
    om = mean["soc"] * rules["som_from_soc_factor"] if mean["soc"] is not None else None
    wc = None
    if mean["sand"] is not None and mean["clay"] is not None and om is not None and mean["cfvo"] is not None:
        wc = water_capacity_mm(mean["sand"], mean["clay"], om, mean["cfvo"])
    # Przedział niepewności (wariant pesymistyczny/optymistyczny z kwantyli, jeśli dostępne)
    awc_range = None
    q = {k: {s: _weighted(layers, k, s, weights, units[k]) for s in ("Q0.05", "Q0.95")}
         for k in ("sand", "clay", "soc")}
    if wc is not None and all(v is not None for d in q.values() for v in d.values()):
        pess = water_capacity_mm(q["sand"]["Q0.95"], q["clay"]["Q0.05"],
                                 q["soc"]["Q0.05"] * rules["som_from_soc_factor"], mean["cfvo"])["awc_mm"]
        opt = water_capacity_mm(q["sand"]["Q0.05"], q["clay"]["Q0.95"],
                                q["soc"]["Q0.95"] * rules["som_from_soc_factor"], mean["cfvo"])["awc_mm"]
        awc_range = sorted([r(pess), r(opt)])
    else:
        limitations.append("Brak kwantyli źródła — nie wyznaczono przedziału niepewności AWC.")
    if missing:
        limitations.append(f"Brak wartości: {', '.join(missing)} — część wskaźników nie została obliczona.")
    awc = wc["awc_mm"] if wc else None
    return {
        "status": status,
        "module": "soil",
        "soil_group_pl": None,
        "soil_group_note_pl": "Klasyfikacja uziarnienia wg PTG nie jest zaimplementowana w prototypie.",
        "retention_class": classify_retention(awc, rules),
        "granulometry": {"sand_pct": r(mean["sand"]), "silt_pct": r(mean["silt"]), "clay_pct": r(mean["clay"])},
        "bulk_density_gcm3": r(mean["bdod"], 2),
        "coarse_fragments_pct": r(mean["cfvo"]),
        "organic_matter": {
            "soc_pct": r(mean["soc"], 2),
            "organic_matter_pct": r(om, 2),
            "low_humus_flag": (om < rules["low_humus_om_pct_lt"]) if om is not None else None,
            "low_humus_threshold_pct": rules["low_humus_om_pct_lt"],
        },
        "water_holding_capacity_0_30cm": {
            "field_capacity_mm": r(wc["field_capacity_mm"]) if wc else None,
            "wilting_point_mm": r(wc["wilting_point_mm"]) if wc else None,
            "awc_mm": r(awc),
            "awc_uncertainty_range_mm": awc_range,
        },
        "iung_susceptibility_category": None,
        "metadata": base_metadata(
            sources=[raw.get("source_label", "SoilGrids")], source_type=st, is_demonstration=demo,
            confidence="orientacyjna", data_is_reference=True, source_version=raw.get("version"),
            limitations=limitations, resolution=f"{raw.get('resolution_m')} m (0–30 cm)",
            method=rules["awc_method"], thresholds_status=analysis_rules()["status"],
            unit_conversions={k: f"{a} -> {b} (×{f})" for k, (a, b, f) in UNIT_CONVERSIONS.items()}),
    }
