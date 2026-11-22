"""Podsumowanie diagnozy wodnej wspólne dla raportu WWW, tekstu i PDF."""
from __future__ import annotations

from datetime import date
from typing import Any

from app.engine.method_catalog import methods_by_id
from app.models.economics import FinalPlan


MODULES = {"weather": "Pogoda", "satellite": "Obserwacje satelitarne", "soil": "Gleba",
           "terrain": "Ukształtowanie terenu", "hydrology": "Hydrologia"}
PROBLEMS = {
    "periodic_water_deficit": "ryzyko okresowych niedoborów wody",
    "low_soil_retention": "mała zdolność gleby do zatrzymywania wody",
    "low_organic_matter": "niska zawartość materii organicznej",
    "surface_runoff_erosion": "ryzyko spływu i erozji",
    "wind_exposure": "narażenie na wysuszanie przez wiatr",
    "uncontrolled_drainage_outflow": "niekontrolowany odpływ przez drenaż",
    "limited_water_storage": "ograniczona możliwość magazynowania wody",
    "irrigation_water_management": "potrzeba gospodarowania wodą do nawodnień",
    "vegetation_moisture_signal": "sygnał wilgotności roślinności",
    "drainage_context": "warunki odpływu i drenażu", "flood_hazard": "zagrożenie powodziowe",
    "water_features_nearby": "obecność cieków i zbiorników",
}
CONFIDENCE = {"high": "wysoka", "medium": "średnia", "low": "niska"}
ACTION_TYPES = {"agrotechnical": "Agrotechnika", "soil": "Agrotechnika", "landscape": "Retencja",
                "hydrotechnical": "Retencja", "drainage": "Retencja", "infrastructure": "Retencja"}
MAIN_ACTION_TYPES = ("Retencja", "Agrotechnika")


def _entry(text: str, label: str = "") -> dict[str, str]:
    return {"label": label, "text": text}


def _date(value: str | None) -> str:
    return date.fromisoformat(value[:10]).strftime("%d.%m.%Y") if value else "brak danych"


def _balance(value: float | None) -> str:
    if value is None:
        return "brak danych"
    if value == 0:
        return "0 mm"
    if 0 < abs(value) < 1:
        return "między −1 a 0 mm" if value < 0 else "między 0 a +1 mm"
    return f"ok. {value:+.0f}\u00a0mm".replace("-", "−")


def _cause(problem: dict[str, Any]) -> str:
    indicators = problem.get("supporting_indicators") or {}
    problem_id = problem["problem_id"]
    if problem_id == "periodic_water_deficit":
        mean_balance = indicators.get("mean_veg_season_p_minus_et0_mm")
        reason = "Dane pogodowe wskazują na ryzyko okresowych niedoborów wody."
        if mean_balance is not None:
            reason += f" Średnie saldo opadów i ewapotranspiracji referencyjnej: {_balance(mean_balance)}."
        return reason
    if problem_id == "low_soil_retention":
        return "Model gleby wskazuje małą zdolność zatrzymywania wody w warstwie 0–30 cm."
    if problem_id == "low_organic_matter":
        return "Model gleby wskazuje małą zawartość materii organicznej, co może ograniczać zatrzymywanie wody."
    if problem_id == "surface_runoff_erosion":
        area = indicators.get("area_over_3deg_pct")
        reason = "Model terenu wskazuje podwyższone ryzyko spływu i erozji."
        if area is not None and area >= 1:
            reason += f" Nachylenie przekracza 3° na ok. {area:.0f}% działki."
        return reason
    return " ".join(problem.get("reasons_pl") or problem.get("reasons") or [])


