from pathlib import Path

from app.config import Settings
from app.graph import run_agent


ROOT = Path(__file__).parents[1]


def make_config(tmp_path):
    return Settings(
        root_dir=ROOT,
        demo_mode=True,
        support_db_path=tmp_path / "resolveflow.db",
        knowledge_base_dir=ROOT / "data/knowledge_base",
        policies_dir=ROOT / "data/policies",
        sample_tickets_path=ROOT / "data/samples/tickets.json",
        runs_dir=tmp_path / "runs",
    )


def test_demo_graph_processes_ticket_without_mutation(tmp_path):
    result = run_agent(
        ticket_id="TICK-1001",
        demo_mode=True,
        config=make_config(tmp_path),
    )
    assert result["completed_tasks"] == ["triage", "knowledge", "response", "action_plan"]
    assert result["verification"].approved is True
    assert result["verification"].sources == ["billing.md"]
    assert "Customer response" in result["artifacts"]["response"]
    assert not result.get("saved_path")
    assert any(event.get("type") == "human_approval" for event in result["event_log"])


def test_demo_graph_saves_only_with_approval(tmp_path):
    config = make_config(tmp_path)
    result = run_agent(
        ticket_id="TICK-1001",
        approve=True,
        demo_mode=True,
        config=config,
    )
    assert result.get("saved_path")
    assert Path(result["saved_path"]).exists()
