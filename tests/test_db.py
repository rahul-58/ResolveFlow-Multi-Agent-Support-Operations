import json
from pathlib import Path

from app.db import SupportStore


ROOT = Path(__file__).parents[1]


def test_store_loads_sample_tickets(tmp_path):
    store = SupportStore(tmp_path / "test.db", ROOT / "data/samples/tickets.json")
    ticket = store.get_ticket("TICK-1001")
    assert ticket is not None
    assert ticket.category == "billing"


def test_approved_resolution_creates_event_and_task(tmp_path):
    store = SupportStore(tmp_path / "test.db", ROOT / "data/samples/tickets.json")
    from app.schemas import ActionItem

    run_id = store.save_approved_resolution(
        "TICK-1001",
        "We will investigate the duplicate charge.",
        [ActionItem(title="Billing review", description="Check the settlement state.", owner="billing")],
        {"approved": True},
        [{"type": "test"}],
    )
    assert len(run_id) == 12
    assert store.get_ticket("TICK-1001").status == "pending_customer"
    assert len(store.list_open_tasks("TICK-1001")) == 1
    assert len(store.history("TICK-1001")) == 1

