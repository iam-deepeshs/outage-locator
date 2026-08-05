from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from typing import Optional

from app.db import get_db
from app.models import Pole, Transformer
from app.schemas import PoleOut, TransformerOut

router = APIRouter(prefix="/network", tags=["network"])


@router.get("/transformers", response_model=list[TransformerOut])
def list_transformers(db: Session = Depends(get_db)):
    return db.query(Transformer).all()


@router.get("/poles", response_model=list[PoleOut])
def list_poles(
    dt_id: Optional[str] = Query(None, description="Filter to poles under one transformer"),
    db: Session = Depends(get_db),
):
    q = db.query(Pole)
    if dt_id:
        q = q.filter(Pole.dt_id == dt_id)
    return q.all()


@router.get("/stats")
def network_stats(db: Session = Depends(get_db)):
    total_poles = db.query(Pole).count()
    total_transformers = db.query(Transformer).count()
    known = db.query(Pole).filter(Pole.topology_source == "known").count()
    inferred = db.query(Pole).filter(Pole.topology_source == "inferred").count()
    no_device = db.query(Pole).filter(Pole.device_id.is_(None)).count()
    return {
        "total_poles": total_poles,
        "total_transformers": total_transformers,
        "topology_known_poles": known,
        "topology_inferred_poles": inferred,
        "poles_without_device": no_device,
    }