"""
API surface for driving the simulator — this is what the fault-injection UI
(and your own curl-based testing) will call. Simulated time is tracked
server-side in memory and advances only when explicitly ticked, so you
control the pace rather than waiting on real wall-clock minutes.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
import datetime

from app.db import get_db
from app.simulator import run_heartbeat_tick, SIM_START, reset_simulator_state

router = APIRouter(prefix="/simulator", tags=["simulator"])

_sim_clock = {"now": SIM_START}


@router.post("/tick")
def advance_tick(minutes: int = 5, db: Session = Depends(get_db)):
    """
    Advances simulated time by `minutes` and fires any heartbeats that
    become due. This is the basic 'heartbeat' of the whole simulator —
    fault injection (Step 4) will build on top of this same clock.
    """
    _sim_clock["now"] += datetime.timedelta(minutes=minutes)
    sent = run_heartbeat_tick(_sim_clock["now"], db=db)
    return {
        "sim_time": _sim_clock["now"].isoformat(),
        "heartbeats_sent": sent,
    }


@router.get("/status")
def simulator_status():
    return {"sim_time": _sim_clock["now"].isoformat()}


@router.post("/reset")
def reset_simulator():
    """Resets the simulated clock and in-memory device state to the start."""
    _sim_clock["now"] = SIM_START
    reset_simulator_state()
    return {"status": "reset", "sim_time": _sim_clock["now"].isoformat()}


from app.simulator import (
    inject_span_fault,
    inject_dt_fault,
    inject_feeder_fault,
    repair_poles,
)

# In-memory record of the last injected fault's ground truth, so /repair
# can restore exactly what was broken without you having to pass pole
# lists by hand. Keyed by an incrementing fault id.
_injected_faults = {}
_next_fault_id = {"n": 1}


@router.post("/fault/span")
def fault_span(dt_id: str, db: Session = Depends(get_db)):
    result = inject_span_fault(db, dt_id, _sim_clock["now"])
    if "error" in result:
        return result
    fault_id = _next_fault_id["n"]
    _next_fault_id["n"] += 1
    _injected_faults[fault_id] = result
    return {"fault_id": fault_id, **result}


@router.post("/fault/dt")
def fault_dt(dt_id: str, db: Session = Depends(get_db)):
    result = inject_dt_fault(db, dt_id, _sim_clock["now"])
    fault_id = _next_fault_id["n"]
    _next_fault_id["n"] += 1
    _injected_faults[fault_id] = result
    return {"fault_id": fault_id, **result}


@router.post("/fault/feeder")
def fault_feeder(feeder_id: str, db: Session = Depends(get_db)):
    result = inject_feeder_fault(db, feeder_id, _sim_clock["now"])
    fault_id = _next_fault_id["n"]
    _next_fault_id["n"] += 1
    _injected_faults[fault_id] = result
    return {"fault_id": fault_id, **result}


@router.post("/repair/{fault_id}")
def repair_fault(fault_id: int, db: Session = Depends(get_db)):
    fault = _injected_faults.get(fault_id)
    if not fault:
        return {"error": f"no such fault_id {fault_id}"}
    repair_poles(db, fault["affected_pole_ids"], _sim_clock["now"])
    return {"status": "repaired", "fault_id": fault_id, "affected_pole_count": fault["affected_pole_count"]}


@router.get("/faults")
def list_injected_faults():
    """Ground truth of everything injected this session — for your own
    testing/validation. The localization algorithm must never read this."""
    return _injected_faults


from app.simulator import (
    inject_dead_sensor,
    inject_scheduled_outage,
    inject_duplicate_and_out_of_order,
)


@router.post("/noise/dead-sensor")
def noise_dead_sensor(pole_id: str, db: Session = Depends(get_db)):
    return inject_dead_sensor(db, pole_id, _sim_clock["now"])


@router.post("/noise/scheduled-outage")
def noise_scheduled_outage(
    scope: str,
    target_id: str,
    duration_minutes: int = 60,
    cancelled_but_not_updated: bool = False,
    db: Session = Depends(get_db),
):
    return inject_scheduled_outage(
        db, scope, target_id, _sim_clock["now"],
        duration_minutes=duration_minutes,
        cancelled_but_not_updated=cancelled_but_not_updated,
    )


@router.post("/noise/duplicate")
def noise_duplicate(pole_id: str, db: Session = Depends(get_db)):
    return inject_duplicate_and_out_of_order(db, pole_id, _sim_clock["now"])