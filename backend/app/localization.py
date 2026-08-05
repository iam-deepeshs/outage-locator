"""
Fault localization engine.

Step 1: current-state resolver.
Step 2: boundary detection (single top-down tree walk, no double-counting).
Step 3: noise filtering for isolated dead sensors (live-children check).
Step 4: scheduled-outage suppression. A boundary is suppressed only while
an active scheduled outage genuinely covers its DT or feeder, WITH a grace
buffer for the routine 20-40 minute overrun the spec describes. Once that
buffer expires, suppression is lifted even if the feed's `end` time has
technically passed -- so a real fault that happens to start during, or
just after, a scheduled window is not permanently hidden. Outages that the
feed lists but that never actually darkened any poles require no special
handling: there's simply no boundary to suppress, since suppression is
checked against DETECTED boundaries (built from real telemetry), never
against the feed alone.
"""

import datetime
from collections import defaultdict
from sqlalchemy.orm import Session

from app.models import Pole, TelemetryEvent, ScheduledOutage, Transformer

HEARTBEAT_INTERVAL_SECONDS = 15 * 60
JITTER_SECONDS = 45
STALE_GRACE_MULTIPLIER = 2.5
SCHEDULED_OUTAGE_OVERRUN_BUFFER_MINUTES = 40

STATE_LIVE = "live"
STATE_DARK = "dark"
STATE_UNKNOWN = "unknown"
STATE_STALE = "stale"


def resolve_pole_states(db: Session, sim_now: datetime.datetime) -> dict[str, dict]:
    poles = db.query(Pole).all()

    events = (
        db.query(TelemetryEvent)
        .order_by(TelemetryEvent.pole_id, TelemetryEvent.seq.desc())
        .all()
    )
    latest_by_pole: dict[str, TelemetryEvent] = {}
    for e in events:
        if e.pole_id not in latest_by_pole:
            latest_by_pole[e.pole_id] = e

    results = {}
    for pole in poles:
        if not pole.device_id:
            results[pole.pole_id] = {
                "state": STATE_UNKNOWN, "last_event": None, "last_seen": None,
                "reason": "no device fitted",
            }
            continue

        last_event = latest_by_pole.get(pole.pole_id)
        if not last_event:
            results[pole.pole_id] = {
                "state": STATE_UNKNOWN, "last_event": None, "last_seen": None,
                "reason": "no telemetry received yet",
            }
            continue

        seconds_since = (sim_now - last_event.device_ts).total_seconds()
        stale_threshold = (HEARTBEAT_INTERVAL_SECONDS + JITTER_SECONDS) * STALE_GRACE_MULTIPLIER

        if last_event.energized:
            state = STATE_STALE if seconds_since > stale_threshold else STATE_LIVE
        else:
            state = STATE_DARK

        results[pole.pole_id] = {
            "state": state, "last_event": last_event.event,
            "last_seen": last_event.device_ts,
            "seconds_since_last_seen": seconds_since,
        }

    return results


def summarize_states(states: dict[str, dict]) -> dict:
    counts = defaultdict(int)
    for info in states.values():
        counts[info["state"]] += 1
    return dict(counts)


def _parent_of(pole: Pole) -> str | None:
    return pole.parent_pole_id or pole.inferred_parent_pole_id


def _topology_source_of(pole: Pole) -> str:
    return pole.topology_source or "unknown"


