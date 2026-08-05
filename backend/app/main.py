from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import os

from app.db import engine
from app.models import Base
from app.routers import network, telemetry, simulator_control, localization_debug, tickets, ai_summary
from app.seed import seed_if_empty

app = FastAPI(title="Outage Locator API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(network.router)
app.include_router(telemetry.router)
app.include_router(simulator_control.router)
app.include_router(localization_debug.router)
app.include_router(tickets.router)
app.include_router(ai_summary.router)


@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)
    seed_if_empty()


@app.get("/health")
def health():
    return {
        "status": "ok",
        "db_url_configured": bool(os.getenv("DATABASE_URL")),
    }
