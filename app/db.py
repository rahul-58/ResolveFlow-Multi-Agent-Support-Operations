"""SQLite persistence for tickets, tasks, and audit events."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schemas import ActionItem, Ticket


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SupportStore:
    def __init__(self, path: Path, sample_tickets_path: Path | None = None):
        self.path = path
        self.sample_tickets_path = sample_tickets_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tickets (
                    id TEXT PRIMARY KEY,
                    subject TEXT NOT NULL,
                    description TEXT NOT NULL,
                    customer TEXT NOT NULL,
                    status TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    category TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            if self.sample_tickets_path and self.sample_tickets_path.exists():
                samples = json.loads(self.sample_tickets_path.read_text(encoding="utf-8"))
                for sample in samples:
                    existing = connection.execute(
                        "SELECT subject, description FROM tickets WHERE id = ?",
                        (sample["id"],),
                    ).fetchone()
                    if existing and existing["subject"] == "Untitled ticket" and not existing["description"]:
                        connection.execute(
                            """
                            UPDATE tickets
                            SET subject = ?, description = ?, customer = ?, priority = ?, category = ?, updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                sample.get("subject", ""),
                                sample.get("description", ""),
                                sample.get("customer", "anonymous"),
                                sample.get("priority", "medium"),
                                sample.get("category", "uncategorized"),
                                _now(),
                                sample["id"],
                            ),
                        )
                    else:
                        self._insert_ticket(connection, sample)

    def _insert_ticket(self, connection: sqlite3.Connection, data: dict[str, Any]) -> None:
        now = _now()
        connection.execute(
            """
            INSERT OR IGNORE INTO tickets
            (id, subject, description, customer, status, priority, category, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["id"],
                data.get("subject", ""),
                data.get("description", ""),
                data.get("customer", "anonymous"),
                data.get("status", "open"),
                data.get("priority", "medium"),
                data.get("category", "uncategorized"),
                now,
                now,
            ),
        )

    def ensure_ticket(
        self,
        ticket_id: str,
        subject: str = "",
        description: str = "",
        customer: str = "anonymous",
    ) -> Ticket:
        existing = self.get_ticket(ticket_id)
        if existing:
            if description or subject:
                with self._connect() as connection:
                    connection.execute(
                        "UPDATE tickets SET subject = ?, description = ?, updated_at = ? WHERE id = ?",
                        (subject or existing.subject, description or existing.description, _now(), ticket_id),
                    )
                return self.get_ticket(ticket_id)  # type: ignore[return-value]
            return existing
        with self._connect() as connection:
            self._insert_ticket(
                connection,
                {
                    "id": ticket_id,
                    "subject": subject or "Untitled ticket",
                    "description": description,
                    "customer": customer,
                    "status": "open",
                    "priority": "medium",
                    "category": "uncategorized",
                },
            )
        return self.get_ticket(ticket_id)  # type: ignore[return-value]

    def get_ticket(self, ticket_id: str) -> Ticket | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        return Ticket(**dict(row)) if row else None

    def list_tickets(self, limit: int = 50) -> list[Ticket]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tickets ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [Ticket(**dict(row)) for row in rows]

    def search_tickets(self, query: str, limit: int = 5) -> list[Ticket]:
        terms = [term for term in query.lower().split() if len(term) > 2]
        tickets = self.list_tickets(limit=200)
        ranked: list[tuple[int, Ticket]] = []
        for ticket in tickets:
            text = f"{ticket.subject} {ticket.description} {ticket.category}".lower()
            score = sum(term in text for term in terms)
            if score:
                ranked.append((score, ticket))
        ranked.sort(key=lambda item: -item[0])
        return [ticket for _, ticket in ranked[:limit]]

    def list_open_tasks(self, ticket_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM tasks WHERE status != 'done'"
        params: tuple[Any, ...] = ()
        if ticket_id:
            query += " AND ticket_id = ?"
            params = (ticket_id,)
        query += " ORDER BY created_at DESC"
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(query, params).fetchall()]

    def history(self, ticket_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE ticket_id = ? ORDER BY created_at", (ticket_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def save_approved_resolution(
        self,
        ticket_id: str,
        response: str,
        action_items: list[ActionItem],
        verification: dict[str, Any],
        trace: list[dict[str, Any]],
    ) -> str:
        payload = {
            "response": response,
            "action_items": [item.model_dump() for item in action_items],
            "verification": verification,
            "trace": trace,
        }
        serialized = json.dumps(payload, sort_keys=True)
        run_id = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:12]
        with self._connect() as connection:
            connection.execute(
                "UPDATE tickets SET status = ?, updated_at = ? WHERE id = ?",
                ("pending_customer", _now(), ticket_id),
            )
            connection.execute(
                "INSERT INTO events (ticket_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
                (ticket_id, "approved_resolution", serialized, _now()),
            )
            for item in action_items:
                connection.execute(
                    """
                    INSERT INTO tasks (ticket_id, title, description, owner, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (ticket_id, item.title, item.description, item.owner, "open", _now()),
                )
        return run_id
