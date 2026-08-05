"""
Telemetry simulator.

Step 2 (this file, for now): baseline heartbeat traffic for a healthy
network — every pole with a device sends heartbeat every 15min +/- 45s
jitter, per 02-data-and-systems.md Section 2.

Later steps (same file, added incrementally): fault injection (span/DT/
feeder), noise injection (dead sensor, duplicates, out-of-order, scheduled
outage), and repair/restoration.

Run modes:
- `run_heartbeat_tick()`: sends ONE round of heartbeats for poles that are
  "due" — meant to be called repeatedly (e.g. every few seconds) to simulate
  a live network without actually needing to wait 15 real minutes per pole.
- For this exercise we compress simulated time: each call advances a virtual
  clock rather than using wall-clock time, so a full 15-minute cycle across
  the whole network can be observed in seconds when driven from the API/UI.
"""

import random
import datetime
from app.db import SessionLocal
from app.models import Pole, TelemetryEvent

random.seed(7)

# Per-device state we need to track between ticks: last seq number sent,
# and simulated "next heartbeat due" time. Kept in memory for simplicity —
# this is a simulator, not the production system, and is explicitly scoped
# as throwaway/test infrastructure in ARCHITECTURE.md.
_device_state = {}  # device_id -> {"seq": int, "next_due": datetime, "energized": bool}

SIM_START = datetime.datetime(2026, 8, 2, 6, 0, 0)


def _ensure_state_initialized(db):
    if _device_state:
        return
    poles = db.query(Pole).filter(Pole.device_id.isnot(None)).all()
    for p in poles:
        _device_state[p.device_id] = {
            "pole_id": p.pole_id,
            "seq": 0,
            "next_due": SIM_START + datetime.timedelta(seconds=random.uniform(0, 900)),
            "energized": True,
        }


def run_heartbeat_tick(sim_now: datetime.datetime, db=None):
    """
    Sends heartbeat telemetry for every device whose next_due time has
    passed, given the current simulated time `sim_now`. Advances each
    fired device's next_due by 15min +/- 45s jitter.

    Returns the number of heartbeat events sent this tick.
    """
    owns_session = db is None
    if owns_session:
        db = SessionLocal()

    try:
        _ensure_state_initialized(db)

        sent = 0
        for device_id, state in _device_state.items():
            if not state["energized"]:
                continue  # dark poles don't heartbeat — handled in fault injection step
            if state["next_due"] > sim_now:
                continue

            state["seq"] += 1
            event = TelemetryEvent(
                device_id=device_id,
                pole_id=state["pole_id"],
                event="heartbeat",
                energized=True,
                device_ts=sim_now + datetime.timedelta(seconds=random.uniform(-2, 2)),
                received_ts=datetime.datetime.utcnow(),
                seq=state["seq"],
                battery_mv=random.randint(3400, 3700),
                rssi=random.randint(-100, -60),
                fw=random.choice(["1.4.2"] * 92 + ["1.2.7"] * 8),  # ~8% on old firmware
            )
            db.add(event)
            sent += 1

            jitter = random.uniform(-45, 45)
            state["next_due"] = sim_now + datetime.timedelta(minutes=15, seconds=jitter)

        db.commit()
        return sent
    finally:
        if owns_session:
            db.close()


def reset_simulator_state():
    """Clears in-memory device state — call when resetting the simulator."""
    _device_state.clear()


if __name__ == "__main__":
    # Manual test: run a handful of ticks advancing simulated time by 5 min each,
    # to confirm heartbeats fire as devices come "due."
    db = SessionLocal()
    try:
        sim_time = SIM_START
        for i in range(6):
            sim_time += datetime.timedelta(minutes=5)
            n = run_heartbeat_tick(sim_time, db=db)
            print(f"tick {i+1} (sim_time={sim_time}): {n} heartbeats sent")
    finally:
        db.close()


# ============================================================
# Step 4a: Fault injection — span, DT, feeder
# ============================================================

from app.models import Transformer