def _actions(plan: FinalPlan) -> list[dict[str, str]]:
    analysis = plan.section_5_analysis
    catalog = methods_by_id()
    ranking = analysis["ranking"]
    ranked = {method["method_id"]: method for group in ranking.values() for method in group}
    screening = {method["method_id"]: method for method in analysis["method_screening"]}
    categories = {method_id: ACTION_TYPES[method["category"]] for method_id, method in catalog.items()}
    categories["efficient_irrigation"] = "Nawadnianie (opcjonalne)"

    def action(method: dict[str, Any], *, selected: bool) -> dict[str, str]:
        method_id = method["method_id"]
        check = screening[method_id]
        category = categories[method_id]
        if method["screening_status"] == "conditional":
            category += " · warunkowa"
        if not selected:
            category += " · poza wybranym planem"
        links = [PROBLEMS[p["problem_id"]] for p in method["linked_problems"] if p["problem_id"] in PROBLEMS]
        if check["need_status"] == "FALSE":
            basis = "Dane nie wskazują na potrzebę tej metody."
        elif check["need_status"] == "UNKNOWN" or not links:
            basis = "Brak danych do potwierdzenia potrzeby tej metody."
        else:
            basis = f"Podstawa w danych: {links[0]}."
        verification = [r["explanation_pl"] for r in check["rule_evaluations"]
                        if r["severity"] in ("hard", "conditional") and r["outcome"] in ("fail", "unknown")]
        parts = [basis, catalog[method_id]["description_pl"]]
        if method["screening_status"] == "conditional":
            parts.append(" ".join(verification[:2]) or "Wymaga weryfikacji przed wdrożeniem.")
        return _entry(" ".join(parts), f"{category} · {method['name_pl']}")

    selected_ids = [method["method_id"] for method in plan.selected_methods]
    entries = [action(ranked[method_id], selected=True) for method_id in selected_ids]
    selected_categories = {categories[method_id] for method_id in selected_ids}
    for category in MAIN_ACTION_TYPES:
        if category in selected_categories:
            continue
        alternative = next((m for m in ranking["recommended"] if categories[m["method_id"]] == category
                            and m["linked_problems"]), None)
        if alternative:
            entries.append(action(alternative, selected=False))
        else:
            entries.append(_entry("Dostępne dane nie uzasadniają jednoznacznej rekomendacji w tej kategorii.", category))
    return entries


