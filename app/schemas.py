"""Typed contracts for the graph and API."""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field


TaskName = Literal["triage", "knowledge", "response", "action_plan"]
TicketStatus = Literal["open", "in_progress", "pending_customer", "resolved", "escalated"]


class Ticket(BaseModel):
    id: str
    subject: str
    description: str
    customer: str = "anonymous"
    status: TicketStatus = "open"
    priority: str = "medium"
    category: str = "uncategorized"


class ActionItem(BaseModel):
    title: str
    description: str
    owner: str = "support"


class Plan(BaseModel):
    tasks: list[TaskName] = Field(default_factory=lambda: ["triage", "knowledge", "response"])
    rationale: str = "Process the ticket using evidence, response drafting, and verification."


class VerificationResult(BaseModel):
    approved: bool
    issues: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)


class ProcessRequest(BaseModel):
    ticket_id: str = "ADHOC-001"
    subject: str = ""
    ticket_text: str = ""
    approve: bool = False
    demo_mode: bool | None = None


class AgentState(TypedDict, total=False):
    ticket_id: str
    subject: str
    ticket_text: str
    approve: bool
    plan: Plan
    remaining_tasks: list[str]
    completed_tasks: list[str]
    artifacts: dict[str, str]
    verification: VerificationResult
    revision_count: int
    saved_path: str
    final_answer: str
    event_log: list[dict[str, Any]]

