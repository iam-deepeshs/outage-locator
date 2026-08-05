from sqlalchemy import (
    Column, String, Float, Integer, Boolean, DateTime, ForeignKey, Text
)
from sqlalchemy.orm import declarative_base, relationship
import datetime

Base = declarative_base()


class Feeder(Base):
    __tablename__ = "feeders"
    feeder_id = Column(String, primary_key=True)          # e.g. F-07-03
    substation_id = Column(String, nullable=False)

    transformers = relationship("Transformer", back_populates="feeder")


class Transformer(Base):
    __tablename__ = "transformers"
    dt_id = Column(String, primary_key=True)              # e.g. D-0112
    feeder_id = Column(String, ForeignKey("feeders.feeder_id"), nullable=False)
    lat = Column(Float, nullable=False)
    lon = Column(Float, nullable=False)
    capacity_kva = Column(Integer)
    households_served = Column(Integer)

    feeder = relationship("Feeder", back_populates="transformers")
    poles = relationship("Pole", back_populates="transformer")


class Pole(Base):
    __tablename__ = "poles"
    pole_id = Column(String, primary_key=True)            # e.g. P-024431
    lat = Column(Float, nullable=False)
    lon = Column(Float, nullable=False)
    feeder_id = Column(String, ForeignKey("feeders.feeder_id"), nullable=False)
    dt_id = Column(String, ForeignKey("transformers.dt_id"), nullable=False)

    # Raw registry fields — NULL exactly where the brief says they're missing
    seq_on_line = Column(Integer, nullable=True)           # missing for ~60% of DTs
    parent_pole_id = Column(String, ForeignKey("poles.pole_id"), nullable=True)

    pole_type = Column(String)
    ward = Column(String)
    pincode = Column(String, nullable=True)                # missing ~3%
    device_id = Column(String, nullable=True)               # missing ~9% (no device)

    # --- Derived topology fields (filled by our inference step, Step 3) ---
    inferred_parent_pole_id = Column(String, nullable=True)
    topology_source = Column(String, default="unknown")     # "known" | "inferred"
    topology_confidence = Column(Float, nullable=True)      # 0..1, only for inferred

    transformer = relationship("Transformer", back_populates="poles")


class TelemetryEvent(Base):
    """Raw ingested telemetry — append-only, mirrors the device payload exactly."""
    __tablename__ = "telemetry_events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    device_id = Column(String, nullable=False)
    pole_id = Column(String, ForeignKey("poles.pole_id"), nullable=False)
    event = Column(String, nullable=False)                 # heartbeat|power_lost|power_restored|boot
    energized = Column(Boolean, nullable=False)
    device_ts = Column(DateTime, nullable=False)            # the device's own clock (skewed)
    received_ts = Column(DateTime, default=datetime.datetime.utcnow)  # our server clock, trustworthy
    seq = Column(Integer, nullable=False)
    battery_mv = Column(Integer)
    rssi = Column(Integer)
    fw = Column(String)


class ScheduledOutage(Base):
    __tablename__ = "scheduled_outages"
    id = Column(String, primary_key=True)                  # e.g. SO-2026-07-29-014
    scope = Column(String, nullable=False)                 # "feeder" | "dt"
    target_id = Column(String, nullable=False)
    start = Column(DateTime, nullable=False)
    end = Column(DateTime, nullable=False)
    reason = Column(Text)


class Incident(Base):
    """A localized, grouped fault — the output of the localization algorithm."""
    __tablename__ = "incidents"
    id = Column(Integer, primary_key=True, autoincrement=True)
    incident_type = Column(String, nullable=False)          # "span" | "dt" | "feeder" | "sensor_fault"
    status = Column(String, default="detected")             # detected|acknowledged|crew_assigned|resolved|verified|closed

    # Localization output
    boundary_live_pole_id = Column(String, nullable=True)
    boundary_dark_pole_id = Column(String, nullable=True)
    dt_id = Column(String, nullable=True)
    feeder_id = Column(String, nullable=True)
    lat = Column(Float)
    lon = Column(Float)
    pincode = Column(String, nullable=True)
    affected_pole_count = Column(Integer)
    confidence = Column(Float)
    confidence_reason = Column(Text)
    localization_type = Column(String)                      # "span_exact" | "span_range" | "dt_level"

    detected_at = Column(DateTime, default=datetime.datetime.utcnow)
    resolved_at = Column(DateTime, nullable=True)
    verified_at = Column(DateTime, nullable=True)
    pin_code_ticket = Column(String, nullable=True)          # crew-facing PIN for the ticket workflow