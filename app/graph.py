"""ResolveFlow LangGraph supervisor and specialist workflow."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from .config import Settings, settings
from .db import SupportStore
from .knowledge import KnowledgeBase
from .llm_agents import (
    build_model,
    create_plan,
    error_detail,
    fallback_plan,
    normalize_plan,
    run_tool_agent,
)
from .schemas import ActionItem, AgentState, Plan, VerificationResult
from .tools import ACTION_TOOLS, KNOWLEDGE_TOOLS, RESPONSE_TOOLS, TRIAGE_TOOLS


def _append_events(state: AgentState, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [*state.get("event_log", []), *events]


def _contains_sensitive_credentials(ticket_text: str) -> bool:
    lower = ticket_text.lower()
    return any(
        phrase in lower
        for phrase in (
            "my password is",
            "password is",
            "authentication code",
            "one-time code",
            "one time code",
            "verification code",
        )
    )


def _is_ambiguous_ticket(ticket_text: str) -> bool:
    lower = ticket_text.lower()
    has_vague_language = any(
        phrase in lower
        for phrase in ("something is not working", "need help", "please help", "not working with my account")
    )
    has_specific_issue = any(
        word in lower
        for word in (
            "password", "login", "sign in", "charged", "payment", "refund", "package",
            "delivery", "shipping", "tracking", "security", "unfamiliar", "suspicious",
        )
    )
    return has_vague_language and not has_specific_issue


def _is_security_ticket(ticket_text: str) -> bool:
    lower = ticket_text.lower()
    return _contains_sensitive_credentials(ticket_text) or any(
        phrase in lower
        for phrase in ("suspicious login", "unfamiliar login", "accessed my account", "unauthorized access")
    )


def _is_multi_issue_ticket(ticket_text: str) -> bool:
    lower = ticket_text.lower()
    has_billing = any(word in lower for word in ("charged", "payment", "refund", "duplicate charge"))
    has_shipping = any(word in lower for word in ("package", "shipping", "delivery", "tracking"))
    return has_billing and has_shipping


def _contains_model_artifact_leak(text: str) -> bool:
    """Detect model narration or tool payloads that do not belong in artifacts."""

    lower = text.lower()
    return bool(
        re.search(
            r"\{\s*[\"'](?:name|tool|function)[\"']\s*:|"
            r"\b(?:i will|i'm going to)\s+(?:use|call|invoke)\s+(?:the )?tool\b|"
            r"\b(?:previous agent artifacts|policy search results|documentation search results|"
            r"ticket update|customer update|based on the previous agent)\b|"
            r"\b(?:get_ticket|get_ticket_history|search_knowledge_base|check_order_status|"
            r"list_open_tasks)\b",
            lower,
        )
    )


def _expected_knowledge_sources(ticket_text: str) -> set[str]:
    if _is_ambiguous_ticket(ticket_text):
        return {"support_policy.md"}
    if _contains_sensitive_credentials(ticket_text) or _is_security_ticket(ticket_text):
        return {"security.md"}
    if _is_multi_issue_ticket(ticket_text):
        return {"billing.md", "shipping.md"}
    lower = ticket_text.lower()
    if any(word in lower for word in ("charged", "payment", "refund", "card")):
        return {"billing.md"}
    if any(word in lower for word in ("password", "login", "sign in", "access")):
        return {"account.md"}
    if any(word in lower for word in ("delivery", "package", "shipping", "tracking")):
        return {"shipping.md"}
    return {"support_policy.md"}


def _knowledge_artifact_is_unsafe(ticket_text: str, knowledge_text: str) -> bool:
    if _contains_model_artifact_leak(knowledge_text):
        return True
    sources = set(re.findall(r"^SOURCE:\s*(\S+)", knowledge_text, flags=re.MULTILINE))
    return bool(sources and not sources.issubset(_expected_knowledge_sources(ticket_text)))


def _triage_artifact_is_unsafe(ticket_text: str, triage_text: str) -> bool:
    if _contains_model_artifact_leak(triage_text):
        return True
    lower = triage_text.lower()
    if not re.search(
        r"^\s*category:\s*(?:billing|account|shipping|account-security|multi-issue|general)\b",
        lower,
    ):
        return True
    expected = _demo_triage(ticket_text).lower()
    if _contains_sensitive_credentials(ticket_text) or _is_security_ticket(ticket_text):
        return "account-security" not in lower
    if _is_multi_issue_ticket(ticket_text):
        return "multi-issue" not in lower
    if _is_ambiguous_ticket(ticket_text):
        return "general" not in lower or "low" not in lower
    if any(word in ticket_text.lower() for word in ("charged", "payment", "refund", "card")):
        return "billing" not in lower
    if any(word in ticket_text.lower() for word in ("password", "login", "sign in", "access")):
        return "account" not in lower
    if any(word in ticket_text.lower() for word in ("delivery", "package", "shipping", "tracking")):
        return "shipping" not in lower
    return not expected.split("\n", 1)[0].split(":", 1)[-1].strip() in lower


def _demo_triage(ticket_text: str) -> str:
    lower = ticket_text.lower()
    if _contains_sensitive_credentials(ticket_text):
        return (
            "Category: account-security\nPriority: high\nConfidence: high\n"
            "Escalation: security-support review is required because sensitive credentials were included.\n"
            "Reason: the ticket contains a password or authentication code that must not be used or repeated."
        )
    if _is_security_ticket(ticket_text):
        return (
            "Category: account-security\nPriority: high\nConfidence: medium\n"
            "Escalation: security-support review is required for suspected unauthorized access.\n"
            "Reason: the ticket reports unfamiliar or potentially unauthorized account activity."
        )
    if _is_multi_issue_ticket(ticket_text):
        return (
            "Category: multi-issue\nPriority: high\nConfidence: medium\n"
            "Escalation: billing and shipping investigations may be required.\n"
            "Reason: the ticket contains separate payment and delivery issues."
        )
    if any(word in lower for word in ("charged", "payment", "refund", "card")):
        return (
            "Category: billing\nPriority: high\nConfidence: high\n"
            "Escalation: billing investigation may be required if both charges are settled.\n"
            "Reason: the ticket reports two charges for one order."
        )
    if any(word in lower for word in ("password", "login", "sign in", "access")):
        return (
            "Category: account\nPriority: medium\nConfidence: high\n"
            "Escalation: account support if the approved reset flow fails.\n"
            "Reason: the customer cannot access the account."
        )
    if any(word in lower for word in ("delivery", "package", "shipping", "tracking")):
        return (
            "Category: shipping\nPriority: medium\nConfidence: medium\n"
            "Escalation: shipping investigation if tracking has not changed for five days.\n"
            "Reason: the ticket reports a delayed delivery."
        )
    return (
        "Category: general\nPriority: medium\nConfidence: low\n"
        "Escalation: human review because no specialized category was confidently identified."
    )


def _demo_knowledge(ticket_text: str, knowledge: KnowledgeBase) -> str:
    lower = ticket_text.lower()
    sensitive = _contains_sensitive_credentials(ticket_text)
    ambiguous = _is_ambiguous_ticket(ticket_text)
    query = (
        "password authentication code account security"
        if sensitive
        else "unfamiliar login account security"
        if _is_security_ticket(ticket_text)
        else "support policy missing information"
        if ambiguous
        else ticket_text
    )
    hits = knowledge.search(query, include_policies=not sensitive)
    if sensitive:
        security_hits = [hit for hit in hits if hit["source"] == "security.md"]
        if security_hits:
            hits = security_hits
    elif _is_security_ticket(ticket_text):
        security_hits = [hit for hit in hits if hit["source"] == "security.md"]
        if security_hits:
            hits = security_hits
    elif ambiguous:
        policy_hits = [hit for hit in hits if hit["source"] == "support_policy.md"]
        if policy_hits:
            hits = policy_hits
    elif _is_multi_issue_ticket(ticket_text):
        relevant_hits = [
            hit for hit in hits if hit["source"] in {"billing.md", "shipping.md"}
        ]
        if relevant_hits:
            hits = relevant_hits
    return knowledge.format_hits(hits)


def _demo_response(ticket_text: str, knowledge_text: str) -> str:
    lower = ticket_text.lower()
    sensitive = _contains_sensitive_credentials(ticket_text)
    if sensitive:
        body = (
            "Hi, please do not share passwords or one-time authentication codes in support requests. "
            "Because sensitive credentials were included here, this should be handled through the approved "
            "account-security support process."
        )
        source = "security.md"
    elif _is_security_ticket(ticket_text):
        body = (
            "Hi, I’m sorry you noticed unfamiliar account activity. Please do not share your password "
            "or any one-time authentication code. This should be reviewed through the approved "
            "account-security support process."
        )
        source = "security.md"
    elif _is_multi_issue_ticket(ticket_text):
        body = (
            "Hi, I understand that you are reporting both a duplicate charge and a delayed package. "
            "Please share the order number and the last four digits of the card, but not the full card number. "
            "We need to confirm whether one charge is still pending and review the latest tracking event. "
            "A billing investigation and a shipping investigation would be the next steps if the available details support them."
        )
        source = "billing.md\nSource: shipping.md"
    elif "charged" in lower or "payment" in lower or "refund" in lower:
        body = (
            "Hi, thanks for reaching out. I understand that you see two charges for one order. "
            "Could you share the order number and the last four digits of the card, but not the full card number? "
            "We need to confirm whether one charge is still pending or whether both charges settled. "
            "If both charges are settled, a billing investigation would be the next step."
        )
        source = "billing.md"
    elif any(word in lower for word in ("suspicious login", "unfamiliar login", "accessed my account")):
        body = (
            "Hi, I’m sorry you noticed unfamiliar account activity. Please do not share your password "
            "or any one-time authentication code. This should be reviewed through the approved "
            "account-security support process."
        )
        source = "security.md"
    elif "password" in lower or "access" in lower or "login" in lower:
        body = (
            "Hi, please check your spam or junk folder and confirm that you are using the email address "
            "associated with the account. A reset email may be resent through the approved support process "
            "after account verification. If the reset flow still does not work, account-support review "
            "would be the next step."
        )
        source = "account.md"
    elif "shipping" in lower or "package" in lower or "delivery" in lower or "tracking" in lower:
        body = (
            "Hi, I’m sorry that your package is delayed. Please share the order number so we can review "
            "the latest tracking event. If tracking has not changed for five days, a shipping investigation "
            "would be the next step."
        )
        source = "shipping.md"
    else:
        body = "Thanks for reaching out. We need a little more information before we can investigate this request."
        source = "support_policy.md"
    return f"Customer response:\n{body}\n\nSource: {source}"


def _demo_action(ticket_text: str) -> str:
    lower = ticket_text.lower()
    sensitive = _contains_sensitive_credentials(ticket_text)
    if sensitive:
        return (
            "Create internal task: Account-security review\n"
            "Owner: security-support\n"
            "Details: Route the ticket through the approved security process; never use or repeat the exposed credentials."
        )
    if _is_security_ticket(ticket_text):
        return (
            "Create internal task: Account-security review\n"
            "Owner: security-support\n"
            "Details: Route the suspected unauthorized access through the approved security process; do not claim the account was secured without evidence."
        )
    if _is_multi_issue_ticket(ticket_text):
        return (
            "Propose internal tasks:\n"
            "1. Billing investigation — confirm whether the duplicate charge is pending or settled.\n"
            "2. Shipping investigation — review the latest tracking event and delivery status."
        )
    if any(word in lower for word in ("charged", "payment", "refund")):
        return (
            "Create internal task: Billing investigation\n"
            "Owner: billing\n"
            "Details: Confirm whether the duplicate charge is pending or settled; do not promise a refund before confirmation."
        )
    if any(word in lower for word in ("password", "access", "login")):
        return (
            "Create internal task: Account access follow-up\n"
            "Owner: account-support\n"
            "Details: Review the approved reset flow if the customer still cannot access the account."
        )
    if any(word in lower for word in ("shipping", "package", "delivery", "tracking")):
        return (
            "Create internal task: Shipping investigation\n"
            "Owner: shipping\n"
            "Details: Review tracking when the promised delivery date has passed or tracking is unchanged for five days."
        )
    return "No internal task is proposed until a human reviews the missing context."


def _demo_final(state: AgentState) -> str:
    verification = state.get("verification")
    pieces = [
        "# ResolveFlow result",
        f"Ticket: `{state.get('ticket_id', '')}`",
        f"Plan: {', '.join(state.get('plan', Plan()).tasks)}",
    ]
    for key, value in state.get("artifacts", {}).items():
        pieces.append(f"## {key.replace('_', ' ').title()}\n{value}")
    if verification:
        pieces.append(
            "## Verification\n"
            f"Approved: {verification.approved}\n"
            f"Issues: {verification.issues or 'None detected'}\n"
            f"Warnings: {verification.warnings or 'None'}\n"
            f"Sources checked: {verification.sources or 'None recorded'}"
        )
    if state.get("saved_path"):
        pieces.append(f"## Approved changes\nSaved run: `{state['saved_path']}`")
    else:
        pieces.append(
            "## Approval\nPreview only. No response, ticket status, or internal task was saved."
        )
    return "\n\n".join(pieces)


def create_graph(config: Settings = settings):
    model = None
    model_error: Exception | None = None
    if not config.demo_mode:
        try:
            model = build_model(config)
        except Exception as exc:  # pragma: no cover - environment-specific
            model_error = exc
    store = SupportStore(config.support_db_path, config.sample_tickets_path)
    knowledge = KnowledgeBase(
        config.knowledge_base_dir,
        config.policies_dir,
        max_hits=config.max_knowledge_hits,
    )

    def plan_node(state: AgentState) -> AgentState:
        ticket_text = f"{state.get('subject', '')}\n{state.get('ticket_text', '')}".strip()
        if config.demo_mode or model is None:
            plan = fallback_plan(ticket_text)
        else:
            plan = normalize_plan(create_plan(model, ticket_text), ticket_text)
        if _is_security_ticket(ticket_text) and "action_plan" not in plan.tasks:
            plan = Plan(
                tasks=[*plan.tasks, "action_plan"],
                rationale=plan.rationale,
            )
        events = [{"type": "supervisor_plan", "tasks": plan.tasks}]
        if model_error:
            events.append({"type": "ollama_unavailable", "reason": type(model_error).__name__})
        return {
            "plan": plan,
            "remaining_tasks": list(plan.tasks),
            "completed_tasks": [],
            "artifacts": {},
            "revision_count": 0,
            "event_log": _append_events(state, events),
        }

    def worker_node(role: str, toolset: list[Any]) -> Callable[[AgentState], AgentState]:
        def run(state: AgentState) -> AgentState:
            ticket_text = f"{state.get('subject', '')}\n{state.get('ticket_text', '')}".strip()
            context = (
                f"Ticket ID: {state.get('ticket_id', '')}\n"
                f"Ticket:\n{ticket_text}\n\n"
                f"Previous agent artifacts:\n{json.dumps(state.get('artifacts', {}), indent=2)}"
            )
            events: list[dict[str, Any]]
            if config.demo_mode or model is None:
                if role == "triage":
                    output = _demo_triage(ticket_text)
                elif role == "knowledge":
                    output = _demo_knowledge(ticket_text, knowledge)
                elif role == "response":
                    output = _demo_response(ticket_text, state.get("artifacts", {}).get("knowledge", ""))
                else:
                    output = _demo_action(ticket_text)
                events = [{"type": "demo_specialist", "agent": role}]
            else:
                prompts = {
                    "triage": (
                        "You are the triage agent. Use ticket tools to classify the issue, priority, "
                        "confidence, and escalation need. Do not invent account or system state."
                    ),
                    "knowledge": (
                        "You are the knowledge agent. Search local documentation and policy. Return the "
                        "most relevant sources and explain what they do and do not support."
                    ),
                    "response": (
                        "You are the resolution agent. Draft a respectful customer response grounded in "
                        "the retrieved sources. Never request secrets or promise an unsupported outcome."
                    ),
                    "action_plan": (
                        "You are the operations agent. Propose internal follow-up tasks or escalation only "
                        "when the evidence supports them. This is a proposal; do not claim it was executed."
                    ),
                }
                try:
                    output, events = run_tool_agent(
                        model,
                        prompts[role],
                        context,
                        toolset,
                        config.max_agent_steps,
                    )
                except Exception as exc:
                    output = (
                        _demo_triage(ticket_text)
                        if role == "triage"
                        else _demo_knowledge(ticket_text, knowledge)
                        if role == "knowledge"
                        else _demo_response(ticket_text, state.get("artifacts", {}).get("knowledge", ""))
                        if role == "response"
                        else _demo_action(ticket_text)
                    )
                    events = [
                        {
                            "type": "specialist_fallback",
                            "agent": role,
                            "reason": type(exc).__name__,
                            "detail": error_detail(exc),
                        }
                    ]
            artifacts = dict(state.get("artifacts", {}))
            artifacts[role] = output
            if role == "triage" and _triage_artifact_is_unsafe(ticket_text, output):
                artifacts[role] = _demo_triage(ticket_text)
                events.append({"type": "triage_normalization_guardrail", "agent": role})
            elif role == "knowledge" and _knowledge_artifact_is_unsafe(ticket_text, output):
                artifacts[role] = _demo_knowledge(ticket_text, knowledge)
                events.append({"type": "knowledge_normalization_guardrail", "agent": role})
            elif role == "action_plan" and knowledge.verify_action_plan(output):
                artifacts[role] = _demo_action(ticket_text)
                events.append({"type": "action_plan_normalization_guardrail", "agent": role})
            if role == "knowledge" and "SOURCE:" not in artifacts[role]:
                grounded = _demo_knowledge(ticket_text, knowledge)
                if grounded != "No local knowledge-base document matched the query.":
                    artifacts[role] = f"{artifacts[role]}\n\n{grounded}"
                    events.append({"type": "knowledge_grounding_guardrail", "agent": role})
            remaining = list(state.get("remaining_tasks", []))
            if role in remaining:
                remaining.remove(role)
            return {
                "artifacts": artifacts,
                "remaining_tasks": remaining,
                "completed_tasks": [*state.get("completed_tasks", []), role],
                "event_log": _append_events(
                    state,
                    [{"agent": role, **event} for event in events],
                ),
            }

        return run

    def verify_node(state: AgentState) -> AgentState:
        artifacts = dict(state.get("artifacts", {}))
        ticket_text = f"{state.get('subject', '')}\n{state.get('ticket_text', '')}".strip()
        guardrail_events: list[dict[str, Any]] = []
        if _contains_sensitive_credentials(ticket_text):
            # A model must not downgrade a ticket containing exposed credentials
            # to an ordinary password-reset workflow.
            artifacts["triage"] = _demo_triage(ticket_text)
            artifacts["knowledge"] = _demo_knowledge(ticket_text, knowledge)
            artifacts["response"] = _demo_response(ticket_text, artifacts["knowledge"])
            artifacts["action_plan"] = _demo_action(ticket_text)
            guardrail_events.append({"type": "sensitive_credentials_guardrail"})
        elif _is_ambiguous_ticket(ticket_text):
            artifacts["triage"] = _demo_triage(ticket_text)
            artifacts["knowledge"] = _demo_knowledge(ticket_text, knowledge)
            artifacts["response"] = _demo_response(ticket_text, artifacts["knowledge"])
            artifacts["action_plan"] = _demo_action(ticket_text)
            guardrail_events.append({"type": "ambiguity_guardrail"})
        elif _is_multi_issue_ticket(ticket_text):
            artifacts["triage"] = _demo_triage(ticket_text)
            artifacts["knowledge"] = _demo_knowledge(ticket_text, knowledge)
            artifacts["response"] = _demo_response(ticket_text, artifacts["knowledge"])
            artifacts["action_plan"] = _demo_action(ticket_text)
            guardrail_events.append({"type": "multi_issue_guardrail"})
        elif _is_security_ticket(ticket_text):
            artifacts["triage"] = _demo_triage(ticket_text)
            artifacts["knowledge"] = _demo_knowledge(ticket_text, knowledge)
            artifacts["response"] = _demo_response(ticket_text, artifacts["knowledge"])
            artifacts["action_plan"] = _demo_action(ticket_text)
            guardrail_events.append({"type": "security_ticket_guardrail"})
        response = artifacts.get("response", "")
        knowledge_text = artifacts.get("knowledge", "")
        result = knowledge.verify_response(response, knowledge_text)
        action_issues = knowledge.verify_action_plan(artifacts.get("action_plan", ""))
        if action_issues:
            artifacts["action_plan"] = _demo_action(
                f"{state.get('subject', '')}\n{state.get('ticket_text', '')}".strip()
            )
            result["warnings"] = [
                *result.get("warnings", []),
                "The unsafe action-plan wording was replaced with a grounded proposal.",
            ]
            result["issues"] = [*result.get("issues", []), *action_issues]
            result["approved"] = False
        verification = VerificationResult.model_validate(result)
        return {
            "artifacts": artifacts,
            "verification": verification,
            "event_log": _append_events(
                state,
                [
                    *guardrail_events,
                    {
                        "type": "verification",
                        "approved": verification.approved,
                        "issues": verification.issues,
                    }
                ],
            ),
        }

    def revise_node(state: AgentState) -> AgentState:
        artifacts = dict(state.get("artifacts", {}))
        verification = state.get("verification", VerificationResult(approved=False))
        issues = "; ".join(verification.issues)
        ticket_text = f"{state.get('subject', '')}\n{state.get('ticket_text', '')}".strip()
        revision_events: list[dict[str, Any]] = [{"type": "revision", "issues": verification.issues}]
        if model is not None:
            try:
                response = model.invoke(
                    "Revise this customer response to remove the following policy issues: "
                    f"{issues}. Use only the retrieved knowledge, ask only for safe references, "
                    "do not promise a refund, reversal, system change, future update, customer "
                    "follow-up, or internal task, and do not claim a guaranteed resolution. Include "
                    "a `Source: filename` line. Return only the revised response.\n\n"
                    f"Retrieved knowledge:\n{artifacts.get('knowledge', '')}\n\n"
                    f"Draft response:\n{artifacts.get('response', '')}"
                )
                candidate = getattr(response, "content", str(response))
                candidate_check = knowledge.verify_response(
                    candidate,
                    artifacts.get("knowledge", ""),
                )
                if candidate_check["approved"]:
                    artifacts["response"] = candidate
                else:
                    artifacts["response"] = _demo_response(
                        ticket_text,
                        artifacts.get("knowledge", ""),
                    )
                    revision_events.append(
                        {
                            "type": "revision_fallback",
                            "reason": "candidate_failed_verification",
                            "issues": candidate_check["issues"],
                        }
                    )
            except Exception as exc:
                artifacts["response"] = _demo_response(
                    ticket_text,
                    artifacts.get("knowledge", ""),
                )
                revision_events.append(
                    {
                        "type": "revision_fallback",
                        "reason": type(exc).__name__,
                        "detail": error_detail(exc),
                    }
                )
        else:
            artifacts["response"] = _demo_response(
                ticket_text,
                artifacts.get("knowledge", ""),
            )
            revision_events.append(
                {
                    "type": "revision_fallback",
                    "reason": "model_unavailable",
                }
            )
        return {
            "artifacts": artifacts,
            "revision_count": state.get("revision_count", 0) + 1,
            "event_log": _append_events(state, revision_events),
        }

    def approval_node(state: AgentState) -> AgentState:
        return {
            "event_log": _append_events(
                state,
                [{"type": "human_approval", "approved": bool(state.get("approve", False))}],
            )
        }

    def save_node(state: AgentState) -> AgentState:
        artifacts = state.get("artifacts", {})
        action_items: list[ActionItem] = []
        action_text = artifacts.get("action_plan", "")
        if action_text and not action_text.lower().startswith("no internal task"):
            action_items.append(
                ActionItem(
                    title=f"Follow up on {state.get('ticket_id', 'ticket')}",
                    description=action_text,
                    owner="support-operations",
                )
            )
        verification = state.get("verification", VerificationResult(approved=False))
        run_id = store.save_approved_resolution(
            state.get("ticket_id", ""),
            artifacts.get("response", ""),
            action_items,
            verification.model_dump(),
            state.get("event_log", []),
        )
        payload = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "ticket_id": state.get("ticket_id", ""),
            "artifacts": artifacts,
            "verification": verification.model_dump(),
            "event_log": state.get("event_log", []),
        }
        config.runs_dir.mkdir(parents=True, exist_ok=True)
        path = config.runs_dir / f"resolveflow-{run_id}.json"
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return {
            "saved_path": str(path),
            "event_log": _append_events(state, [{"type": "approved_changes_saved", "path": str(path)}]),
        }

    def finalize_node(state: AgentState) -> AgentState:
        # Keep finalization deterministic so a second LLM pass cannot add
        # unsupported promises after verification has completed.
        return {"final_answer": _demo_final(state)}

    def next_task(state: AgentState) -> str:
        remaining = state.get("remaining_tasks", [])
        return remaining[0] if remaining else "verify"

    def after_verify(state: AgentState) -> str:
        verification = state.get("verification")
        if verification and not verification.approved and state.get("revision_count", 0) < 1:
            return "revise"
        return "approval"

    def after_approval(state: AgentState) -> str:
        verification = state.get("verification")
        if state.get("approve", False) and verification and verification.approved:
            return "save"
        return "finalize"

    graph = StateGraph(AgentState)
    graph.add_node("plan", plan_node)
    graph.add_node("triage", worker_node("triage", TRIAGE_TOOLS))
    graph.add_node("knowledge", worker_node("knowledge", KNOWLEDGE_TOOLS))
    graph.add_node("response", worker_node("response", RESPONSE_TOOLS))
    graph.add_node("action_plan", worker_node("action_plan", ACTION_TOOLS))
    graph.add_node("verify", verify_node)
    graph.add_node("revise", revise_node)
    graph.add_node("approval", approval_node)
    graph.add_node("save", save_node)
    graph.add_node("finalize", finalize_node)
    graph.add_edge(START, "plan")
    route_map = {
        "triage": "triage",
        "knowledge": "knowledge",
        "response": "response",
        "action_plan": "action_plan",
        "verify": "verify",
    }
    graph.add_conditional_edges("plan", next_task, route_map)
    for node in ("triage", "knowledge", "response", "action_plan"):
        graph.add_conditional_edges(node, next_task, route_map)
    graph.add_conditional_edges("verify", after_verify, {"revise": "revise", "approval": "approval"})
    graph.add_edge("revise", "verify")
    graph.add_conditional_edges("approval", after_approval, {"save": "save", "finalize": "finalize"})
    graph.add_edge("save", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile()


def run_agent(
    ticket_id: str = "TICK-1001",
    subject: str = "",
    ticket_text: str = "",
    approve: bool = False,
    demo_mode: bool | None = None,
    config: Settings = settings,
) -> AgentState:
    if demo_mode is not None and demo_mode != config.demo_mode:
        config = Settings(
            root_dir=config.root_dir,
            ollama_base_url=config.ollama_base_url,
            ollama_model=config.ollama_model,
            demo_mode=demo_mode,
            support_db_path=config.support_db_path,
            knowledge_base_dir=config.knowledge_base_dir,
            policies_dir=config.policies_dir,
            sample_tickets_path=config.sample_tickets_path,
            runs_dir=config.runs_dir,
            max_agent_steps=config.max_agent_steps,
            max_knowledge_hits=config.max_knowledge_hits,
        )
    store = SupportStore(config.support_db_path, config.sample_tickets_path)
    ticket = store.ensure_ticket(ticket_id, subject, ticket_text)
    graph = create_graph(config)
    return graph.invoke(
        {
            "ticket_id": ticket.id,
            "subject": ticket.subject,
            "ticket_text": ticket.description,
            "approve": approve,
            "event_log": [],
        }
    )
