from pathlib import Path

from app.knowledge import KnowledgeBase


ROOT = Path(__file__).parents[1]


def test_duplicate_charge_retrieves_billing_source():
    kb = KnowledgeBase(ROOT / "data/knowledge_base", ROOT / "data/policies")
    hits = kb.search("charged twice pending settled refund")
    assert hits
    assert hits[0]["source"] == "billing.md"
    assert {hit["source"] for hit in hits} == {"billing.md"}


def test_policy_rejects_secret_request():
    kb = KnowledgeBase(ROOT / "data/knowledge_base", ROOT / "data/policies")
    result = kb.verify_response("Please send your password so we can investigate.", "account.md")
    assert result["approved"] is False


def test_policy_allows_password_reset_guidance():
    kb = KnowledgeBase(ROOT / "data/knowledge_base", ROOT / "data/policies")
    source_text = kb.format_hits(kb.search("password reset email"))
    result = kb.verify_response(
        "Please check your spam folder and use the approved password reset flow.\n"
        "Source: account.md",
        source_text,
    )
    assert result["approved"] is True


def test_policy_rejects_unsupported_financial_promises_and_missing_citation():
    kb = KnowledgeBase(ROOT / "data/knowledge_base", ROOT / "data/policies")
    source_text = kb.format_hits(kb.search("charged twice pending settled refund"))
    result = kb.verify_response(
        "I'll make sure to reverse one of the charges and keep you updated.",
        source_text,
    )
    assert result["approved"] is False
    assert any("financial remedy" in issue for issue in result["issues"])
    assert any("future communication" in issue for issue in result["issues"])


def test_policy_requires_source_citation_when_knowledge_exists():
    kb = KnowledgeBase(ROOT / "data/knowledge_base", ROOT / "data/policies")
    source_text = kb.format_hits(kb.search("charged twice pending settled refund"))
    result = kb.verify_response("We need the order number to investigate.", source_text)
    assert result["approved"] is False
    assert any("supporting source" in issue for issue in result["issues"])


def test_action_plan_rejects_completed_refund_claim():
    kb = KnowledgeBase(ROOT / "data/knowledge_base", ROOT / "data/policies")
    issues = kb.verify_action_plan(
        "The issue was resolved by the previous agent and a refund was processed."
    )
    assert issues


def test_policy_allows_negated_refund_uncertainty():
    kb = KnowledgeBase(ROOT / "data/knowledge_base", ROOT / "data/policies")
    source_text = kb.format_hits(kb.search("charged twice pending settled refund"))
    result = kb.verify_response(
        "We will not be able to confirm a refund until the billing team reviews the transaction.\n"
        "Source: billing.md",
        source_text,
    )
    assert not any("financial remedy" in issue for issue in result["issues"])


def test_policy_rejects_unapproved_task_and_follow_up_commitments():
    kb = KnowledgeBase(ROOT / "data/knowledge_base", ROOT / "data/policies")
    source_text = kb.format_hits(kb.search("charged twice pending settled refund"))
    result = kb.verify_response(
        "I will create a billing investigation task and follow up with you.\n"
        "Source: billing.md",
        source_text,
    )
    assert result["approved"] is False
    assert any("internal action" in issue for issue in result["issues"])
    assert any("customer follow-up" in issue for issue in result["issues"])


def test_policy_rejects_escalation_and_update_commitments():
    kb = KnowledgeBase(ROOT / "data/knowledge_base", ROOT / "data/policies")
    source_text = kb.format_hits(kb.search("charged twice pending settled refund"))
    result = kb.verify_response(
        "I will need to escalate this issue to our billing team and provide you with an update.\n"
        "Source: billing.md",
        source_text,
    )
    assert result["approved"] is False
    assert any("internal action" in issue for issue in result["issues"])
    assert any("future communication" in issue for issue in result["issues"])