def detect_boundaries_for_dt(db: Session, dt_id: str, states: dict[str, dict]) -> list[dict]:
    poles = db.query(Pole).filter(Pole.dt_id == dt_id).all()
    if not poles:
        return []
    pole_by_id = {p.pole_id: p for p in poles}

    def state_of(pid):
        return states.get(pid, {}).get("state", "unknown")

    if not any(state_of(p.pole_id) == "live" for p in poles):
        non_live = [p.pole_id for p in poles if state_of(p.pole_id) != "live"]
        if any(state_of(pid) in ("dark", "stale") for pid in non_live):
            return [{"type": "dt", "dt_id": dt_id, "affected_pole_ids": non_live}]
        return []

    children: dict[str, list[Pole]] = {}
    roots: list[Pole] = []
    for p in poles:
        parent_id = _parent_of(p)
        if parent_id and parent_id in pole_by_id:
            children.setdefault(parent_id, []).append(p)
        else:
            roots.append(p)

    boundaries: list[dict] = []
    sensor_faults: list[dict] = []

    def children_states(pid):
        return [state_of(c.pole_id) for c in children.get(pid, [])]

    def walk(pole: Pole, last_confirmed_live_id: str | None,
             in_affected_branch: bool, current_boundary: dict | None):
        pid = pole.pole_id
        s = state_of(pid)

        if in_affected_branch:
            current_boundary["affected_pole_ids"].append(pid)
            for child in children.get(pid, []):
                walk(child, last_confirmed_live_id, True, current_boundary)
            return

        if s == "dark":
            source = _topology_source_of(pole)
            if source == "inferred" or last_confirmed_live_id is None:
                b = {
                    "type": "span_range", "dt_id": dt_id,
                    "dark_boundary_pole_id": pid,
                    "candidate_live_parent_id": _parent_of(pole),
                    "last_confirmed_live_pole_id": last_confirmed_live_id,
                    "confidence": pole.topology_confidence,
                    "affected_pole_ids": [pid],
                }
            else:
                b = {
                    "type": "span_exact", "dt_id": dt_id,
                    "live_boundary_pole_id": _parent_of(pole),
                    "dark_boundary_pole_id": pid,
                    "affected_pole_ids": [pid],
                }
            boundaries.append(b)
            for child in children.get(pid, []):
                walk(child, last_confirmed_live_id, True, b)
            return

        if s == "stale":
            child_states = children_states(pid)
            has_live_child = "live" in child_states
            has_down_child = any(cs in ("dark", "stale") for cs in child_states)

            if has_live_child and not has_down_child:
                sensor_faults.append({
                    "type": "sensor_fault", "dt_id": dt_id, "pole_id": pid,
                    "reason": "stale (no confirmed power_lost) with live children downstream -- physically inconsistent with a real line fault",
                })
                for child in children.get(pid, []):
                    walk(child, last_confirmed_live_id, False, None)
                return

            is_leaf = len(child_states) == 0
            source = _topology_source_of(pole)
            b = {
                "type": "span_range", "dt_id": dt_id,
                "dark_boundary_pole_id": pid,
                "candidate_live_parent_id": _parent_of(pole),
                "last_confirmed_live_pole_id": last_confirmed_live_id,
                "confidence": (pole.topology_confidence or 0.5) * 0.7,
                "note": "stale (no confirmed power_lost) -- inferred from silence" + (" (leaf pole)" if is_leaf else " (children also down)"),
                "affected_pole_ids": [pid],
            }
            boundaries.append(b)
            for child in children.get(pid, []):
                walk(child, last_confirmed_live_id, True, b)
            return

        next_last_live = pid if s == "live" else last_confirmed_live_id
        for child in children.get(pid, []):
            walk(child, next_last_live, False, None)

    for root in roots:
        walk(root, None, False, None)

    return boundaries + sensor_faults


def _active_scheduled_outages(db: Session, sim_now: datetime.datetime) -> list[ScheduledOutage]:
    buffer = datetime.timedelta(minutes=SCHEDULED_OUTAGE_OVERRUN_BUFFER_MINUTES)
    all_outages = db.query(ScheduledOutage).all()
    return [
        so for so in all_outages
        if so.start <= sim_now <= (so.end + buffer)
    ]


def _apply_schedule_suppression(db: Session, boundaries: list[dict], sim_now: datetime.datetime) -> list[dict]:
    active = _active_scheduled_outages(db, sim_now)
    if not active:
        for b in boundaries:
            b["suppressed"] = False
        return boundaries

    dt_to_feeder = {t.dt_id: t.feeder_id for t in db.query(Transformer).all()}
    for b in boundaries:
        if b["type"] == "sensor_fault":
            b["suppressed"] = False
            continue

        dt_id = b.get("dt_id")
        feeder_id = dt_to_feeder.get(dt_id)

        matched_so = None
        for so in active:
            if so.scope == "dt" and so.target_id == dt_id:
                matched_so = so
                break
            if so.scope == "feeder" and so.target_id == feeder_id:
                matched_so = so
                break

        if matched_so:
            b["suppressed"] = True
            b["suppressed_by"] = matched_so.id
        else:
            b["suppressed"] = False

    return boundaries


