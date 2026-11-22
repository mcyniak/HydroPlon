from app.engine.diagnosis import diagnose_parcel


def snapshot():
    return {
        "analysis_id": "a-1", "teryt_id": "040301_1.0005.124/2", "as_of_date": "2019-06-15",
        "modules": {
            "soil": {"status": "ok", "retention_class": "niska",
                     "organic_matter": {"low_humus_flag": True},
                     "metadata": {"source": "SoilGrids", "confidence": "orientacyjna"}},
            "terrain": {"status": "ok", "runoff_erosion_risk": "umiarkowane",
                        "metadata": {"source": "NMT"}},
            "weather": {"status": "ok", "target_date": "2019-06-15", "climate_deficit_risk": "wysokie",
                        "metadata": {"data_completeness_pct": 95.0}},
            "hydrology": {"status": "ok", "nearby_water_features": {"nearest_ditch_m": 1.0},
                          "farmer_confirmation_flags": {
                              "subsurface_drainage_status": "unknown_requires_confirmation",
                              "water_abstraction_permit_status": "unknown_requires_confirmation",
                          }},
            "satellite": {"status": "ok", "target_date": "2019-06-15",
                          "latest_valid_observation": {"acquisition_date": "2019-06-11"},
                          "baseline_comparison": {"canopy_moisture_drop_detected": True}},
        },
    }


def test_diagnosis_is_reproducible_and_crop_independent():
    original = snapshot()
    result = diagnose_parcel(original)
    assert result == diagnose_parcel(original)
    assert {p["problem_id"] for p in result["problems"]} == {
        "low_soil_retention", "low_organic_matter", "periodic_water_deficit", "surface_runoff_erosion"
    }
    assert "crop_id" not in result
    assert result["status"] == "ok"
    assert all(p["evidence_status"] == "estimated" for p in result["problems"])


def test_unknown_drainage_and_rights_not_assumed_false_or_true():
    result = diagnose_parcel(snapshot())
    constraints = {x["constraint_id"]: x for x in result["constraints"]}
    assert constraints["subsurface_drainage_unverified"]["status"] == "unknown"
    assert constraints["water_abstraction_right_unverified"]["status"] == "unknown"
    assert "uncontrolled_drainage_outflow" not in {p["problem_id"] for p in result["problems"]}


def test_satellite_only_supports_vegetation_signal():
    result = diagnose_parcel(snapshot())
    assert result["supporting_signals"][0]["signal_id"] == "vegetation_moisture_anomaly"
    assert "water_stress_confirmed" not in {p["problem_id"] for p in result["problems"]}


def test_future_observation_is_excluded():
    s = snapshot()
    s["modules"]["satellite"]["latest_valid_observation"]["acquisition_date"] = "2020-06-15"
    s["modules"]["weather"]["target_date"] = "2020-01-01"
    r = diagnose_parcel(s)
    assert r["status"] == "partial"
    assert set(r["missing_modules"]) == {"weather", "satellite"}
    assert not r["supporting_signals"]
    assert "periodic_water_deficit" not in {x["problem_id"] for x in r["problems"]}


def test_no_sources_is_not_zero_or_no_problem_proof():
    r = diagnose_parcel({"as_of_date": "2019-06-15", "modules": {}})
    assert r["status"] == "no_data"
    assert r["problems"] == []
    assert len(r["missing_modules"]) == 5
