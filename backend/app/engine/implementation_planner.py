"""Etap 5.4 — jeden plan wdrożenia dla całego zestawu metod.

Zasady (§20.4, §6 części III): scalanie szablonów (bez dubli), zależności jako DAG,
wykrywanie cykli (bez generowania niewykonalnego harmonogramu), terminy względne,
brak dat decyzji administracyjnych, warunki STOP/GO z weryfikacji.
"""
from __future__ import annotations

import heapq
import json
from datetime import date
from functools import lru_cache
from typing import Any

from app.config import DATA_DIR
from app.engine.ids import make_id
from app.engine.funding_matcher import catalog_for_version
from app.engine.method_catalog import methods_by_id
from app.models.economics import CostEstimate, FundingMatchResult, ImplementationPlan, PlanTask
from app.models.methods import MethodSelectionResult

ENGINE_VERSION = "implementation-planner-0.2.0"


@lru_cache(maxsize=1)
def load_templates() -> dict:
    data = json.loads((DATA_DIR / "task_templates.json").read_text(encoding="utf-8"))
    ids = [t["template_id"] for t in data["templates"]]
    if len(ids) != len(set(ids)):
        raise RuntimeError("Powtórzone template_id w szablonach zadań")
    return data


def find_cycle(nodes: list[str], deps: dict[str, list[str]]) -> list[str] | None:
    """Zwraca listę węzłów tworzących cykl (DFS) albo None."""
    color = {n: 0 for n in nodes}
    stack: list[str] = []

    def visit(n: str) -> list[str] | None:
        color[n] = 1
        stack.append(n)
        for d in deps.get(n, []):
            if d not in color:
                continue
            if color[d] == 1:
                return stack[stack.index(d):] + [d]
            if color[d] == 0:
                found = visit(d)
                if found:
                    return found
        stack.pop()
        color[n] = 2
        return None

    for n in sorted(nodes):
        if color[n] == 0:
            found = visit(n)
            if found:
                return found
    return None


def topo_order(nodes: list[str], deps: dict[str, list[str]], key: dict[str, tuple]) -> list[str]:
    indeg = {n: 0 for n in nodes}
    children: dict[str, list[str]] = {n: [] for n in nodes}
    for n in nodes:
        for d in deps.get(n, []):
            if d in indeg:
                indeg[n] += 1
                children[d].append(n)
    heap = [(key[n], n) for n in nodes if indeg[n] == 0]
    heapq.heapify(heap)
    out = []
    while heap:
        _, n = heapq.heappop(heap)
        out.append(n)
        for c in children[n]:
            indeg[c] -= 1
            if indeg[c] == 0:
                heapq.heappush(heap, (key[c], c))
    return out


def next_window(md_from: str, md_to: str, ref: date) -> tuple[date, date]:
    """Najbliższe (lub trwające) okno coroczne MM-DD..MM-DD względem ``ref``; okno może przechodzić przez Nowy Rok."""
    (m1, d1), (m2, d2) = (tuple(int(x) for x in md.split("-")) for md in (md_from, md_to))
    for year in (ref.year - 1, ref.year, ref.year + 1):
        start = date(year, m1, d1)
        end = date(year + (1 if (m2, d2) < (m1, d1) else 0), m2, d2)
        if end >= ref:
            return start, end
    raise ValueError("nie znaleziono okna")  # nieosiągalne dla poprawnych dat


def _program_steps(funding: FundingMatchResult, active_methods: set[str]) -> tuple[dict, dict, dict]:
    """Kroki z dopasowanych programów wsparcia (katalog): {step_id: szablon}, {step_id: metody}, {step_id: programy}."""
    catalog = {p["program_id"]: p for p in catalog_for_version(funding.catalog_version)["programs"]}
    steps: dict[str, dict] = {}
    methods: dict[str, list[str]] = {}
    programs: dict[str, list[str]] = {}
    for m in funding.matches:
        prog = catalog.get(m.program_id)
        if prog is None or m.match_status == "not_eligible":
            continue
        for st in prog.get("plan_steps", []):
            ms = [x for x in m.method_ids if x in active_methods and (not st.get("method_ids") or x in st["method_ids"])]
            if not ms:
                continue
            sid = st["step_id"]
            steps.setdefault(sid, dict(st, kind="program", depends_on=st.get("depends_on", []), cost_components=[],
                                       requires_specialist=False, monitoring_indicator=None,
                                       start_condition_pl=st.get("start_condition_pl", "zgodnie z warunkami programu"),
                                       resources_pl=st.get("resources_pl", [])))
            methods.setdefault(sid, []).extend(ms)
            label = f"{prog.get('code') or prog['program_id']} {prog['name_pl']}"
            if label not in programs.setdefault(sid, []):
                programs[sid].append(label)
    return steps, methods, programs


