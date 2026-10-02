"""Read-only tools available to ResolveFlow specialist agents."""

from __future__ import annotations

import json

from langchain_core.tools import tool

from .config import settings
from .db import SupportStore
from .knowledge import KnowledgeBase


def _store() -> SupportStore:
    return SupportStore(settings.support_db_path, settings.sample_tickets_path)


def _kb() -> KnowledgeBase:
    return KnowledgeBase(
        settings.knowledge_base_dir,
        settings.policies_dir,
        max_hits=settings.max_knowledge_hits,
    )


@tool
def get_ticket(ticket_id: str) -> str:
    """Read the current ticket record for a ticket ID."""

    ticket = _store().get_ticket(ticket_id)
    if not ticket:
        return f"No ticket found for ID: {ticket_id}"
    return ticket.model_dump_json(indent=2)


@tool
def get_ticket_history(ticket_id: str) -> str:
    """Read prior agent and support events for a ticket."""

    history = _store().history(ticket_id)
    return json.dumps(history, indent=2) if history else "No prior ticket events were found."


@tool
def search_similar_tickets(query: str) -> str:
    """Find related tickets that may provide operational context."""

    tickets = _store().search_tickets(query)
    return json.dumps([ticket.model_dump() for ticket in tickets], indent=2)


@tool
def search_knowledge_base(query: str) -> str:
    """Search local product and support documentation."""

    hits = _kb().search(query)
    return _kb().format_hits(hits)


@tool
def get_support_policy() -> str:
    """Read the local support response and escalation policy."""

    return _kb().policy_text() or "No support policy file was found."


@tool
def list_open_tasks(ticket_id: str = "") -> str:
    """List existing internal tasks, optionally limited to one ticket."""

    tasks = _store().list_open_tasks(ticket_id or None)
    return json.dumps(tasks, indent=2)


@tool
def check_response_policy(response: str) -> str:
    """Run deterministic policy checks against a proposed customer response."""

    result = _kb().verify_response(response, "")
    return json.dumps(result, indent=2)


TRIAGE_TOOLS = [get_ticket, get_ticket_history, search_similar_tickets]
KNOWLEDGE_TOOLS = [get_ticket, get_ticket_history, search_knowledge_base, get_support_policy]
RESPONSE_TOOLS = [get_ticket, search_knowledge_base, get_support_policy, check_response_policy]
ACTION_TOOLS = [get_ticket, list_open_tasks, get_support_policy]
