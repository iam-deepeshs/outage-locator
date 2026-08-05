"""
Unit tests for the localization engine, against known toy topologies.

Per the deliverables doc: "If you test one thing, test that a known fault
in a known topology produces the expected span." These tests build small,
fully-known networks in memory (SQLite) and inject exact telemetry states,
rather than relying on the simulator's randomness -- so results are
deterministic and the expected answer is known in advance.
"""

import datetime
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Feeder, Transformer, Pole, TelemetryEvent
from app.localization import detect_all_boundaries, resolve_pole_states


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _add_pole(db, pole_id, dt_id, feeder_id, parent_id=None, device=True, lat=12.0, lon=77.0):
    p = Pole(
        pole_id=pole_id, lat=lat, lon=lon, dt_id=dt_id, feeder_id=feeder_id,
        parent_pole_id=parent_id, topology_source="known",
        device_id=f"DEV-{pole_id}" if device else None,
        pincode="560001",
    )
    db.add(p)
    return p


def _heartbeat(db, pole_id, energized, seq, ts):
    db.add(TelemetryEvent(
        device_id=f"DEV-{pole_id}", pole_id=pole_id, event="heartbeat" if energized else "power_lost",
        energized=energized, device_ts=ts, seq=seq,
    ))


def _simple_line_network(db):
    """DT -> P1 -> P2 -> P3 -> P4 (known topology, straight line)."""
    db.add(Feeder(feeder_id="F1", substation_id="SS1"))
    db.add(Transformer(dt_id="DT1", feeder_id="F1", lat=12.0, lon=77.0, capacity_kva=100, households_served=50))
    _add_pole(db, "P1", "DT1", "F1", parent_id=None)
    _add_pole(db, "P2", "DT1", "F1", parent_id="P1")
    _add_pole(db, "P3", "DT1", "F1", parent_id="P2")
    _add_pole(db, "P4", "DT1", "F1", parent_id="P3")
    db.commit()


def test_no_fault_no_boundaries(db):
    """A fully healthy line should produce zero boundaries."""
    _simple_line_network(db)
    now = datetime.datetime(2026, 1, 1, 12, 0, 0)
    for pid in ["P1", "P2", "P3", "P4"]:
        _heartbeat(db, pid, True, 1, now)
    db.commit()

    result = detect_all_boundaries(db, now, include_suppressed=False)
    assert result == []


def test_known_span_fault_produces_exact_span(db):
    """
    P1, P2 live; P3, P4 dark. Known topology -> must produce exactly one
    span_exact boundary at P2 (live) / P3 (dark), affecting P3 and P4.
    """
    _simple_line_network(db)
    now = datetime.datetime(2026, 1, 1, 12, 0, 0)
    _heartbeat(db, "P1", True, 1, now)
    _heartbeat(db, "P2", True, 1, now)
    _heartbeat(db, "P3", False, 1, now)
    _heartbeat(db, "P4", False, 1, now)
    db.commit()

    result = detect_all_boundaries(db, now, include_suppressed=False)
    assert len(result) == 1
    b = result[0]
    assert b["type"] == "span_exact"
    assert b["live_boundary_pole_id"] == "P2"
    assert b["dark_boundary_pole_id"] == "P3"
    assert set(b["affected_pole_ids"]) == {"P3", "P4"}


def test_whole_dt_fault_when_all_dark(db):
    """Every pole dark, none live -> single dt-type boundary."""
    _simple_line_network(db)
    now = datetime.datetime(2026, 1, 1, 12, 0, 0)
    for pid in ["P1", "P2", "P3", "P4"]:
        _heartbeat(db, pid, False, 1, now)
    db.commit()

    result = detect_all_boundaries(db, now, include_suppressed=False)
    assert len(result) == 1
    assert result[0]["type"] == "dt"
    assert set(result[0]["affected_pole_ids"]) == {"P1", "P2", "P3", "P4"}


def test_isolated_stale_pole_with_live_children_is_sensor_fault(db):
    """
    P2 goes silent (stale) but its child P3 (and grandchild P4) are still
    live -- physically impossible as a real fault. Must be classified as
    sensor_fault, not a ticketed boundary.
    """
    _simple_line_network(db)
    now = datetime.datetime(2026, 1, 1, 12, 0, 0)
    old_ts = now - datetime.timedelta(minutes=60)  # long silence -> stale
    _heartbeat(db, "P1", True, 1, now)
    _heartbeat(db, "P2", True, 1, old_ts)   # stale: last event long ago
    _heartbeat(db, "P3", True, 1, now)      # still live!
    _heartbeat(db, "P4", True, 1, now)      # still live!
    db.commit()

    result = detect_all_boundaries(db, now, include_suppressed=False)
    # Filtered boundaries (include_suppressed=False) still include
    # sensor_fault entries when queried via detect_all_boundaries directly,
    # since sensor faults aren't part of suppression filtering -- check
    # via detect_boundaries_for_dt-level typing instead.
    types = [b["type"] for b in result]
    assert "span_exact" not in types
    assert "span_range" not in types
    assert "dt" not in types
    assert "sensor_fault" in types


def test_multiple_simultaneous_faults_not_merged(db):
    """
    Two independent DTs each with their own fault must produce two
    separate incidents, not one merged incident and not zero.
    """
    db.add(Feeder(feeder_id="F1", substation_id="SS1"))
    db.add(Transformer(dt_id="DT1", feeder_id="F1", lat=12.0, lon=77.0, capacity_kva=100, households_served=50))
    db.add(Transformer(dt_id="DT2", feeder_id="F1", lat=13.0, lon=78.0, capacity_kva=100, households_served=50))
    _add_pole(db, "A1", "DT1", "F1", parent_id=None)
    _add_pole(db, "A2", "DT1", "F1", parent_id="A1")
    _add_pole(db, "B1", "DT2", "F1", parent_id=None)
    _add_pole(db, "B2", "DT2", "F1", parent_id="B1")
    db.commit()

    now = datetime.datetime(2026, 1, 1, 12, 0, 0)
    _heartbeat(db, "A1", True, 1, now)
    _heartbeat(db, "A2", False, 1, now)
    _heartbeat(db, "B1", True, 1, now)
    _heartbeat(db, "B2", False, 1, now)
    db.commit()

    result = detect_all_boundaries(db, now, include_suppressed=False)
    assert len(result) == 2
    dt_ids = {b["dt_id"] for b in result}
    assert dt_ids == {"DT1", "DT2"}
