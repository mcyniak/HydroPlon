"""HydroPlon — prototyp mechanizmów (FastAPI). Dokumentacja interaktywna: /docs

Uruchomienie (z katalogu głównego repozytorium):
    .venv/Scripts/python -m uvicorn app.main:app --app-dir backend --port 8000
"""
from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import analysis, crops, methods, parcels, plans, system
from app.config import APP_DIR, APP_VERSION, settings
from app.services.errors import HydroPlonError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("hydroplon.api")

app = FastAPI(
    title="HydroPlon — prototyp mechanizmów",
    version=APP_VERSION,
    description=("Działka → 5 usług analitycznych → diagnoza → uprawa → metody → koszty → opłacalność → dotacje → "
                 f"plan wdrożenia → raport. Tryb danych: **{settings.mode}**. "
                 "W trybie fixture wszystkie dane są SYNTETYCZNE i oznaczone is_demonstration=true."),
)


@app.middleware("http")
async def request_id_mw(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    request.state.request_id = rid
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    return response


def _rid(request: Request) -> str:
    return getattr(request.state, "request_id", uuid.uuid4().hex[:12])


@app.exception_handler(HydroPlonError)
async def hydroplon_error(request: Request, exc: HydroPlonError):
    return JSONResponse(status_code=exc.http_status,
                        content={"error_code": exc.error_code, "message_pl": exc.message_pl,
                                 "request_id": _rid(request), "details": exc.details})


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    errors = [{"loc": list(e.get("loc", [])), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"error_code": "validation_error",
                                                  "message_pl": "Niepoprawne dane wejściowe.",
                                                  "request_id": _rid(request), "details": {"errors": errors}})


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("Nieobsłużony błąd (request_id=%s)", _rid(request))
    return JSONResponse(status_code=500, content={"error_code": "internal_error",
                                                  "message_pl": "Wewnętrzny błąd serwera.",
                                                  "request_id": _rid(request), "details": {}})


for r in (system.router, parcels.router, analysis.router, crops.router, methods.router, plans.router):
    app.include_router(r)

# Pliki frontendu /app (app.css, app.js) — edytowalne bez restartu serwera.
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