def _included(t: dict, facts: dict, special: dict[str, bool]) -> bool:
    cond = t.get("include_if")
    if not cond:
        return True
    if "special" in cond:
        return special.get(cond["special"], False)
    return facts.get(cond["fact"]) in cond["in"]


def _window(t: dict, ref: date, done: dict[str, tuple[date, date]], tid: str) -> dict:
    w = t.get("window")
    if not w:
        return {}
    if w.get("after_step") in done:  # np. utrzymanie międzyplonu liczone od najbliższego okna siewu
        ref = max(ref, done[w["after_step"]][0])
    start, end = next_window(w["from"], w["to"], ref)
    done[tid] = (start, end)
    note = ("termin coroczny według zasad programu" if w.get("certainty") == "rule"
            else "termin według ostatniego naboru — kolejny nabór nie jest jeszcze ogłoszony")
    if start <= ref <= end:
        note = "okno trwa — " + note
    return {"window_from": start, "window_to": end, "window_note_pl": note}


def build_plan(selection: MethodSelectionResult, facts: dict, cost: CostEstimate, funding: FundingMatchResult,
               screening: dict[str, Any], implementation_start: date | None = None,
               templates: dict | None = None) -> ImplementationPlan:
    tpl_data = templates or load_templates()
    tpls = {t["template_id"]: t for t in tpl_data["templates"]}
    tpl_order = {t["template_id"]: i for i, t in enumerate(tpl_data["templates"])}
    phases = tpl_data["phases"]
    catalog = methods_by_id()
    special = {"has_unpriced_costs": bool(cost.totals.unpriced_item_ids),
               "has_funding_matches": any(m.match_status != "not_eligible" for m in funding.matches)}
    chosen: dict[str, list[str]] = {}
    warnings: list[str] = []
    active = [m for m in selection.selected_methods if not m.blocked]
    for sm in active:
        for tid in catalog[sm.method_id]["task_template_ids"]:
            if tid not in tpls:
                warnings.append(f"Brak szablonu zadania {tid} dla metody {sm.method_id}.")
                continue
            if _included(tpls[tid], facts, special):
                chosen.setdefault(tid, []).append(sm.method_id)
    for tid in ("T_QUOTES", "T_FUNDING_CHECK"):
        if tid in tpls and _included(tpls[tid], facts, special) and active:
            chosen.setdefault(tid, []).extend(m.method_id for m in active)
    # Kroki wynikające z dopasowanych programów wsparcia (terminy kampanii, wymogi praktyk)
    prog_steps, prog_methods, prog_programs = _program_steps(funding, {m.method_id for m in active})
    for sid, st in prog_steps.items():
        if sid not in tpls:
            tpls[sid] = st
            tpl_order[sid] = len(tpl_order)
            chosen.setdefault(sid, []).extend(prog_methods[sid])
    ref_day = implementation_start or date.fromisoformat(funding.reference_today)
    dedup = [{"template_id": tid, "merged_for_methods": ms,
              "reason_pl": "Jedno wspólne zadanie dla kilku metod (bez dublowania)."}
             for tid, ms in sorted(chosen.items()) if len(set(ms)) > 1]
    nodes = sorted(chosen)
    deps = {tid: [d for d in tpls[tid]["depends_on"] if d in chosen] for tid in nodes}
    cycle = find_cycle(nodes, deps)
    pid = make_id("impl", {"selection": selection.selection_id, "cost": cost.cost_estimate_id,
                           "funding": funding.funding_match_id, "templates": tpl_data["version"],
                           "start": implementation_start.isoformat() if implementation_start else None,
                           "engine": ENGINE_VERSION})
    if cycle:
        return ImplementationPlan(
            implementation_plan_id=pid, selection_id=selection.selection_id, status="error", tasks=[], milestones=[],
            stop_go_conditions=[], cycle_detected=cycle, deduplicated_tasks=dedup,
            implementation_start_date=implementation_start,
            warnings=warnings + ["Wykryto cykl zależności zadań: " + " → ".join(cycle) +
                                 ". Harmonogram nie został wygenerowany (wymaga korekty szablonów)."],
            engine_version=ENGINE_VERSION)
    for tid in nodes:
        for d in deps[tid]:
            if tpls[d]["phase"] > tpls[tid]["phase"]:
                warnings.append(f"Zadanie {tid} zależy od zadania z późniejszej fazy ({d}).")
    order_key = {tid: (tpls[tid]["phase"], tpl_order[tid]) for tid in nodes}
    ordered = topo_order(nodes, deps, order_key)
    conditional_methods = {m.method_id for m in active if m.screening_status == "conditional"}
    item_by_component: dict[str, list[str]] = {}
    for i in cost.items:
        item_by_component.setdefault(i.component_id, []).append(i.cost_item_id)
    tasks = []
    windows: dict[str, tuple[date, date]] = {}
    for n, tid in enumerate(ordered, start=1):
        t = tpls[tid]
        methods = sorted(set(chosen[tid]), key=lambda m: catalog[m]["catalog_order"])
        rationale = []
        for m in methods:
            s = screening.get(m)
            if s is not None and s.explanation_pl:
                rationale.append(f"{catalog[m]['name_pl']}: {s.explanation_pl[0]}")
        requires_verif = t["kind"] == "verification" or bool(set(methods) & conditional_methods)
        tasks.append(PlanTask(
            task_id=f"task-{n:02d}-{tid}", template_id=tid, phase=t["phase"], phase_label_pl=phases[str(t["phase"])],
            order=n, title_pl=t["title_pl"], purpose_pl=t["purpose_pl"], rationale_pl=rationale[:4],
            timing_pl=t["timing_pl"], depends_on=[f"task-{ordered.index(d) + 1:02d}-{d}" for d in deps[tid]],
            method_ids=methods, cost_item_ids=[c for comp in t["cost_components"] for c in item_by_component.get(comp, [])],
            resources_pl=t["resources_pl"], requires_specialist=t["requires_specialist"],
            start_condition_pl=t["start_condition_pl"], completion_check_pl=t["completion_check_pl"],
            monitoring_indicator=t.get("monitoring_indicator"),
            status="requires_verification" if requires_verif else "ready_to_consider",
            source_rule=(f"{funding.catalog_version}:{tid}" if t.get("kind") == "program" else f"{tpl_data['version']}:{tid}"),
            program_ids=prog_programs.get(tid, []), **_window(t, ref_day, windows, tid)))
    stop_go = []
    for v in selection.required_verifications:
        stop_go.append(f"STOP/GO: {v['verification_id']} ({v['method_id']}) — bez pozytywnego wyniku nie realizować "
                       "zadań wykonawczych tej metody.")
    milestones = []
    for ph in sorted({t.phase for t in tasks}):
        last = [t for t in tasks if t.phase == ph][-1]
        milestones.append({"phase": ph, "label_pl": f"Zakończona faza: {phases[str(ph)]}", "after_task": last.task_id})
    if implementation_start:
        warnings.append(f"Data rozpoczęcia {implementation_start.isoformat()} służy jako punkt odniesienia; terminy zadań "
                        "pozostają względne (etapy sezonu, decyzje organów).")
    return ImplementationPlan(
        implementation_plan_id=pid, selection_id=selection.selection_id,
        status="conditional" if any(t.status == "requires_verification" for t in tasks) else "ok",
        tasks=tasks, milestones=milestones, stop_go_conditions=list(dict.fromkeys(stop_go)), cycle_detected=None,
        deduplicated_tasks=dedup, implementation_start_date=implementation_start, warnings=warnings,
        engine_version=ENGINE_VERSION)
