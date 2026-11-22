"""Etap 5.1 — kosztorys CAPEX/OPEX: ilość × cena jednostkowa, z deduplikacją wspólnych pozycji.

Zasady (§20.1, §3.1 części III):
  * brak ilości => requires_sizing / requires_design; brak ceny => requires_quote; NIGDY 0 zł,
  * pozycja współdzielona (shareable) występuje raz i jest przypisana do wszystkich metod,
  * istniejąca infrastruktura zadeklarowana przez rolnika => przegląd zamiast nowej budowy,
  * suma jest "znana częściowo", gdy którakolwiek pozycja nie ma kwoty.
"""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from app.config import DATA_DIR
from app.engine.facts import TRUE, UNKNOWN
from app.engine.ids import make_id
from app.engine.method_catalog import methods_by_id
from app.models.economics import CostEstimate, CostItem, CostTotals
from app.models.methods import MethodSelectionResult

ENGINE_VERSION = "cost-engine-0.1.0"
ALLOWED_UNITS = {"ha", "m2", "m", "m3", "szt.", "t", "kg", "usługa"}


@lru_cache(maxsize=1)
def load_prices() -> dict:
    data = json.loads((DATA_DIR / "unit_prices.fixture.json").read_text(encoding="utf-8"))
    for cid, comp in data["components"].items():
        if comp["unit"] not in ALLOWED_UNITS:
            raise RuntimeError(f"Nieznana jednostka {comp['unit']} w {cid}")
        if comp["unit_price_net"] is not None and comp["unit_price_net"] <= 0:
            raise RuntimeError(f"Cena {cid} musi być dodatnia albo null (brak ceny)")
    return data


def _include(spec: dict, facts: dict) -> bool:
    if "only_if_fact" in spec:
        return facts.get(spec["only_if_fact"]) == TRUE
    if "unless_fact" in spec:
        return facts.get(spec["unless_fact"]) != TRUE
    if "only_if_fact_unknown" in spec:
        return facts.get(spec["only_if_fact_unknown"]) == UNKNOWN
    return True