def _merge_fragmented_dt_boundaries(dt_id: str, boundaries: list[dict], total_poles_in_dt: int,
                                     coverage_threshold: float = 0.6) -> list[dict]:
    """
    If a single DT produced multiple span-type boundaries (not sensor
    faults, not already a 'dt' type) whose COMBINED affected poles cover a
    large majority of that DT, this is far more likely to be one real
    fault fragmented by telemetry noise (lost power_lost messages leaving
    scattered poles falsely reading 'live') than several independent
    coincidental faults. Collapses them into one dt-level incident in that
    case. Below the threshold, boundaries are left separate -- genuinely
    plausible as distinct simultaneous faults (see 01-problem-context.md's
    multi-fault requirement).
    """
    span_boundaries = [b for b in boundaries if b["type"] in ("span_exact", "span_range")]
    other = [b for b in boundaries if b["type"] not in ("span_exact", "span_range")]

    if len(span_boundaries) == 0 or total_poles_in_dt == 0:
        return boundaries

    union_poles = set()
    for b in span_boundaries:
        union_poles.update(b["affected_pole_ids"])

    coverage = len(union_poles) / total_poles_in_dt
    if coverage >= coverage_threshold:
        return other + [{
            "type": "dt",
            "dt_id": dt_id,
            "affected_pole_ids": sorted(union_poles),
            "note": (
                f"single span boundary already covers {coverage:.0%} of this DT's poles -- treated as a whole-DT fault"
                if len(span_boundaries) == 1 else
                f"merged from {len(span_boundaries)} fragmented span boundaries covering {coverage:.0%} of this DT's poles -- likely one fault, not several"
            ),
        }]
    return boundaries


def detect_all_boundaries(db: Session, sim_now: datetime.datetime, include_suppressed: bool = True) -> list[dict]:
    states = resolve_pole_states(db, sim_now)
    dt_ids = [row[0] for row in db.query(Pole.dt_id).distinct().all()]

    all_results = []
    for dt_id in dt_ids:
        dt_boundaries = detect_boundaries_for_dt(db, dt_id, states)
        total_poles = db.query(Pole).filter(Pole.dt_id == dt_id).count()
        dt_boundaries = _merge_fragmented_dt_boundaries(dt_id, dt_boundaries, total_poles)
        all_results.extend(dt_boundaries)

    all_results = _apply_schedule_suppression(db, all_results, sim_now)

    if include_suppressed:
        return all_results
    return [r for r in all_results if not r.get("suppressed", False)]

# ============================================================
# Step 5: Feeder-level merge + persisting incidents
# ============================================================

from collections import Counter
from app.models import Incident

DEFAULT_CONFIDENCE = {"span_exact": 0.95, "dt": 0.90, "feeder": 0.90}
OPEN_STATUSES = ("detected", "acknowledged", "crew_assigned")


def _merge_feeder_level(db: Session, active: list[dict]) -> list[dict]:
    """
    If EVERY DT under a feeder is present as a full 'dt'-type boundary,
    that's actually one feeder-level fault, not N independent DT faults.
    Merges them into a single 'feeder' boundary. This mirrors the same
    "don't alert once per symptom" principle already applied at the pole
    level, one level up the tree.
    """
    dt_boundaries = [b for b in active if b["type"] == "dt"]
    other = [b for b in active if b["type"] != "dt"]

    dt_to_feeder = {t.dt_id: t.feeder_id for t in db.query(Transformer).all()}
    by_feeder = defaultdict(list)
    for b in dt_boundaries:
        by_feeder[dt_to_feeder.get(b["dt_id"])].append(b)

    merged = []
    for feeder_id, group in by_feeder.items():
        all_dts_on_feeder = {
            t.dt_id for t in db.query(Transformer).filter(Transformer.feeder_id == feeder_id).all()
        }
        down_dts = {b["dt_id"] for b in group}
        if feeder_id and len(all_dts_on_feeder) > 1 and down_dts == all_dts_on_feeder:
            affected = [pid for b in group for pid in b["affected_pole_ids"]]
            merged.append({"type": "feeder", "feeder_id": feeder_id, "affected_pole_ids": affected})
        else:
            merged.extend(group)

    return other + merged


def _pincode_for_dt(db: Session, dt_id: str) -> str | None:
    rows = db.query(Pole.pincode).filter(Pole.dt_id == dt_id, Pole.pincode.isnot(None)).all()
    if not rows:
        return None
    return Counter(r[0] for r in rows).most_common(1)[0][0]


