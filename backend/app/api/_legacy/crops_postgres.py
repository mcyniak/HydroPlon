"""Etap 3.3. Router FastAPI do wpiecia w istniejacy backend HydroPlon.

Wymaga projektu app.db.session oraz tabel analysis_runs / crop_assessments.
Ten plik nie tworzy samodzielnego serwera ani DB. Schemat migracji w docs.
"""
from __future__ import annotations

import json
import uuid
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.session import get_db  # w istniejacym backendzie
from app.engine.crop_water_demand import ENGINE_VERSION, evaluate_crop_water_demand

router = APIRouter(prefix="/api/crops", tags=["Crops"])
CROPS_PATH = Path(__file__).resolve().parent.parent / "data" / "crops.json"


@lru_cache(maxsize=1)
def _catalog() -> dict:
    data = json.loads(CROPS_PATH.read_text(encoding="utf-8"))
    ids = [c["crop_id"] for c in data["crops"]]
    if len(set(ids)) != len(ids):
        raise RuntimeError("Niejednoznaczne crop_id w katalogu")
    return data


class CropAssessmentRequest(BaseModel):
    analysis_id: uuid.UUID
    crop_id: str


@router.get("")
def list_available_crops():
    cat = _catalog()
    return {
        "status": "ok", "catalog_version": cat["version"],
        "count": len(cat["crops"]),
        "crops": [{
            "crop_id": c["crop_id"],
            "name_pl": c["name_pl"],
            "category": c.get("category"),
            "water_sensitivity_class": c.get("water_sensitivity_class", "nieustalona"),
            "critical_stage_label_pl": c.get("critical_stage_label_pl"),
            "parameter_status": c.get("parameter_status"),
            "available_for_assessment": c.get("parameter_status") in ("validated", "provisional"),
            "agronomic_notes_pl": c.get("agronomic_notes_pl"),
        } for c in cat["crops"]],
    }


@router.post("/assess")
def assess_crop(req: CropAssessmentRequest, db: Session = Depends(get_db)):
    cat = _catalog()
    crop = next((c for c in cat["crops"] if c["crop_id"] == req.crop_id), None)
    if crop is None:
        raise HTTPException(status_code=404, detail="Nieznany identyfikator uprawy")
    if crop.get("parameter_status") not in ("validated", "provisional"):
        raise HTTPException(status_code=422, detail="Parametry tej uprawy czekaja na weryfikacje; nie obliczamy fikcyjnych wynikow")

    key = {"analysis_id": str(req.analysis_id), "crop_id": req.crop_id,
           "catalog_version": cat["version"], "engine_version": ENGINE_VERSION}
    query = text("""SELECT assessment_id, result_payload FROM crop_assessments
        WHERE analysis_id = CAST(:analysis_id AS UUID) AND crop_id = :crop_id
          AND crop_catalog_version = :catalog_version AND engine_version = :engine_version""")
    cached = db.execute(query, key).mappings().first()
    if cached:
        return {"crop_assessment_id": str(cached["assessment_id"]), **cached["result_payload"]}

    row = db.execute(text("""SELECT result_payload FROM analysis_runs
        WHERE analysis_id = CAST(:analysis_id AS UUID)"""), key).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Nie znaleziono analizy dzialki")
    snapshot = row["result_payload"]
    try:
        result = evaluate_crop_water_demand(snapshot, crop)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    new_id = uuid.uuid4()
    insert = text("""INSERT INTO crop_assessments (
         assessment_id, analysis_id, crop_id, crop_catalog_version,
         engine_version, result_payload)
      VALUES (CAST(:assessment_id AS UUID), CAST(:analysis_id AS UUID),
              :crop_id, :catalog_version, :engine_version, CAST(:payload AS JSONB))
      ON CONFLICT (analysis_id, crop_id, crop_catalog_version, engine_version) DO NOTHING
      RETURNING assessment_id""")
    inserted = db.execute(insert, {**key,"assessment_id": str(new_id),
                                   "payload": json.dumps(result,ensure_ascii=False)}).scalar_one_or_none()
    db.commit()
    if inserted is not None:
        return {"crop_assessment_id": str(inserted), **result}
    # Inna instancja w tym czasie utworzyla taki sam wynik.
    concurrent = db.execute(query, key).mappings().first()
    if concurrent is None:
        raise HTTPException(status_code=503,detail="Nie udalo sie odczytac wyniku po zapisie")
    return {"crop_assessment_id":str(concurrent["assessment_id"]),**concurrent["result_payload"]}
