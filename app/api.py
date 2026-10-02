"""Minimal FastAPI interface for ResolveFlow."""

from __future__ import annotations

from fastapi import FastAPI

from .config import settings
from .db import SupportStore
from .graph import run_agent
from .schemas import ProcessRequest


app = FastAPI(title="ResolveFlow", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "resolveflow"}


@app.get("/tickets")
def tickets() -> list[dict]:
    store = SupportStore(settings.support_db_path, settings.sample_tickets_path)
    return [ticket.model_dump() for ticket in store.list_tickets()]


@app.post("/process")
def process(request: ProcessRequest) -> dict:
    result = run_agent(
        ticket_id=request.ticket_id,
        subject=request.subject,
        ticket_text=request.ticket_text,
        approve=request.approve,
        demo_mode=request.demo_mode,
    )
    response = dict(result)
    if response.get("plan"):
        response["plan"] = response["plan"].model_dump()
    if response.get("verification"):
        response["verification"] = response["verification"].model_dump()
    return response

