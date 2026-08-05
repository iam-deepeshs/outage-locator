from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
import datetime

from app.db import get_db
from app.models import TelemetryEvent, Pole
from app.schemas import TelemetryIn

router = APIRouter(prefix="/telemetry", tags=["telemetry"])


@router.post("")
def ingest_telemetry(payload: TelemetryIn, db: Session = Depends(get_db)):
    """
    Accepts one telemetry event exactly as pole devices would send it.

    We do NOT reject duplicates or out-of-order messages here — ingest is
    dumb and fast, append-only. Deduplication and ordering happen at read
    time in the localization layer, using (device_id, seq) — see
    ARCHITECTURE.md for why: rejecting at ingest requires a lookup per
    message, which doesn't survive a 5,000-message burst as well as just
    writing everything down and reasoning about it afterward.
    """
    event = TelemetryEvent(
        device_id=payload.device_id,
        pole_id=payload.pole_id,
        event=payload.event,
        energized=payload.energized,
        device_ts=payload.ts,
        received_ts=datetime.datetime.utcnow(),
        seq=payload.seq,
        battery_mv=payload.battery_mv,
        rssi=payload.rssi,
        fw=payload.fw,
    )
    db.add(event)
    db.commit()
    return {"status": "accepted", "id": event.id}


@router.get("/recent")
def recent_telemetry(limit: int = 50, db: Session = Depends(get_db)):
    """Debug helper — see what's landed recently, ordered by our own clock."""
    events = (
        db.query(TelemetryEvent)
        .order_by(TelemetryEvent.received_ts.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": e.id,
            "device_id": e.device_id,
            "pole_id": e.pole_id,
            "event": e.event,
            "energized": e.energized,
            "device_ts": e.device_ts,
            "received_ts": e.received_ts,
            "seq": e.seq,
        }
        for e in events
    ]