def estimate_costs(selection: MethodSelectionResult, facts: dict, reference_area_ha: float) -> CostEstimate:
    prices = load_prices()
    comps = prices["components"]
    catalog = methods_by_id()
    qty = {(q.method_id, q.quantity_kind): q for q in selection.quantity_requirements}
    # component_id -> {"methods": [...], "quantity": ..., "qstatus": ...}
    lines: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    dedup: list[dict] = []
    for sm in selection.selected_methods:
        if sm.blocked:
            continue
        for spec in catalog[sm.method_id]["cost_components"]:
            if not _include(spec, facts):
                continue
            cid = spec["component_id"]
            comp = comps[cid]
            src = spec["quantity_from"]
            if src.startswith("fixed:"):
                quantity, qstatus = float(src.split(":", 1)[1]), "known"
            else:
                q = qty.get((sm.method_id, src))
                quantity = q.quantity_value if q else None
                qstatus = q.sizing_status if q else "unknown"
                if q and q.unit != comp["unit"]:
                    raise ValueError(f"Niezgodna jednostka ilości {q.unit} i ceny {comp['unit']} dla {cid}")
            if comp["shareable"]:
                key = cid
            else:
                key = f"{cid}::{sm.method_id}"
            if key in lines:
                lines[key]["methods"].append(sm.method_id)
                dedup.append({"component_id": cid, "kept_once_for": lines[key]["methods"][:],
                              "reason_pl": "Wspólna pozycja kilku metod — naliczona raz."})
                continue
            lines[key] = {"cid": cid, "methods": [sm.method_id], "quantity": quantity, "qstatus": qstatus}
            order.append(key)
    items: list[CostItem] = []
    for key in order:
        ln = lines[key]
        comp = comps[ln["cid"]]
        price = comp["unit_price_net"]
        quantity = ln["quantity"]
        notes = []
        if quantity is None:
            status = "requires_design" if ln["qstatus"] == "requires_design" else "requires_sizing"
            notes.append("Brak ilości robót — pozycja wymaga ustalenia zakresu (nie 0 zł).")
        elif price is None:
            status = "requires_quote"
            notes.append("Brak ceny jednostkowej — wymagana oferta (nie 0 zł).")
        else:
            status = "priced"
        if ln["qstatus"] == "derived":
            notes.append("Ilość przyjęta jako cała powierzchnia działki — do potwierdzenia.")
        total_net = round(quantity * price, 2) if status == "priced" else None
        total_gross = round(total_net * (1 + comp["vat_rate"]), 2) if total_net is not None else None
        items.append(CostItem(
            cost_item_id=f"ci-{ln['cid']}" + ("" if comp["shareable"] else f"-{ln['methods'][0]}"),
            component_id=ln["cid"], name_pl=comp["name_pl"], method_ids=ln["methods"],
            shared=len(ln["methods"]) > 1, cost_type=comp["cost_type"], cost_category=comp["cost_category"],
            frequency=comp["frequency"], quantity=quantity, unit=comp["unit"], quantity_status=ln["qstatus"],
            unit_price_net_pln=price, vat_rate=comp["vat_rate"], total_net_pln=total_net, total_gross_pln=total_gross,
            status=status, price_source="Cennik syntetyczny (fixture) — NIE cena rynkowa" if price is not None else None,
            price_as_of_date=prices["price_as_of_date"] if price is not None else None,
            is_demonstration=prices["is_demonstration"],
            applies_to_existing_infrastructure=bool(comp.get("applies_to_existing_infrastructure")), notes_pl=notes))

    def total(kind: str, field: str) -> tuple[float | None, bool]:
        sel = [i for i in items if i.cost_type == kind]
        known = [getattr(i, field) for i in sel if getattr(i, field) is not None]
        complete = all(getattr(i, field) is not None for i in sel)
        return (round(sum(known), 2) if known else None), complete

    capex_net, capex_ok = total("CAPEX", "total_net_pln")
    capex_gross, _ = total("CAPEX", "total_gross_pln")
    opex_net, opex_ok = total("OPEX", "total_net_pln")
    opex_gross, _ = total("OPEX", "total_gross_pln")
    unpriced = [i.cost_item_id for i in items if i.status != "priced"]
    if not items:
        total_status, expl = "unknown", "Brak pozycji kosztorysu."
    elif not unpriced:
        total_status, expl = "complete", "Wszystkie pozycje wycenione (ceny syntetyczne)."
    else:
        total_status = "partial"
        expl = (f"Suma obejmuje tylko pozycje wycenione; {len(unpriced)} pozycji wymaga wyceny lub ustalenia ilości — "
                "całkowity koszt jest WYŻSZY niż podana suma znana i pozostaje nieokreślony.")
    per_method: dict[str, dict] = {}
    for sm in selection.selected_methods:
        own = [i for i in items if sm.method_id in i.method_ids]
        per_method[sm.method_id] = {
            "item_ids": [i.cost_item_id for i in own],
            "capex_known_net_pln_excl_shared": round(sum(i.total_net_pln for i in own if i.cost_type == "CAPEX"
                                                         and i.total_net_pln is not None and not i.shared), 2),
            "has_unpriced_items": any(i.status != "priced" for i in own),
            "shared_item_ids": [i.cost_item_id for i in own if i.shared],
            "note_pl": "Pozycje wspólne pokazano raz w sumie; nie są dzielone na metody, by uniknąć pozornej precyzji.",
        }
    assumptions = [
        "Ceny jednostkowe pochodzą z SYNTETYCZNEGO cennika testowego — nie są cenami rynkowymi ani ofertami.",
        "Kwoty netto; brutto policzono informacyjnie ze stawką VAT z cennika (możliwość odliczenia VAT nieustalona).",
        f"Cennik z dnia {prices['price_as_of_date']} — nie jest przeliczany na datę historyczną analizy.",
    ]
    if facts.get("field.existing_weir_declared") == TRUE or facts.get("field.existing_pond_declared") == TRUE:
        assumptions.append("Dla zadeklarowanej istniejącej infrastruktury przewidziano przegląd zamiast budowy nowej.")
    eid = make_id("cost", {"selection_id": selection.selection_id, "prices": prices["version"],
                           "engine": ENGINE_VERSION, "facts": {k: facts.get(k) for k in sorted(facts) if k.startswith("field.")}})
    return CostEstimate(
        cost_estimate_id=eid, selection_id=selection.selection_id, analysis_id=selection.analysis_id,
        status="ok" if total_status == "complete" else ("partial" if items else "unavailable"),
        price_catalog_version=prices["version"], price_catalog_is_demonstration=prices["is_demonstration"],
        price_as_of_date=prices["price_as_of_date"], items=items,
        totals=CostTotals(capex_known_net_pln=capex_net, capex_known_gross_pln=capex_gross,
                          opex_annual_known_net_pln=opex_net, opex_annual_known_gross_pln=opex_gross,
                          capex_is_complete=capex_ok, opex_is_complete=opex_ok,
                          capex_per_ha_known_net_pln=(round(capex_net / reference_area_ha, 2)
                                                      if capex_net is not None and reference_area_ha else None),
                          reference_area_ha=reference_area_ha, unpriced_item_ids=unpriced,
                          total_status=total_status, explanation_pl=expl),
        per_method=per_method, deduplicated_shared_items=dedup, assumptions=assumptions,
        warnings=["Kosztorys orientacyjny; brak ilości/ceny oznaczono jawnie."] if unpriced else [],
        engine_version=ENGINE_VERSION)
