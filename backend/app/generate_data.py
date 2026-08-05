"""
Synthetic pole/transformer/feeder network generator.

Matches the scale ratios in 02-data-and-systems.md, at reduced absolute size
per the FAQ ("a few thousand poles across a few dozen transformers is plenty").

Reproduces:
- Radial tree topology: substation -> feeder -> DT -> poles (with branches/spurs)
- ~60% of DTs missing seq_on_line / parent_pole_id entirely
- ~9% of poles with no device fitted
- ~3% of poles missing pincode
- Poles per DT ranging ~9-240, median ~70 (scaled down proportionally)
"""

import random
import math
from app.db import SessionLocal, engine
from app.models import Base, Feeder, Transformer, Pole

random.seed(42)  # reproducible runs — worth mentioning in DECISIONS.md

# ---- Scale (scaled down from the real spec, same ratios) ----
N_SUBSTATIONS = 2
N_FEEDERS = 6
N_TRANSFORMERS = 40
TARGET_POLES = 3000

DT_MISSING_TOPOLOGY_RATE = 0.60
POLE_NO_DEVICE_RATE = 0.09
POLE_NO_PINCODE_RATE = 0.03

# Rough Bengaluru-area bounding box for plausible lat/lon
BASE_LAT, BASE_LON = 12.9716, 77.5946


def jitter_latlon(lat, lon, max_meters):
    """Random offset within max_meters, converted to degrees."""
    meters_per_deg_lat = 111_320
    meters_per_deg_lon = 111_320 * math.cos(math.radians(lat))
    dlat = random.uniform(-max_meters, max_meters) / meters_per_deg_lat
    dlon = random.uniform(-max_meters, max_meters) / meters_per_deg_lon
    return lat + dlat, lon + dlon


def build_feeders_and_substations():
    feeders = []
    for s in range(1, N_SUBSTATIONS + 1):
        sub_id = f"SS-{s:02d}"
        n_feeders_here = N_FEEDERS // N_SUBSTATIONS
        for f in range(1, n_feeders_here + 1):
            feeder_id = f"F-{s:02d}-{f:02d}"
            feeders.append(Feeder(feeder_id=feeder_id, substation_id=sub_id))
    return feeders


def build_transformers(feeders):
    transformers = []
    for i in range(1, N_TRANSFORMERS + 1):
        feeder = random.choice(feeders)
        dt_id = f"D-{i:04d}"
        lat, lon = jitter_latlon(BASE_LAT, BASE_LON, max_meters=8000)
        transformers.append(Transformer(
            dt_id=dt_id,
            feeder_id=feeder.feeder_id,
            lat=lat,
            lon=lon,
            capacity_kva=random.choice([100, 160, 250, 400]),
            households_served=random.randint(80, 500),
        ))
    return transformers


def build_poles_for_transformer(dt, poles_for_this_dt, topology_known):
    """
    Builds a radial LT line from the DT: a main run with 1-5 branches/spurs,
    matching the physical shape described in 01-problem-context.md.
    """
    poles = []
    pole_counter = [0]

    def next_pole_id():
        pole_counter[0] += 1
        # Global-ish uniqueness: dt_id + local counter
        return f"P-{dt.dt_id[2:]}{pole_counter[0]:03d}"

    # Main line: walk outward from the DT, one pole at a time
    main_len = max(3, int(poles_for_this_dt * random.uniform(0.5, 0.7)))
    branch_pole_budget = poles_for_this_dt - main_len

    cur_lat, cur_lon = dt.lat, dt.lon
    prev_pole = None
    main_chain = []

    for seq in range(1, main_len + 1):
        cur_lat, cur_lon = jitter_latlon(cur_lat, cur_lon, max_meters=25)
        pid = next_pole_id()
        pole = Pole(
            pole_id=pid,
            lat=cur_lat,
            lon=cur_lon,
            feeder_id=dt.feeder_id,
            dt_id=dt.dt_id,
            seq_on_line=seq if topology_known else None,
            parent_pole_id=prev_pole.pole_id if (topology_known and prev_pole) else None,
            pole_type=random.choice(["LT-9m-PCC", "LT-8m-Steel", "LT-9m-Steel"]),
            ward=f"W-{random.randint(1, 150):03d}",
            pincode=None if random.random() < POLE_NO_PINCODE_RATE else str(random.randint(560001, 560110)),
            device_id=None if random.random() < POLE_NO_DEVICE_RATE else f"KSPDB-{dt.dt_id}-{pid}",
        )
        poles.append(pole)
        main_chain.append(pole)
        prev_pole = pole

    # Branches/spurs: 1-5 branches off random points on the main line
    n_branches = random.randint(1, 5) if branch_pole_budget > 0 else 0
    remaining = branch_pole_budget
    for b in range(n_branches):
        if remaining <= 0 or not main_chain:
            break
        branch_len = max(1, remaining // (n_branches - b) if (n_branches - b) > 0 else remaining)
        branch_len = min(branch_len, remaining)
        origin = random.choice(main_chain)
        blat, blon = origin.lat, origin.lon
        bprev = origin
        for _ in range(branch_len):
            blat, blon = jitter_latlon(blat, blon, max_meters=25)
            pid = next_pole_id()
            pole = Pole(
                pole_id=pid,
                lat=blat,
                lon=blon,
                feeder_id=dt.feeder_id,
                dt_id=dt.dt_id,
                seq_on_line=None,  # branch ordering also unknown even when main line is known,
                                   # unless topology_known — kept simple: inherit same known/unknown state
                parent_pole_id=bprev.pole_id if topology_known else None,
                pole_type=random.choice(["LT-9m-PCC", "LT-8m-Steel", "LT-9m-Steel"]),
                ward=f"W-{random.randint(1, 150):03d}",
                pincode=None if random.random() < POLE_NO_PINCODE_RATE else str(random.randint(560001, 560110)),
                device_id=None if random.random() < POLE_NO_DEVICE_RATE else f"KSPDB-{dt.dt_id}-{pid}",
            )
            poles.append(pole)
            bprev = pole
            remaining -= 1

    return poles


def generate():
    feeders = build_feeders_and_substations()
    transformers = build_transformers(feeders)

    poles_per_dt = TARGET_POLES // N_TRANSFORMERS
    all_poles = []
    for dt in transformers:
        topology_known = random.random() > DT_MISSING_TOPOLOGY_RATE
        n_poles = max(9, int(random.gauss(poles_per_dt, poles_per_dt * 0.3)))
        all_poles.extend(build_poles_for_transformer(dt, n_poles, topology_known))

    db = SessionLocal()
    try:
        db.query(Pole).delete()
        db.query(Transformer).delete()
        db.query(Feeder).delete()
        db.commit()

        db.add_all(feeders)
        db.commit()
        db.add_all(transformers)
        db.commit()
        db.add_all(all_poles)
        db.commit()

        print(f"Generated: {len(feeders)} feeders, {len(transformers)} transformers, {len(all_poles)} poles")
        known_dts = sum(1 for dt in transformers if any(
            p.dt_id == dt.dt_id and p.seq_on_line is not None for p in all_poles
        ))
        print(f"DTs with known topology: {known_dts}/{len(transformers)} ({known_dts/len(transformers)*100:.0f}%)")
        no_device = sum(1 for p in all_poles if p.device_id is None)
        print(f"Poles with no device: {no_device}/{len(all_poles)} ({no_device/len(all_poles)*100:.0f}%)")
    finally:
        db.close()


if __name__ == "__main__":
    Base.metadata.create_all(bind=engine)
    generate()