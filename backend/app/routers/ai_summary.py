"""
AI feature: plain-English incident summaries for the operator/dispatch note.

Deliberately narrow scope, per the brief's explicit warning against using
an LLM for localization itself: this endpoint ONLY turns an already-
computed, deterministic Incident record into a short human-readable
explanation. It never decides where the fault is -- that's 100% graph
traversal (see localization.py). If the AI call fails or no API key is
configured, we fall back to a template string built from the same fields,
so the feature degrades gracefully rather than breaking the ticket view.

Cost: one short Claude API call per incident, on demand (not per
telemetry message) -- roughly a few hundred input/output tokens, well
under a cent per call at current pricing.
"""

import os
import json
import urllib.request
import urllib.error

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Incident

router = APIRouter(prefix="/ai", tags=["ai"])

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
MODEL = "claude-sonnet-4-6"


def _template_summary(incident: Incident) -> str:
    """Fallback used when no API key is set or the API call fails."""
    where = incident.dt_id or incident.feeder_id or "the network"
    kind = {
        "span": "a break in the line",
        "dt": "a transformer-level outage",
        "feeder": "a feeder-wide outage",
    }.get(incident.incident_type, "a fault")
    return (
        f"{kind.capitalize()} detected at {where}, affecting "
        f"{incident.affected_pole_count} poles. Confidence: "
        f"{incident.confidence:.0%} ({incident.confidence_reason}). "
        f"Pincode: {incident.pincode or 'unknown'}."
    )


def _call_claude(prompt: str) -> str | None:
    if not ANTHROPIC_API_KEY:
        return None
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 200,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            for block in data.get("content", []):
                if block.get("type") == "text":
                    return block["text"].strip()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, KeyError, json.JSONDecodeError):
        return None
    return None


@router.get("/incidents/{incident_id}/summary")
def incident_summary(incident_id: int, db: Session = Depends(get_db)):
    incident = db.query(Incident).filter(Incident.id == incident_id).first()
    if not incident:
        raise HTTPException(404, "incident not found")

    prompt = (
        "You are writing a one-paragraph dispatch note for a power-utility "
        "control room operator. Be concise, factual, and actionable -- no "
        "filler, no apology, plain language a non-engineer crew dispatcher "
        "can read in five seconds.\n\n"
        f"Incident type: {incident.incident_type}\n"
        f"Location: {incident.dt_id or incident.feeder_id}\n"
        f"Coordinates: {incident.lat}, {incident.lon}\n"
        f"Pincode: {incident.pincode}\n"
        f"Affected poles: {incident.affected_pole_count}\n"
        f"Confidence: {incident.confidence:.0%}\n"
        f"Confidence reasoning: {incident.confidence_reason}\n"
        f"Localization type: {incident.localization_type}\n\n"
        "Write the dispatch note now."
    )

    ai_text = _call_claude(prompt)
    if ai_text:
        return {"summary": ai_text, "source": "ai"}

    return {"summary": _template_summary(incident), "source": "template"}