def _downstream_poles(db, dt_id: str, boundary_pole_id: str | None):
    """
    Returns all poles electrically downstream of `boundary_pole_id` within
    the given DT's tree, using whichever topology source is available
    (known parent_pole_id, else inferred_parent_pole_id).

    If boundary_pole_id is None, returns ALL poles under the DT (used for
    DT-level faults, where nothing downstream survives).
    """
    poles = db.query(Pole).filter(Pole.dt_id == dt_id).all()
    by_id = {p.pole_id: p for p in poles}

    def parent_of(p):
        return p.parent_pole_id or p.inferred_parent_pole_id

    if boundary_pole_id is None:
        return poles

    # Build children map, then BFS/DFS downstream from the boundary pole
    children = {}
    for p in poles:
        parent_id = parent_of(p)
        if parent_id:
            children.setdefault(parent_id, []).append(p)

    result = []
    stack = list(children.get(boundary_pole_id, []))
    while stack:
        p = stack.pop()
        result.append(p)
        stack.extend(children.get(p.pole_id, []))
    return result


def inject_span_fault(db, dt_id: str, sim_now: datetime.datetime):
    """
    Breaks a random span within dt_id. Everything downstream goes dark.
    Returns a dict describing what was actually broken (ground truth, for
    your own testing/validation — the localization algorithm must NOT be
    given this directly).
    """
    poles = db.query(Pole).filter(Pole.dt_id == dt_id).all()
    candidates = [p for p in poles if (p.parent_pole_id or p.inferred_parent_pole_id)]
    if not candidates:
        return {"error": f"no poles with a parent found under {dt_id}"}

    dark_boundary_pole = random.choice(candidates)
    live_boundary_pole_id = dark_boundary_pole.parent_pole_id or dark_boundary_pole.inferred_parent_pole_id

    affected = [dark_boundary_pole] + _downstream_poles(db, dt_id, dark_boundary_pole.pole_id)

    _go_dark(db, affected, sim_now)

    return {
        "fault_type": "span",
        "dt_id": dt_id,
        "boundary_live_pole_id": live_boundary_pole_id,
        "boundary_dark_pole_id": dark_boundary_pole.pole_id,
        "affected_pole_count": len(affected),
        "affected_pole_ids": [p.pole_id for p in affected],
    }


def inject_dt_fault(db, dt_id: str, sim_now: datetime.datetime):
    """DT/HT-fuse fault: every pole under this DT goes dark, no survivors."""
    affected = db.query(Pole).filter(Pole.dt_id == dt_id).all()
    _go_dark(db, affected, sim_now)
    return {
        "fault_type": "dt",
        "dt_id": dt_id,
        "affected_pole_count": len(affected),
        "affected_pole_ids": [p.pole_id for p in affected],
    }


def inject_feeder_fault(db, feeder_id: str, sim_now: datetime.datetime):
    """Feeder fault: every pole under every DT on this feeder goes dark."""
    affected = db.query(Pole).filter(Pole.feeder_id == feeder_id).all()
    _go_dark(db, affected, sim_now)
    return {
        "fault_type": "feeder",
        "feeder_id": feeder_id,
        "affected_pole_count": len(affected),
        "affected_pole_ids": [p.pole_id for p in affected],
    }


def _go_dark(db, poles: list[Pole], sim_now: datetime.datetime):
    """
    Produces the telemetry a real fault would cause for the given poles,
    respecting the messy reality from 02-data-and-systems.md:
    - No device at all (~9%): sends nothing, obviously.
    - Firmware 1.2.x (~8% of fleet): never sends power_lost, just goes
      silent — simulated by NOT sending an event and NOT updating state,
      so heartbeats simply stop appearing on future ticks.
    - Firmware >=1.3: attempts one power_lost message, succeeds ~70% of
      the time (capacitor reserve may be too low, or the radio busy).
    """
    for p in poles:
        if not p.device_id or p.device_id not in _device_state:
            continue  # no device fitted — genuinely silent, nothing to send

        state = _device_state[p.device_id]
        state["energized"] = False  # stops future heartbeats regardless of firmware

        is_old_firmware = random.random() < 0.08
        if is_old_firmware:
            continue  # firmware 1.2.x: goes quiet, no power_lost message ever

        dying_message_succeeds = random.random() < 0.70
        if not dying_message_succeeds:
            continue  # capacitor too low / radio busy — silent death

        state["seq"] += 1
        event = TelemetryEvent(
            device_id=p.device_id,
            pole_id=p.pole_id,
            event="power_lost",
            energized=False,
            device_ts=sim_now + datetime.timedelta(seconds=random.uniform(-2, 2)),
            received_ts=datetime.datetime.utcnow(),
            seq=state["seq"],
            battery_mv=random.randint(3200, 3480),  # reserve draining
            rssi=random.randint(-100, -60),
            fw="1.4.2",
        )
        db.add(event)
    db.commit()


