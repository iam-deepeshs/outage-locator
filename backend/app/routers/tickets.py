"""
Ticket lifecycle API. States: detected -> acknowledged -> crew_assigned ->
resolved -> verified -> closed.

Key rule from the brief: restoration must be verified from telemetry, not
from a human clicking a button. "resolved" just means a crew CLAIMS the
fix is done; "verified" only happens once the system independently
confirms the affected poles are actually live again. Marking "resolved"
while poles are still dark is rejected outright.
"""

import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Incident, Pole
from app.localization import resolve_pole_states
from app.routers.simulator_control import _sim_clock

router = APIRouter(prefix="/tickets", tags=["tickets"])

VALID_TRANSITIONS = {
    "detected": ["acknowledged"],
    "acknowledged": ["crew_assigned"],
    "crew_assigned": ["resolved"],
    "resolved": ["verified", "crew_assigned"],  # can bounce back if verification fails
    "verified": ["closed"],
    "closed": [],
}


def _affected_pole_ids(db: Session, incident: Incident) -> list[str]:
    if incident.incident_type == "feeder":
        rows = db.query(Pole.pole_id).filter(Pole.feeder_id == incident.feeder_id).all()
    elif incident.incident_type == "dt":
        rows = db.query(Pole.pole_id).filter(Pole.dt_id == incident.dt_id).all()
    else:
        # span -- walk downstream from the dark boundary pole
        from app.simulator import _downstream_poles  # reuse existing tree-walk helper
        dt_poles = db.query(Pole).filter(Pole.dt_id == incident.dt_id).all()
        downstream = _downstream_poles(db, incident.dt_id, incident.boundary_dark_pole_id)
        rows = [(incident.boundary_dark_pole_id,)] + [(p.pole_id,) for p in downstream]
    return [r[0] for r in rows]


@router.get("")
def list_tickets(status: str | None = None, db: Session = Depends(get_db)):
    q = db.query(Incident)
    if status:
        q = q.filter(Incident.status == status)
    incidents = q.order_by(Incident.detected_at.desc()).all()
    return [_to_dict(i) for i in incidents]


@router.get("/{incident_id}")
def get_ticket(incident_id: int, db: Session = Depends(get_db)):
    incident = db.query(Incident).filter(Incident.id == incident_id).first()
    if not incident:
        raise HTTPException(404, "ticket not found")
    return _to_dict(incident)


@router.post("/{incident_id}/acknowledge")
def acknowledge(incident_id: int, db: Session = Depends(get_db)):
    return _transition(db, incident_id, "acknowledged")


@router.post("/{incident_id}/assign-crew")
def assign_crew(incident_id: int, db: Session = Depends(get_db)):
    return _transition(db, incident_id, "crew_assigned")


@router.post("/{incident_id}/mark-resolved")
def mark_resolved(incident_id: int, db: Session = Depends(get_db)):
    """
    Crew claims the fix is done. We do NOT trust this by itself -- we
    check telemetry right now, and if the affected poles are still dark,
    we REJECT the transition and tell the caller why, rather than letting
    a false "resolved" status sit in the system.
    """
    incident = db.query(Incident).filter(Incident.id == incident_id).first()
    if not incident:
        raise HTTPException(404, "ticket not found")

    states = resolve_pole_states(db, _sim_clock["now"])
    affected = _affected_pole_ids(db, incident)
    still_dark = [pid for pid in affected if states.get(pid, {}).get("state") in ("dark", "stale")]

    if still_dark:
        raise HTTPException(
            409,
            f"Cannot mark resolved: {len(still_dark)} of {len(affected)} affected poles "
            f"are still dark/stale per telemetry. Sample: {still_dark[:5]}",
        )

    return _transition(db, incident_id, "resolved")


@router.post("/{incident_id}/verify")
def verify(incident_id: int, db: Session = Depends(get_db)):
    """
    Independent confirmation from telemetry that affected poles are truly
    live again. This is also what the auto-verification background check
    (below) calls -- exposed here too so it can be triggered manually/by
    the UI as a "check now" action.
    """
    incident = db.query(Incident).filter(Incident.id == incident_id).first()
    if not incident:
        raise HTTPException(404, "ticket not found")
    if incident.status != "resolved":
        raise HTTPException(409, f"Can only verify from 'resolved' status, currently '{incident.status}'")

    states = resolve_pole_states(db, _sim_clock["now"])
    affected = _affected_pole_ids(db, incident)
    still_dark = [pid for pid in affected if states.get(pid, {}).get("state") in ("dark", "stale")]

    if still_dark:
        raise HTTPException(
            409,
            f"Verification failed: {len(still_dark)} of {len(affected)} poles still dark/stale. "
            "Ticket remains in 'resolved' pending re-check.",
        )

    incident.verified_at = _sim_clock["now"]
    result = _transition(db, incident_id, "verified")
    return result


@router.post("/{incident_id}/close")
def close(incident_id: int, db: Session = Depends(get_db)):
    return _transition(db, incident_id, "closed")


@router.post("/auto-verify-sweep")
def auto_verify_sweep(db: Session = Depends(get_db)):
    """
    Checks every 'resolved' ticket against current telemetry and
    auto-verifies any whose affected poles are confirmed live. This is
    what makes verification telemetry-driven rather than click-driven --
    intended to be called periodically (e.g. by the same UI action that
    ticks the simulator), not something a human triggers per-ticket.
    """
    resolved_tickets = db.query(Incident).filter(Incident.status == "resolved").all()
    states = resolve_pole_states(db, _sim_clock["now"])
    verified_ids = []

    for incident in resolved_tickets:
        affected = _affected_pole_ids(db, incident)
        still_dark = [pid for pid in affected if states.get(pid, {}).get("state") in ("dark", "stale")]
        if not still_dark:
            incident.status = "verified"
            incident.verified_at = _sim_clock["now"]
            verified_ids.append(incident.id)

    db.commit()
    return {"auto_verified_ticket_ids": verified_ids, "checked": len(resolved_tickets)}


def _transition(db: Session, incident_id: int, new_status: str):
    incident = db.query(Incident).filter(Incident.id == incident_id).first()
    if not incident:
        raise HTTPException(404, "ticket not found")

    allowed = VALID_TRANSITIONS.get(incident.status, [])
    if new_status not in allowed:
        raise HTTPException(
            409, f"Cannot transition from '{incident.status}' to '{new_status}'. Allowed: {allowed}"
        )

    incident.status = new_status
    if new_status == "resolved":
        incident.resolved_at = _sim_clock["now"]
    db.commit()
    return _to_dict(incident)


def _to_dict(i: Incident) -> dict:
    return {
        "id": i.id, "type": i.incident_type, "status": i.status,
        "dt_id": i.dt_id, "feeder_id": i.feeder_id,
        "lat": i.lat, "lon": i.lon, "pincode": i.pincode,
        "affected_pole_count": i.affected_pole_count,
        "confidence": i.confidence, "confidence_reason": i.confidence_reason,
        "localization_type": i.localization_type,
        "detected_at": i.detected_at, "resolved_at": i.resolved_at,
        "verified_at": i.verified_at,
    }
