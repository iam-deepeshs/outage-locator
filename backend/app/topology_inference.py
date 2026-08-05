"""
Geometric topology inference for DTs with missing seq_on_line/parent_pole_id.

Approach: minimum spanning tree (MST) rooted at the DT, using haversine
distance between poles as edge weight. This mirrors real LT line layout,
which minimizes copper run rather than being random.

This is explicitly an ESTIMATE. Every inferred edge is tagged with a
confidence score. See ARCHITECTURE.md and DECISIONS.md for the reasoning,
known failure modes (parallel lines running close together can get swapped;
branches can attach to the wrong point on the main run), and what the UI
does differently for inferred vs known topology.
"""

import math
import heapq
from collections import defaultdict

from app.db import SessionLocal, engine
from app.models import Base, Transformer, Pole


def haversine_m(lat1, lon1, lat2, lon2):
    """Distance in meters between two lat/lon points."""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def infer_topology_for_dt(dt: Transformer, poles: list[Pole]):
    """
    Runs Prim's MST algorithm rooted at the DT over `poles` (all belonging
    to this dt_id). Mutates each Pole with inferred_parent_pole_id,
    topology_source='inferred', topology_confidence in [0,1].
    """
    if not poles:
        return

    # Node 0 = the DT itself (the root); nodes 1..n = poles
    nodes = [("DT", dt.lat, dt.lon)] + [(p.pole_id, p.lat, p.lon) for p in poles]
    n = len(nodes)

    in_tree = [False] * n
    min_edge = [math.inf] * n
    parent_idx = [-1] * n
    min_edge[0] = 0

    parent_of = {}  # pole_id -> parent_pole_id ("DT" for main-line root poles)
    edge_len_of = {}  # pole_id -> distance to its chosen parent, meters

    for _ in range(n):
        # Pick the unvisited node with the smallest connecting edge (Prim's)
        u = -1
        best = math.inf
        for i in range(n):
            if not in_tree[i] and min_edge[i] < best:
                best = min_edge[i]
                u = i
        if u == -1:
            break
        in_tree[u] = True

        if u != 0:
            pole_id = nodes[u][0]
            parent_node = nodes[parent_idx[u]][0]
            parent_of[pole_id] = parent_node
            edge_len_of[pole_id] = min_edge[u]

        # Relax edges to all unvisited nodes
        _, ulat, ulon = nodes[u]
        for v in range(n):
            if in_tree[v]:
                continue
            _, vlat, vlon = nodes[v]
            d = haversine_m(ulat, ulon, vlat, vlon)
            if d < min_edge[v]:
                min_edge[v] = d
                parent_idx[v] = u

    # Confidence: compare each edge to the local median edge length.
    # A pole whose chosen edge is short relative to its neighborhood is a
    # confident call; a pole whose shortest available edge was still long
    # (i.e. it's geographically isolated / on a sparse spur) is less certain.
    lengths = list(edge_len_of.values())
    lengths.sort()
    median_len = lengths[len(lengths) // 2] if lengths else 1.0
    median_len = max(median_len, 1.0)  # avoid div by zero on tiny networks

    pole_by_id = {p.pole_id: p for p in poles}
    for pole_id, parent_id in parent_of.items():
        edge_len = edge_len_of[pole_id]
        confidence = 1 - (edge_len / (edge_len + median_len))
        confidence = max(0.05, min(0.98, confidence))  # clip to a sane range

        pole = pole_by_id[pole_id]
        pole.inferred_parent_pole_id = None if parent_id == "DT" else parent_id
        pole.topology_source = "inferred"
        pole.topology_confidence = round(confidence, 3)


def run_inference():
    db = SessionLocal()
    try:
        transformers = db.query(Transformer).all()
        updated_dts = 0
        for dt in transformers:
            poles = db.query(Pole).filter(Pole.dt_id == dt.dt_id).all()
            already_known = [p for p in poles if p.seq_on_line is not None]

            if already_known:
                # Topology already known from the registry — mark it explicitly,
                # don't touch parent_pole_id (that's ground truth).
                for p in poles:
                    p.topology_source = "known"
                continue

            infer_topology_for_dt(dt, poles)
            updated_dts += 1

        db.commit()
        print(f"Inferred topology for {updated_dts} DTs with missing registry data.")
    finally:
        db.close()


if __name__ == "__main__":
    Base.metadata.create_all(bind=engine)
    run_inference()