def repair_poles(db, affected_pole_ids: list[str], sim_now: datetime.datetime):
    """
    Restores power: sends boot + power_restored for every device that had
    one, within ~20s of each other, per spec. Poles with no device simply
    become live again with no telemetry (as in reality).
    """
    for pole_id in affected_pole_ids:
        pole = db.query(Pole).filter(Pole.pole_id == pole_id).first()
        if not pole or not pole.device_id or pole.device_id not in _device_state:
            continue

        state = _device_state[pole.device_id]
        state["energized"] = True

        state["seq"] += 1
        boot_event = TelemetryEvent(
            device_id=pole.device_id,
            pole_id=pole.pole_id,
            event="boot",
            energized=True,
            device_ts=sim_now,
            received_ts=datetime.datetime.utcnow(),
            seq=state["seq"],
            battery_mv=random.randint(3500, 3700),
            rssi=random.randint(-100, -60),
            fw="1.4.2",
        )
        db.add(boot_event)

        state["seq"] += 1
        restored_event = TelemetryEvent(
            device_id=pole.device_id,
            pole_id=pole.pole_id,
            event="power_restored",
            energized=True,
            device_ts=sim_now + datetime.timedelta(seconds=random.uniform(5, 20)),
            received_ts=datetime.datetime.utcnow(),
            seq=state["seq"],
            battery_mv=random.randint(3500, 3700),
            rssi=random.randint(-100, -60),
            fw="1.4.2",
        )
        db.add(restored_event)

        # Resume normal heartbeat cadence from now
        state["next_due"] = sim_now + datetime.timedelta(minutes=15, seconds=random.uniform(-45, 45))

    db.commit()

# ============================================================
# Step 4c: Noise injection — dead sensor, scheduled outage, dup/out-of-order
# ============================================================

from app.models import ScheduledOutage


def inject_dead_sensor(db, pole_id: str, sim_now: datetime.datetime):
    """
    Simulates a device that stops reporting for reasons UNRELATED to power
    — vandalism, expired SIM, water ingress. The pole itself stays
    energized; only the sensor goes quiet. This is the key noise case: an
    isolated dark pole with live children downstream is not a real fault.

    We simulate this simply by marking the device as no longer heartbeating
    while leaving `energized` state at the physical/topology level
    untouched — i.e. this device just silently stops appearing in future
    heartbeat ticks. No power_lost message is sent, because the device
    doesn't know it's about to go dark — it's not losing power, its radio
    or modem is failing on its own.
    """
    pole = db.query(Pole).filter(Pole.pole_id == pole_id).first()
    if not pole or not pole.device_id or pole.device_id not in _device_state:
        return {"error": f"pole {pole_id} has no active device to fail"}

    state = _device_state[pole.device_id]
    state["energized"] = False  # stops heartbeats — but note NO power_lost sent,
                                 # and the pole's real-world power state is untouched.
                                 # Downstream poles (if any) keep heartbeating normally,
                                 # which is exactly the tell that this is a sensor
                                 # fault, not a line fault.
    return {
        "noise_type": "dead_sensor",
        "pole_id": pole_id,
        "device_id": pole.device_id,
        "note": "device silenced, no power_lost sent, pole remains physically energized",
    }


