"""Versioned qualification boundary and per-physical-attempt accounting context."""

import hashlib
import json
import os
from collections.abc import Callable
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from app.models import Qualification

PROMPT_VERSION = "qualification-v1"
SCHEMA_VERSION = "qualification-v1"
ROOT = Path(__file__).resolve().parents[1]
SYSTEM_PROMPT = (ROOT / "prompts/qualification-v1.txt").read_text().strip()
PROMPT_HASH = hashlib.sha256((ROOT / "prompts/qualification-v1.txt").read_bytes()).hexdigest()
SCHEMA_HASH = hashlib.sha256(
    json.dumps(Qualification.model_json_schema(), sort_keys=True).encode()
).hexdigest()
attempt_hooks: ContextVar[Callable[[], str] | None] = ContextVar("llm_admit", default=None)
usage_hook: ContextVar[Callable[[str, str, dict[str, Any] | None, float], None] | None] = ContextVar(
    "llm_record", default=None
)


def output_limit() -> int:
    return int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "900"))


def validate_response(raw: str) -> Qualification:
    if len(raw.encode()) > 16000:
        raise ValueError("Qualification response exceeds limit")

    def unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result

    json.loads(raw, object_pairs_hook=unique_fields)
    return Qualification.model_validate_json(raw)


def replay_responses(responses: list[str]) -> dict[str, Any]:
    """The same repair/validation/routing boundary used by the runtime, without I/O."""
    for raw in responses[:2]:
        try:
            return {"status": "qualified", "qualification": validate_response(raw).model_dump()}
        except ValueError:
            continue
    return {"status": "manual_review", "manual_reason": "invalid_llm_output"}
