"""Etap 2 — orkiestracja pięciu usług i budowa kanonicznego AnalysisSnapshot.

Każda usługa działa niezależnie (równolegle). Awaria jednej nie kończy analizy:
moduł dostaje status unavailable/error z powodem, a pozostałe wyniki zostają.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import date, datetime
from typing import Any, Callable

from app.config import APP_VERSION, TZ, settings
from app.engine.diagnosis import DIAGNOSIS_VERSION, diagnose_parcel
from app.engine.ids import make_id
from app.models.analysis import AnalysisSnapshot, TimeContext
from app.models.parcel import ParcelProfile
from app.services.common import analysis_rules, unavailable
from app.services.errors import AdapterNotImplementedError, InputValidationError, SourceError, SourceTimeoutError
from app.services.hydrology_service import compute_hydrology
from app.services.satellite_service import apply_canopy_rule, compute_satellite
from app.services.soil_service import compute_soil
from app.services.terrain_service import compute_terrain
from app.services.weather_service import compute_weather

log = logging.getLogger("hydroplon.analysis")
MODULES = ("soil", "terrain", "hydrology", "weather", "satellite")
EARLIEST_AS_OF = date(2016, 1, 1)


def build_time_context(as_of: date | None, *, knowledge_cutoff: date | None = None, today: date | None = None,
                       strict_backtest: bool = False, implementation_start: date | None = None,
                       finance_horizon_years: int | None = None) -> TimeContext:
    today = today or settings.today()
    as_of = as_of or today
    if as_of < EARLIEST_AS_OF:
        raise InputValidationError("Data analizy musi być nie wcześniejsza niż 2016-01-01 (zakres archiwum 10 lat).")
    if strict_backtest:
        knowledge_cutoff = min(as_of, today)
    knowledge_cutoff = knowledge_cutoff or today
    if knowledge_cutoff > today:
        raise InputValidationError("Granica wiedzy (knowledge_cutoff_date) nie może być datą przyszłą.")
    mode = "historical" if as_of < today else "current" if as_of == today else "future"
    note = {
        "historical": (f"Rekonstrukcja stanu na {as_of.isoformat()} z wiedzą dostępną do "
                       f"{knowledge_cutoff.isoformat()}. Obserwacje pogody i satelity ograniczono do daty analizy; "
                       "gleba, teren i hydrologia to współczesne warstwy referencyjne."),
        "current": "Analiza na dzień bieżący; aktualność poszczególnych źródeł podano w metadanych modułów.",
        "future": ("Data przyszła: brak obserwacji z przyszłości. Część meteorologiczna opiera się wyłącznie na "
                   "historii; programy wsparcia tylko według ogłoszeń znanych na dzień granicy wiedzy."),
    }[mode]
    return TimeContext(as_of_date=as_of, knowledge_cutoff_date=knowledge_cutoff, reference_today=today, mode=mode,
                       implementation_start_date=implementation_start,
                       finance_horizon_years=finance_horizon_years or settings.finance_horizon_years,
                       strict_backtest=strict_backtest, note_pl=note)


def _timed(module: str, fn: Callable[[], dict], source_type: str) -> tuple[dict, float]:
    t0 = time.perf_counter()
    res = _safe(module, fn, source_type)
    return res, round(time.perf_counter() - t0, 3)


def _safe(module: str, fn: Callable[[], dict], source_type: str) -> dict:
    demo = source_type == "fixture"
    try:
        return fn()
    except AdapterNotImplementedError as exc:
        return unavailable(module, reason_code="adapter_not_implemented", message_pl=exc.message_pl,
                           source_type=source_type, is_demonstration=demo)
    except SourceTimeoutError as exc:
        return unavailable(module, reason_code="source_timeout", message_pl=exc.message_pl,
                           source_type=source_type, is_demonstration=demo)
    except SourceError as exc:
        return unavailable(module, status="error", reason_code=exc.error_code, message_pl=exc.message_pl,
                           source_type=source_type, is_demonstration=demo)
    except Exception:  # noqa: BLE001 — błąd jednego modułu nie może zatrzymać całej analizy
        log.exception("Błąd modułu %s", module)
        return unavailable(module, status="error", reason_code="internal_module_error",
                           message_pl=f"Wewnętrzny błąd modułu {module}; wynik pominięto.",
                           source_type=source_type, is_demonstration=demo)


def run_analysis(parcel: ParcelProfile, tc: TimeContext, adapters, budget_s: float | None = None) -> AnalysisSnapshot:
    budget = min(budget_s or settings.analysis_budget_s, settings.analysis_budget_s)
    pid = parcel.parcel_id
    st = adapters.mode
    jobs: dict[str, Callable[[], dict]] = {
        "soil": lambda: compute_soil(adapters.soil_raw(pid, parcel), parcel, tc.mode),
        "terrain": lambda: compute_terrain(adapters.terrain_raw(pid, parcel), parcel, tc.mode),
        "hydrology": lambda: compute_hydrology(adapters.hydrology_raw(pid, parcel), parcel, tc.mode),
        "weather": lambda: compute_weather(adapters.weather_raw(pid, parcel, as_of=tc.as_of_date), parcel,
                                           tc.as_of_date, tc.mode),
        "satellite": lambda: compute_satellite(adapters.satellite_raw(pid, parcel, as_of=tc.as_of_date), parcel,
                                               tc.as_of_date),
    }
    # Budżet czasu analizy (cel ≤ 30 s, kryterium jury): moduł, który nie zdąży, jest oznaczany jako
    # niedostępny; jego pobieranie kończy się w tle i zasila cache, więc ponowna analiza będzie pełniejsza.
    pool = ThreadPoolExecutor(max_workers=5)
    t_start = time.perf_counter()
    futures = {m: pool.submit(_timed, m, fn, st) for m, fn in jobs.items()}
    deadline = time.monotonic() + budget
    modules, timings = {}, {}
    for m, f in futures.items():
        try:
            modules[m], timings[m] = f.result(timeout=max(0.0, deadline - time.monotonic()))
        except FuturesTimeout:
            timings[m] = None  # przekroczony budżet
            modules[m] = unavailable(m, reason_code="analysis_time_budget", source_type=st,
                                     is_demonstration=st == "fixture",
                                     message_pl=f"Źródło nie odpowiedziało w budżecie {budget:.0f} s "
                                                "analizy; pobieranie trwa w tle — ponów analizę za chwilę.")
    pool.shutdown(wait=False)
    modules["satellite"] = apply_canopy_rule(modules["satellite"], modules["weather"], tc.as_of_date)
    timings["_modules_wall"] = round(time.perf_counter() - t_start, 3)

    source_versions = {"data_mode": st, **adapters.source_versions()}
    rules_versions = {"analysis_rules": analysis_rules()["version"], "analysis_rules_status": analysis_rules()["status"],
                      "diagnosis": DIAGNOSIS_VERSION, "app": APP_VERSION}
    key = {"parcel_id": pid, "geometry_version": parcel.geometry_version,
           "time": tc.model_dump(mode="json", exclude={"note_pl", "implementation_start_date", "finance_horizon_years"}),
           "source_versions": source_versions, "rules_versions": rules_versions}
    analysis_id = make_id("an", key)
    payload = {"analysis_id": analysis_id, "analysis_snapshot_id": analysis_id, "parcel_id": pid, "teryt_id": pid,
               "as_of_date": tc.as_of_date.isoformat(), "modules": modules}
    diagnosis = diagnose_parcel(payload)

    statuses = {m: _norm(modules[m].get("status")) for m in MODULES}
    warnings: list[str] = []
    if st == "fixture":
        warnings.append("TRYB TESTOWY (fixture): wszystkie dane są syntetyczne i nie opisują rzeczywistej działki.")
    for m in MODULES:
        if statuses[m] in ("unavailable", "error", "unknown"):
            reason = (modules[m].get("metadata") or {}).get("unavailable_reason") or statuses[m]
            warnings.append(f"Moduł {m}: brak wyniku ({reason}).")
        for w in (modules[m].get("metadata") or {}).get("warnings", []) or []:
            warnings.append(f"Moduł {m}: {w}")
    if tc.mode == "historical":
        warnings.append("Gleba, teren i hydrologia to współczesne warstwy referencyjne, nie stan z dnia analizy.")
    if tc.mode == "future":
        warnings.append("Data przyszła: brak obserwacji; wyniki meteorologiczne nie są prognozą.")
    usable = [m for m in MODULES if statuses[m] in ("ok", "partial")]
    overall = "ok" if all(statuses[m] == "ok" for m in MODULES) else ("partial" if usable else "unavailable")
    return AnalysisSnapshot(
        analysis_id=analysis_id, status=overall, data_mode=st, parcel=parcel, time_context=tc,
        modules=modules, module_statuses=statuses, diagnosis=diagnosis, source_versions=source_versions,
        rules_versions=rules_versions, warnings=list(dict.fromkeys(warnings)), timings_s=timings,
        generated_at=datetime.now(TZ).isoformat(timespec="seconds"))


def _norm(v: Any) -> str:
    from app.models.common import normalize_status
    return normalize_status(v)