def inject_scheduled_outage(db, scope: str, target_id: str, sim_now: datetime.datetime,
                             duration_minutes: int = 60, overrun_minutes: int = 0,
                             cancelled_but_not_updated: bool = False):
    """
    Registers a scheduled outage in the scheduled_outages table (as the
    mocked department feed would show), and — unless cancelled_but_not_updated
    is True — actually darkens the relevant poles to simulate the real
    planned shutdown.

    `overrun_minutes` simulates the spec's "shutdowns start late and overrun
    by 20-40 minutes routinely" — the feed's `end` time undershoots reality.

    `cancelled_but_not_updated` simulates "about one in ten is cancelled
    without the feed being updated" — the feed says an outage happened, but
    no poles actually went dark. This tests whether your localization
    algorithm blindly trusts the feed or checks telemetry.
    """
    so_id = f"SO-SIM-{sim_now.strftime('%Y%m%d%H%M%S')}-{random.randint(1000,9999)}"
    end_time = sim_now + datetime.timedelta(minutes=duration_minutes)
    record = ScheduledOutage(
        id=so_id,
        scope=scope,
        target_id=target_id,
        start=sim_now,
        end=end_time,
        reason="Simulated load shedding" if scope == "dt" else "Simulated planned maintenance",
    )
    db.add(record)
    db.commit()

    if cancelled_but_not_updated:
        return {
            "noise_type": "scheduled_outage_cancelled_silently",
            "scheduled_outage_id": so_id,
            "note": "feed shows this outage, but no poles were actually darkened",
        }

    # Actually darken the poles, with the realistic overrun
    if scope == "dt":
        affected = db.query(Pole).filter(Pole.dt_id == target_id).all()
    elif scope == "feeder":
        affected = db.query(Pole).filter(Pole.feeder_id == target_id).all()
    else:
        return {"error": f"unknown scope {scope}"}

    _go_dark(db, affected, sim_now)

    return {
        "noise_type": "scheduled_outage",
        "scheduled_outage_id": so_id,
        "scope": scope,
        "target_id": target_id,
        "feed_end_time": end_time.isoformat(),
        "actual_overrun_minutes": overrun_minutes,
        "affected_pole_count": len(affected),
        "affected_pole_ids": [p.pole_id for p in affected],
    }


def inject_duplicate_and_out_of_order(db, pole_id: str, sim_now: datetime.datetime):
    """
    Re-sends the most recent event for a pole's device, but arriving late
    and with a jittered/skewed device_ts — simulating the spec's
    at-least-once delivery and up-to-90s clock skew. Useful for testing
    that your localization/dedup logic uses (device_id, seq) correctly
    rather than trusting arrival order.
    """
    pole = db.query(Pole).filter(Pole.pole_id == pole_id).first()
    if not pole or not pole.device_id:
        return {"error": f"pole {pole_id} has no device"}

    last_event = (
        db.query(TelemetryEvent)
        .filter(TelemetryEvent.device_id == pole.device_id)
        .order_by(TelemetryEvent.seq.desc())
        .first()
    )
    if not last_event:
        return {"error": f"no prior telemetry for {pole.device_id} to duplicate"}

    # Re-send with the SAME seq (true duplicate) but skewed device_ts and a
    # much later received_ts (simulating a retried delivery arriving stale)
    dup = TelemetryEvent(
        device_id=last_event.device_id,
        pole_id=last_event.pole_id,
        event=last_event.event,
        energized=last_event.energized,
        device_ts=last_event.device_ts + datetime.timedelta(seconds=random.uniform(-90, 90)),
        received_ts=datetime.datetime.utcnow(),
        seq=last_event.seq,  # SAME seq — true duplicate, dedup logic must catch this
        battery_mv=last_event.battery_mv,
        rssi=last_event.rssi,
        fw=last_event.fw,
    )
    db.add(dup)
    db.commit()

    return {
        "noise_type": "duplicate_out_of_order",
        "pole_id": pole_id,
        "original_seq": last_event.seq,
        "duplicate_id": dup.id,
    }