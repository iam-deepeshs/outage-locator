"""
Auto-seed logic, run on FastAPI startup. Checks if the database is already
populated (idempotent) before generating -- so restarts don't wipe manually
created state, and the stack is genuinely a one-command `docker compose up`
away from a working demo, per gate G3.
"""

import os
from app.db import SessionLocal
from app.models import Pole


def seed_if_empty():
    if os.getenv("SEED_ON_STARTUP", "true").lower() != "true":
        print("SEED_ON_STARTUP disabled, skipping auto-seed.")
        return

    db = SessionLocal()
    try:
        existing = db.query(Pole).count()
        if existing > 0:
            print(f"Database already has {existing} poles, skipping seed.")
            return
    finally:
        db.close()

    print("Database empty -- running auto-seed (generate_data + topology_inference)...")
    from app.generate_data import generate
    from app.topology_inference import run_inference

    generate()
    run_inference()
    print("Auto-seed complete.")