def _location_for(db: Session, boundary: dict, pole_by_id: dict) -> dict:
    if boundary["type"] == "dt":
        dt = db.query(Transformer).filter(Transformer.dt_id == boundary["dt_id"]).first()
        if dt:
            return {"lat": dt.lat, "lon": dt.lon, "pincode": _pincode_for_dt(db, boundary["dt_id"])}
        return {"lat": None, "lon": None, "pincode": None}

    if boundary["type"] == "feeder":
        pts = [(pole_by_id[p].lat, pole_by_id[p].lon) for p in boundary["affected_pole_ids"] if p in pole_by_id]
        lat = sum(p[0] for p in pts) / len(pts) if pts else None
        lon = sum(p[1] for p in pts) / len(pts) if pts else None
        pincodes = [pole_by_id[p].pincode for p in boundary["affected_pole_ids"] if p in pole_by_id and pole_by_id[p].pincode]
        pincode = Counter(pincodes).most_common(1)[0][0] if pincodes else None
        return {"lat": lat, "lon": lon, "pincode": pincode}

    pole = pole_by_id.get(boundary.get("dark_boundary_pole_id"))
    if pole:
        pincode = pole.pincode or _pincode_for_dt(db, boundary["dt_id"])
        return {"lat": pole.lat, "lon": pole.lon, "pincode": pincode}
    return {"lat": None, "lon": None, "pincode": None}


def _confidence_for(boundary: dict) -> float:
    if boundary.get("confidence") is not None:
        return round(float(boundary["confidence"]), 3)
    return DEFAULT_CONFIDENCE.get(boundary["type"], 0.5)


def _incident_key(boundary: dict) -> tuple:
    if boundary["type"] == "feeder":
        return ("feeder", boundary["feeder_id"], None)
    if boundary["type"] == "dt":
        return ("dt", boundary["dt_id"], None)
    return ("span", boundary["dt_id"], boundary.get("dark_boundary_pole_id"))


def _key_for_incident_record(inc: Incident) -> tuple:
    if inc.incident_type == "feeder":
        return ("feeder", inc.feeder_id, None)
    if inc.incident_type == "dt":
        return ("dt", inc.dt_id, None)
    return ("span", inc.dt_id, inc.boundary_dark_pole_id)


def sync_incidents_from_boundaries(db: Session, sim_now: datetime.datetime) -> dict:
    """
    Runs full localization (current-state -> boundaries -> noise filter ->
    schedule suppression -> feeder merge) and upserts results into the
    Incident table. Matching an existing OPEN incident (by dt/feeder/pole
    identity) updates it in place, so a fault that's still ongoing when
    this is re-run doesn't spawn duplicate tickets. Sensor faults are
    intentionally never written as Incidents -- see DECISIONS.md.
    """
    boundaries = detect_all_boundaries(db, sim_now, include_suppressed=False)
    active = [b for b in boundaries if b["type"] != "sensor_fault"]
    active = _merge_feeder_level(db, active)

    all_pole_ids = {pid for b in active for pid in b.get("affected_pole_ids", [])}
    pole_by_id = (
        {p.pole_id: p for p in db.query(Pole).filter(Pole.pole_id.in_(all_pole_ids)).all()}
        if all_pole_ids else {}
    )

    open_incidents = db.query(Incident).filter(Incident.status.in_(OPEN_STATUSES)).all()
    open_by_key = {_key_for_incident_record(inc): inc for inc in open_incidents}

    created, updated = 0, 0
    for b in active:
        key = _incident_key(b)
        existing = open_by_key.get(key)
        loc = _location_for(db, b, pole_by_id)
        confidence = _confidence_for(b)

        if existing:
            existing.affected_pole_count = len(b.get("affected_pole_ids", []))
            existing.confidence = confidence
            existing.lat, existing.lon, existing.pincode = loc["lat"], loc["lon"], loc["pincode"]
            updated += 1
        else:
            incident = Incident(
                incident_type="feeder" if b["type"] == "feeder" else ("dt" if b["type"] == "dt" else "span"),
                status="detected",
                boundary_live_pole_id=b.get("live_boundary_pole_id") or b.get("candidate_live_parent_id"),
                boundary_dark_pole_id=b.get("dark_boundary_pole_id"),
                dt_id=b.get("dt_id"),
                feeder_id=b.get("feeder_id"),
                lat=loc["lat"], lon=loc["lon"], pincode=loc["pincode"],
                affected_pole_count=len(b.get("affected_pole_ids", [])),
                confidence=confidence,
                confidence_reason=b.get("note") or (
                    "exact known topology" if b["type"] == "span_exact" else
                    "geometric topology inference" if b["type"] == "span_range" else
                    "every DT on this feeder is down" if b["type"] == "feeder" else
                    "no live pole under this DT"
                ),
                localization_type=b["type"],
                detected_at=datetime.datetime.utcnow(),
            )
            db.add(incident)
            created += 1

    db.commit()
    return {"created": created, "updated": updated, "active_boundaries": len(active)}