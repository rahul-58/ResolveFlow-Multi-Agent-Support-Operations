from langchain_core.messages import AIMessage

from app.llm_agents import run_tool_agent
from app.tools import search_knowledge_base


class FakeModel:
    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        if messages[-1].type == "human":
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "search_knowledge_base",
                        "args": {"query": "duplicate charge"},
                        "id": "call-1",
                    }
                ],
            )
        return AIMessage(content="I used the billing source.")


def test_tool_agent_executes_and_records_tool_call():
    answer, events = run_tool_agent(
        FakeModel(),
        "Use the tool before answering.",
        "Find billing guidance.",
        [search_knowledge_base],
        max_steps=3,
    )
    assert answer == "I used the billing source."
    assert any(event.get("tool") == "search_knowledge_base" for event in events)


class JsonFallbackModel:
    def bind_tools(self, tools):
        raise RuntimeError("model does not support native tools")

    def invoke(self, messages):
        if messages[-1].type == "human" and "Tool result for" not in messages[-1].content:
            return AIMessage(
                content='{"tool":"search_knowledge_base","arguments":{"query":"duplicate charge"}}'
            )
        return AIMessage(content='{"final":"I used the billing source."}')


def test_tool_agent_uses_bounded_json_fallback_when_native_tools_fail():
    answer, events = run_tool_agent(
        JsonFallbackModel(),
        "Use the tool before answering.",
        "Find billing guidance.",
        [search_knowledge_base],
        max_steps=3,
    )
    assert answer == "I used the billing source."
    assert any(event.get("type") == "native_tool_call_error" for event in events)
    assert any(event.get("mode") == "manual_json_tools" for event in events)
    assert any(event.get("tool") == "search_knowledge_base" for event in events)


class ResponseErrorModel(JsonFallbackModel):
    def __init__(self):
        self.failed_native_call = False

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        if not self.failed_native_call:
            self.failed_native_call = True
            raise RuntimeError("ResponseError: model rejected the tools request")
        return super().invoke(messages)


def test_tool_agent_recovers_when_model_rejects_tools_during_invoke():
    answer, events = run_tool_agent(
        ResponseErrorModel(),
        "Use the tool before answering.",
        "Find billing guidance.",
        [search_knowledge_base],
        max_steps=3,
    )
    assert answer == "I used the billing source."
    error = next(event for event in events if event.get("type") == "native_tool_call_error")
    assert error["reason"] == "RuntimeError"
    assert "model rejected the tools request" in error["detail"]
