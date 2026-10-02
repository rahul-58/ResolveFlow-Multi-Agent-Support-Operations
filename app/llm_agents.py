"""Ollama-backed planning and bounded tool-calling agents."""

from __future__ import annotations

import json
from typing import Any, Iterable

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool

from .config import Settings
from .schemas import Plan


def build_model(config: Settings) -> Any:
    """Create ChatOllama lazily so demo mode does not require Ollama."""

    from langchain_ollama import ChatOllama

    return ChatOllama(
        model=config.ollama_model,
        base_url=config.ollama_base_url,
        temperature=0.2,
        validate_model_on_init=True,
    )


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text", item)) if isinstance(item, dict) else str(item)
            for item in content
        )
    return str(content)


def error_detail(exc: Exception, limit: int = 280) -> str:
    """Return a compact, user-safe exception description for the audit trace."""

    detail = " ".join(str(exc).split())
    return (detail or type(exc).__name__)[:limit]


def _parse_json_object(text: str) -> dict[str, Any] | None:
    """Extract the first JSON object from a model response."""

    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        candidate = "\n".join(line for line in lines if not line.strip().startswith("```"))
    decoder = json.JSONDecoder()
    for index, character in enumerate(candidate):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(candidate[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _manual_tool_command(payload: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    """Normalize a JSON tool command or final response from a local model."""

    if "final" in payload:
        return "final", str(payload["final"]), {}

    function = payload.get("function")
    if isinstance(function, dict):
        name = function.get("name")
        arguments = function.get("arguments", {})
    else:
        name = payload.get("tool") or payload.get("name")
        arguments = (
            payload.get("arguments")
            or payload.get("parameters")
            or payload.get("args")
            or {}
        )
    if not name:
        return None
    if not isinstance(arguments, dict):
        arguments = {}
    return "tool", str(name), arguments


def run_prompt_tool_agent(
    model: Any,
    system_prompt: str,
    user_prompt: str,
    tools: Iterable[BaseTool],
    max_steps: int,
) -> tuple[str, list[dict[str, Any]]]:
    """Use a bounded JSON protocol when native Ollama tools are unavailable.

    This keeps the workflow agentic for older Ollama model templates or models
    that can chat but reject the native ``tools`` request parameter.
    """

    tool_list = list(tools)
    tool_map = {item.name: item for item in tool_list}
    tool_specs = [
        {
            "name": item.name,
            "description": item.description,
            "arguments": item.args,
        }
        for item in tool_list
    ]
    protocol = (
        "\n\nYou have access to read-only tools. Use at most one tool per step. "
        "Return exactly one JSON object and no markdown. To call a tool, use "
        '{"tool":"tool_name","arguments":{}}. When you have enough information, '
        'return {"final":"your answer"}. Available tools:\n'
        f"{json.dumps(tool_specs, indent=2, default=str)}"
    )
    messages: list[Any] = [
        SystemMessage(content=system_prompt + protocol),
        HumanMessage(content=user_prompt),
    ]
    events: list[dict[str, Any]] = [{"type": "manual_tool_mode"}]
    for step in range(max_steps):
        response = model.invoke(messages)
        content = _text(getattr(response, "content", response)).strip()
        messages.append(response)
        payload = _parse_json_object(content)
        command = _manual_tool_command(payload) if payload else None
        events.append(
            {
                "type": "model_step",
                "mode": "manual_json_tools",
                "step": step + 1,
                "tool_calls": [command[1]] if command and command[0] == "tool" else [],
            }
        )
        if not command:
            return content, events
        kind, name, arguments = command
        if kind == "final":
            return name.strip(), events
        tool = tool_map.get(name)
        try:
            result = tool.invoke(arguments) if tool else "Unknown tool."
            status = "completed" if tool else "rejected"
        except Exception as exc:  # pragma: no cover - model-dependent
            result = f"Tool failed safely: {type(exc).__name__}: {exc}"
            status = "failed"
        events.append(
            {
                "type": "tool_call",
                "mode": "manual_json_tools",
                "step": step + 1,
                "tool": name,
                "status": status,
            }
        )
        messages.append(
            HumanMessage(
                content=(
                    f"Tool result for {name}:\n{result}\n\n"
                    "Continue. Return exactly one JSON object: either another tool call "
                    'or {"final":"your answer"}.'
                )
            )
        )
    return "The agent reached its step limit without a final response.", events


def run_tool_agent(
    model: Any,
    system_prompt: str,
    user_prompt: str,
    tools: Iterable[BaseTool],
    max_steps: int,
) -> tuple[str, list[dict[str, Any]]]:
    """Execute a bounded tool-calling loop and return its audit events."""

    tool_list = list(tools)
    tool_map = {item.name: item for item in tool_list}
    try:
        bound = model.bind_tools(tool_list)
        messages: list[Any] = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ]
        events: list[dict[str, Any]] = []
        final_text = ""
        for step in range(max_steps):
            response = bound.invoke(messages)
            messages.append(response)
            calls = list(getattr(response, "tool_calls", []) or [])
            final_text = _text(getattr(response, "content", ""))
            events.append(
                {
                    "type": "model_step",
                    "mode": "native_tools",
                    "step": step + 1,
                    "tool_calls": [call.get("name", "") for call in calls],
                }
            )
            if not calls:
                return final_text.strip(), events
            for call in calls:
                name = str(call.get("name", ""))
                tool = tool_map.get(name)
                try:
                    result = tool.invoke(call.get("args", {}) or {}) if tool else "Unknown tool."
                    status = "completed" if tool else "rejected"
                except Exception as exc:  # pragma: no cover - model-dependent
                    result = f"Tool failed safely: {type(exc).__name__}: {exc}"
                    status = "failed"
                messages.append(
                    ToolMessage(
                        content=str(result),
                        tool_call_id=call.get("id", f"call-{step}-{name}"),
                    )
                )
                events.append(
                    {
                        "type": "tool_call",
                        "mode": "native_tools",
                        "step": step + 1,
                        "tool": name,
                        "status": status,
                    }
                )
        return final_text or "The agent reached its step limit without a final response.", events
    except Exception as native_error:
        manual_text, manual_events = run_prompt_tool_agent(
            model,
            system_prompt,
            user_prompt,
            tool_list,
            max_steps,
        )
        return manual_text, [
            {
                "type": "native_tool_call_error",
                "reason": type(native_error).__name__,
                "detail": error_detail(native_error),
            },
            *manual_events,
        ]


def fallback_plan(ticket_text: str) -> Plan:
    lower = ticket_text.lower()
    tasks: list[str] = ["triage", "knowledge", "response"]
    if any(
        word in lower
        for word in (
            "charged", "refund", "security", "password", "late", "missing",
            "package", "shipping", "delivery", "tracking", "escalate",
        )
    ):
        tasks.append("action_plan")
    return Plan(
        tasks=list(dict.fromkeys(tasks)),
        rationale="The fallback supervisor selected a safe ticket-processing sequence.",
    )


def normalize_plan(plan: Plan, ticket_text: str) -> Plan:
    """Enforce safety-critical minimum work even if a model omits a task."""

    ordered: list[str] = []
    for task in ["triage", "knowledge", "response", *plan.tasks]:
        if task not in ordered:
            ordered.append(task)
    fallback = fallback_plan(ticket_text)
    if "action_plan" in fallback.tasks and "action_plan" not in ordered:
        ordered.append("action_plan")
    return Plan(tasks=ordered, rationale=plan.rationale)


def create_plan(model: Any, ticket_text: str) -> Plan:
    prompt = f"""
You are a support workflow supervisor. Choose an ordered plan using only these tasks:
- triage: classify the issue, urgency, confidence, and escalation need
- knowledge: retrieve relevant product documentation and policy
- response: draft a customer-facing response grounded in sources
- action_plan: propose internal follow-up tasks or escalation

Ticket:
{ticket_text}

Always include triage, knowledge, and response. Add action_plan when another
team may need to investigate, the issue is high impact, or the ticket requires
follow-up. Return only a valid Plan object.
""".strip()
    try:
        result = model.with_structured_output(Plan).invoke(
            [
                SystemMessage(content="Return a valid Plan with only the allowed task names."),
                HumanMessage(content=prompt),
            ]
        )
        return result if isinstance(result, Plan) else Plan.model_validate(result)
    except Exception:
        return fallback_plan(ticket_text)