def build_water_report(plan: FinalPlan) -> dict[str, Any]:
    analysis = plan.section_5_analysis
    modules = analysis["modules"]
    metadata = {name: module.get("metadata") or {} for name, module in modules.items()}
    assessment = analysis["crop_assessment"]
    balance = assessment.get("crop_water_balance") or {}
    current = balance.get("target_season_to_date_p_minus_etc_mm")
    critical = balance.get("indicative_critical_window_p_minus_etc_mm")
    if current is None:
        state = "Brak danych do oceny bilansu opadów i potrzeb uprawy w sezonie do daty analizy."
    elif current < 0:
        state = "W sezonie do daty analizy opady były niższe od szacowanych potrzeb wodnych uprawy."
    else:
        state = "Bilans sezonu do daty analizy nie wskazuje na niedobór opadów względem szacowanych potrzeb uprawy."
    critical_status = "Brak danych do oceny okresu największych potrzeb uprawy."
    if critical is not None:
        critical_status = ("Dane wieloletnie wskazują na ryzyko niedoboru" if critical < 0 else
                           "Bilans z danych wieloletnich nie wskazuje na niedobór")
        critical_status += f" w okresie największych potrzeb uprawy (saldo {_balance(critical)})."
    status_items = [_entry(state + (f" Saldo sezonu: {_balance(current)}." if current is not None else "")),
                    _entry(critical_status)]

    diagnosis = analysis["diagnosis"]
    problems = diagnosis["problems"]
    causes = [_entry(reason) for p in problems if (reason := _cause(p))]
    if not causes:
        causes.append(_entry("W dostępnych danych nie wykryto czynników wskazujących na niedobór wody."))

    dates = [_entry(_date(plan.time_context["as_of_date"]), "Data analizy"),
             _entry(_date(metadata["weather"].get("last_observation_date")), "Ostatnia obserwacja IMGW w analizie"),
             _entry(_date((modules["satellite"].get("latest_valid_observation") or {}).get("acquisition_date")),
                    "Ostatnia użyteczna scena Sentinel-2")]
    parcel_source = plan.parcel_summary.get("source") or {}
    parcel_label = parcel_source.get("service") or "brak danych o źródle"
    if parcel_source.get("fetched_at"):
        parcel_label += f" · pobrano {_date(parcel_source['fetched_at'])}"
    sources = [_entry(parcel_label, "Granice działki")]
    for name, label in MODULES.items():
        source = ", ".join(metadata[name].get("sources") or [])
        status = modules[name].get("status")
        if status not in ("ok", "partial"):
            source = "brak danych" + (f" ({source})" if source else "")
        elif status == "partial":
            source = f"dane częściowe ({source})" if source else "dane częściowe, źródło niepodane"
        sources.append(_entry(source or "brak danych o źródle", label))
        if name == "weather":
            period = modules[name].get("decade_summary_up_to_target") or {}
            if period.get("period_start") and period.get("period_end"):
                sources.append(_entry(f"{_date(period['period_start'])} – {_date(period['period_end'])}",
                                      "Zakres analizy pogody"))
    parameter_sources = (assessment.get("crop") or {}).get("parameter_sources") or []
    if parameter_sources:
        sources.append(_entry(", ".join(dict.fromkeys(s["label"] for s in parameter_sources)), "Parametry uprawy"))

    weather_problem = next((p for p in problems if p["problem_id"] == "periodic_water_deficit"), {})
    confidence = CONFIDENCE.get(weather_problem.get("confidence"), "nieustalona")
    limits = [
        _entry(f"Pewność rozpoznania okresowych niedoborów: {confidence}. Saldo to różnica między opadami "
               "a oszacowanym zużyciem wody przez uprawę. Brakuje bezpośrednich pomiarów wilgotności gleby na działce."),
    ]
    station = (modules["weather"].get("stations_used") or {}).get("precipitation_station") or {}
    if station.get("distance_km") is not None:
        distance = f"{station['distance_km']:.1f}".replace(".", ",")
        limits.append(_entry(f"Stacja opadowa jest oddalona o {distance} km. Opady na działce mogą się różnić od pomiaru stacji."))
    if modules["soil"].get("status") in ("ok", "partial"):
        limits.append(_entry("Model gleby opisuje warstwę 0–30 cm. Nie ustalono zasobu wody w całej strefie korzeniowej "
                             "ani rzeczywistego zagęszczenia gleby."))
        if any("mniejsza niż pojedynczy piksel" in t for t in metadata["soil"].get("limitations", [])):
            limits.append(_entry("Działka jest mniejsza od piksela modelu gleby. Wynik opisuje również jej otoczenie."))
    if modules["satellite"].get("latest_valid_observation"):
        limits.append(_entry("Wskaźniki satelitarne opisują okrywę roślinną. Na wynik mogą wpływać m.in. żniwa, uprawa gleby i choroby."))
    limits.extend(_entry(c["reason_pl"]) for c in diagnosis["constraints"] if c.get("reason_pl"))
    for gap in diagnosis.get("data_gaps", []):
        missing = [PROBLEMS[g] for g in gap["cannot_determine"] if g in PROBLEMS]
        if missing:
            limits.append(_entry(f"Brak danych do oceny: {', '.join(missing)}.", MODULES[gap["module"]]))

    return {"dates": dates, "sections": [
        {"title": "Stan niedoboru wody", "entries": status_items},
        {"title": "Przyczyny i czynniki ryzyka", "entries": causes},
        {"title": "Zalecane działania i uzasadnienie", "entries": _actions(plan)},
        {"title": "Źródła danych", "entries": sources},
        {"title": "Ograniczenia i braki danych", "entries": limits},
    ]}
