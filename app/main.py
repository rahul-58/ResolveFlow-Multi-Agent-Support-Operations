"""ResolveFlow command-line interface."""

from __future__ import annotations

import argparse
from pathlib import Path

from .graph import run_agent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the local ResolveFlow support agent.")
    parser.add_argument("--ticket-id", default="TICK-1001")
    parser.add_argument("--subject", default="")
    parser.add_argument("--ticket", dest="ticket_text", default="")
    parser.add_argument("--ticket-file", type=Path)
    parser.add_argument("--demo", action="store_true", help="Run without connecting to Ollama.")
    parser.add_argument(
        "--approve-save",
        action="store_true",
        help="Approve saving the response and proposed internal actions.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    ticket_text = args.ticket_text
    if args.ticket_file:
        ticket_text = args.ticket_file.read_text(encoding="utf-8")
    result = run_agent(
        ticket_id=args.ticket_id,
        subject=args.subject,
        ticket_text=ticket_text,
        approve=args.approve_save,
        demo_mode=True if args.demo else None,
    )
    print(result.get("final_answer", "No final answer was produced."))
    print("\n--- execution trace ---")
    for event in result.get("event_log", []):
        print(event)


if __name__ == "__main__":
    main()

