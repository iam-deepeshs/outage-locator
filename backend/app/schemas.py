from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class PoleOut(BaseModel):
    pole_id: str
    lat: float
    lon: float
    dt_id: str
    feeder_id: str
    parent_pole_id: Optional[str] = None            # known, from registry
    inferred_parent_pole_id: Optional[str] = None    # our MST guess
    topology_source: str
    topology_confidence: Optional[float] = None
    device_id: Optional[str] = None
    pincode: Optional[str] = None

    class Config:
        from_attributes = True


class TransformerOut(BaseModel):
    dt_id: str
    feeder_id: str
    lat: float
    lon: float
    capacity_kva: Optional[int] = None
    households_served: Optional[int] = None

    class Config:
        from_attributes = True



class TelemetryIn(BaseModel):
    device_id: str
    pole_id: str
    event: str          # heartbeat | power_lost | power_restored | boot
    energized: bool
    ts: datetime
    seq: int
    battery_mv: int | None = None
    rssi: int | None = None
    fw: str | None = None