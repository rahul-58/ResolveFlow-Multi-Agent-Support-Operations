"""Streamlit interface for ResolveFlow."""

from __future__ import annotations

import json

import streamlit as st

from app.config import settings
from app.db import SupportStore
from app.graph import run_agent


st.set_page_config(page_title="ResolveFlow", page_icon="🛠️", layout="wide")
st.title("ResolveFlow")
st.caption("Local multi-agent support and issue-resolution workflow")

store = SupportStore(settings.support_db_path, settings.sample_tickets_path)
tickets = store.list_tickets()

with st.sidebar:
    st.subheader("Runtime")
    demo_mode = st.checkbox("Deterministic demo mode", value=settings.demo_mode)
    approve = st.checkbox(
        "Approve response and internal actions",
        value=False,
        help="Without approval, the run is preview-only and does not update ticket status or create tasks.",
    )
    st.write(f"Model: `{settings.ollama_model}`")
    st.write(f"Tickets loaded: `{len(tickets)}`")

selected_id = st.selectbox(
    "Ticket",
    options=[ticket.id for ticket in tickets] or ["ADHOC-001"],
)
selected = next((ticket for ticket in tickets if ticket.id == selected_id), None)
subject = st.text_input("Subject", value=selected.subject if selected else "")
ticket_text = st.text_area(
    "Ticket description",
    value=selected.description if selected else "",
    height=160,
)

if st.button("Process ticket", type="primary"):
    if not ticket_text.strip():
        st.warning("Enter a ticket description first.")
    else:
        with st.spinner("Supervisor is coordinating the support agents..."):
            result = run_agent(
                ticket_id=selected_id,
                subject=subject,
                ticket_text=ticket_text,
                approve=approve,
                demo_mode=demo_mode,
            )
        st.markdown(result.get("final_answer", "No final answer was produced."))
        if result.get("saved_path"):
            st.success(f"Approved changes saved to `{result['saved_path']}`")
        with st.expander("Execution trace"):
            st.json(result.get("event_log", []))
        with st.expander("Raw state"):
            serializable = dict(result)
            if serializable.get("plan"):
                serializable["plan"] = serializable["plan"].model_dump()
            if serializable.get("verification"):
                serializable["verification"] = serializable["verification"].model_dump()
            st.code(json.dumps(serializable, indent=2, default=str), language="json")

