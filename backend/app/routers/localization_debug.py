"""
Debug/inspection endpoints for the localization engine's intermediate
steps. Not part of the "real" product API -- useful for verifying each
piece works before wiring it into the actual incident pipeline.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.localization import resolve_pole_states, summarize_states, detect_all_boundaries
from app.routers.simulator_control import _sim_clock

router = APIRouter(prefix="/debug/localization", tags=["localization-debug"])


@router.get("/pole-states")
def pole_states(db: Session = Depends(get_db)):
    states = resolve_pole_states(db, _sim_clock["now"])
    return {
        "sim_time": _sim_clock["now"].isoformat(),
        "summary": summarize_states(states),
        "states": states,
    }


@router.get("/boundaries")
def boundaries(include_suppressed: bool = True, db: Session = Depends(get_db)):
    result = detect_all_boundaries(db, _sim_clock["now"], include_suppressed=include_suppressed)
    active_count = sum(1 for r in result if not r.get("suppressed", False))
    suppressed_count = sum(1 for r in result if r.get("suppressed", False))
    return {
        "sim_time": _sim_clock["now"].isoformat(),
        "total_count": len(result),
        "active_count": active_count,
        "suppressed_count": suppressed_count,
        "boundaries": result,
    }

from app.localization import sync_incidents_from_boundaries
from app.models import Incident


@router.post("/run")
def run_localization(db: Session = Depends(get_db)):
    """Triggers a full localization pass and syncs results into Incidents."""
    return sync_incidents_from_boundaries(db, _sim_clock["now"])


@router.get("/incidents")
def list_incidents(db: Session = Depends(get_db)):
    incidents = db.query(Incident).order_by(Incident.detected_at.desc()).all()
    return [
        {
            "id": i.id, "type": i.incident_type, "status": i.status,
            "dt_id": i.dt_id, "feeder_id": i.feeder_id,
            "lat": i.lat, "lon": i.lon, "pincode": i.pincode,
            "affected_pole_count": i.affected_pole_count,
            "confidence": i.confidence, "confidence_reason": i.confidence_reason,
            "localization_type": i.localization_type,
            "detected_at": i.detected_at,
        }
        for i in incidents
    ]