def test_policy_rejects_future_task_creation_statement():
    kb = KnowledgeBase(ROOT / "data/knowledge_base", ROOT / "data/policies")
    source_text = kb.format_hits(kb.search("charged twice pending settled refund"))
    result = kb.verify_response(
        "If both charges are settled, a billing investigation task will be created.\n"
        "Source: billing.md",
        source_text,
    )
    assert result["approved"] is False
    assert any("internal action" in issue for issue in result["issues"])


def test_policy_rejects_tool_payload_and_reasoning_in_response():
    kb = KnowledgeBase(ROOT / "data/knowledge_base", ROOT / "data/policies")
    source_text = kb.format_hits(kb.search("password reset email"))
    result = kb.verify_response(
        'Here is the JSON for the function call: {"name": "search_knowledge_base", '
        '"parameters": {"query": "password reset"}}\nSource: account.md',
        source_text,
    )
    assert result["approved"] is False
    assert any("tool instructions" in issue for issue in result["issues"])


def test_action_plan_rejects_tool_execution_claims():
    issues = KnowledgeBase.verify_action_plan(
        'I will call get_ticket to retrieve the current ticket record.\n'
        '{"name": "get_ticket", "parameters": {"ticket_id": "TICK-1002"}}'
    )
    assert issues


def test_action_plan_rejects_invented_customer_state():
    issues = KnowledgeBase.verify_action_plan(
        "Based on the customer's response, they confirmed the email address. "
        "Resend the password reset email after verification."
    )
    assert issues


def test_policy_rejects_unsupported_shipping_remedy_claims():
    kb = KnowledgeBase(ROOT / "data/knowledge_base", ROOT / "data/policies")
    source_text = kb.format_hits(kb.search("package tracking unchanged five days delivery date"))
    result = kb.verify_response(
        "The customer is entitled to a replacement or refund, and we will contact the shipping carrier.\n"
        "Source: shipping.md",
        source_text,
    )
    assert result["approved"] is False
    assert any("refund or replacement" in issue for issue in result["issues"])
    assert any("carrier investigation" in issue for issue in result["issues"])


def test_policy_rejects_confirmed_transaction_claim_from_customer_report():
    kb = KnowledgeBase(ROOT / "data/knowledge_base", ROOT / "data/policies")
    source_text = kb.format_hits(kb.search("charged twice pending settled refund"))
    result = kb.verify_response(
        "I reviewed your ticket and confirmed that you were charged twice.\n"
        "Source: billing.md",
        source_text,
    )
    assert result["approved"] is False
    assert any("confirmed transaction outcome" in issue for issue in result["issues"])


def test_policy_rejects_internal_action_recommendation_in_response():
    kb = KnowledgeBase(ROOT / "data/knowledge_base", ROOT / "data/policies")
    source_text = kb.format_hits(kb.search("charged twice pending settled refund"))
    result = kb.verify_response(
        "If both charges are settled, we should create a billing investigation task.\n"
        "Source: billing.md",
        source_text,
    )
    assert result["approved"] is False
    assert any("internal action" in issue for issue in result["issues"])


def test_policy_rejects_refund_issuance_promise():
    kb = KnowledgeBase(ROOT / "data/knowledge_base", ROOT / "data/policies")
    source_text = kb.format_hits(kb.search("charged twice pending settled refund"))
    result = kb.verify_response(
        "A refund will only be issued after the billing team confirms the transaction.\n"
        "Source: billing.md",
        source_text,
    )
    assert result["approved"] is False
    assert any("financial remedy" in issue for issue in result["issues"])


def test_policy_rejects_customer_facing_task_creation_and_revision_note():
    kb = KnowledgeBase(ROOT / "data/knowledge_base", ROOT / "data/policies")
    source_text = kb.format_hits(kb.search("package tracking unchanged five days delivery date"))
    result = kb.verify_response(
        "Please contact support to create a shipping investigation task.\n"
        "(Note: I removed the internal tool instructions.)\n"
        "Source: shipping.md",
        source_text,
    )
    assert result["approved"] is False
    assert any("internal action" in issue for issue in result["issues"])
    assert any("internal research narration" in issue for issue in result["issues"])
