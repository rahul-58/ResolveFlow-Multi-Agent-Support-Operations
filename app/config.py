"""ResolveFlow configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    root_dir: Path = ROOT_DIR
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen3:4b")
    demo_mode: bool = _bool(os.getenv("DEMO_MODE"), False)
    support_db_path: Path = ROOT_DIR / os.getenv("SUPPORT_DB_PATH", "data/resolveflow.db")
    knowledge_base_dir: Path = ROOT_DIR / os.getenv("KNOWLEDGE_BASE_DIR", "data/knowledge_base")
    policies_dir: Path = ROOT_DIR / os.getenv("POLICIES_DIR", "data/policies")
    sample_tickets_path: Path = ROOT_DIR / os.getenv(
        "SAMPLE_TICKETS_PATH", "data/samples/tickets.json"
    )
    runs_dir: Path = ROOT_DIR / os.getenv("RUNS_DIR", "runs")
    max_agent_steps: int = int(os.getenv("MAX_AGENT_STEPS", "6"))
    max_knowledge_hits: int = int(os.getenv("MAX_KNOWLEDGE_HITS", "5"))


settings = Settings()
