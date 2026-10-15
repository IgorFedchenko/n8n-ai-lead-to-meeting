"""Conservative configured-rate accounting, separate from provider invoices."""

import os
import secrets
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select

from app.db import LLMAttempt, get_run, transaction
from app.qualification import PROMPT_HASH, PROMPT_VERSION, SCHEMA_HASH, output_limit
from app.telemetry import operation


class BudgetDenied(ValueError):
    pass


def positive_decimal(name: str) -> Decimal:
    try:
        value = Decimal(os.environ[name])
    except (KeyError, InvalidOperation) as error:
        raise BudgetDenied(f"Configure {name}") from error
    if not value.is_finite() or value <= 0:
        raise BudgetDenied(f"{name} must be finite and positive")
    return value


def admit(
    run_id: str,
    mode: str,
    owner: str,
    *,
    prompt_version: str = PROMPT_VERSION,
    prompt_hash: str = PROMPT_HASH,
    schema_hash: str = SCHEMA_HASH,
) -> str:
    max_input = int(os.getenv("LLM_MAX_INPUT_BYTES", "20000"))
    max_output = output_limit()
    max_attempts = int(os.getenv("LLM_MAX_ATTEMPTS", "6"))
    if not (1 <= max_attempts <= 12 and 1 <= max_input <= 64000 and 1 <= max_output <= 4000):
        raise BudgetDenied("Invalid hard LLM limits")
    cost = Decimal(0)
    if mode == "live":
        if os.getenv("LLM_SPEND_APPROVED") != "true" or not os.getenv("LLM_RATE_SOURCE", "").strip():
            raise BudgetDenied("Live spend requires approval and rate provenance")
        if not os.getenv("LLM_MODEL") or not os.getenv("LLM_API_KEY"):
            raise BudgetDenied("Configure live model and credentials")
        cost = (
            Decimal(max_input) * positive_decimal("LLM_INPUT_USD_PER_MILLION")
            + Decimal(max_output) * positive_decimal("LLM_OUTPUT_USD_PER_MILLION")
        ) / Decimal(1000000)
        ceiling = positive_decimal("LLM_RUN_BUDGET_USD")
    else:
        ceiling = Decimal(0)
    with transaction() as session:
        run = get_run(session, run_id)
        active = run.data.get("qualification_operation", {})
        if (
            active.get("id") != owner
            or run.data["status"] == "manual_review"
            or datetime.fromisoformat(active["expires_at"]) <= datetime.now(UTC)
        ):
            raise BudgetDenied("Qualification ownership changed")
        attempts = list(session.scalars(select(LLMAttempt).where(LLMAttempt.run_id == run_id)))
        reserved = sum((Decimal(a.data["reserved_usd"]) for a in attempts), Decimal(0))
        if len(attempts) >= max_attempts or reserved + cost > ceiling:
            raise BudgetDenied("Durable LLM attempt/cost limit reached")
        identity = secrets.token_hex(16)
        session.add(
            LLMAttempt(
                id=identity,
                run_id=run_id,
                data={
                    "attempt": len(attempts) + 1,
                    "status": "reserved",
                    "reserved_usd": str(cost),
                    "provider": os.getenv("LLM_PROVIDER", "anthropic"),
                    "model": os.getenv("LLM_MODEL") or "synthetic-demo",
                    "synthetic": mode == "mock",
                    "prompt_version": prompt_version,
                    "prompt_hash": prompt_hash,
                    "schema_hash": schema_hash,
                    "input_rate_usd_per_million": os.getenv("LLM_INPUT_USD_PER_MILLION")
                    if mode == "live"
                    else "0",
                    "output_rate_usd_per_million": os.getenv("LLM_OUTPUT_USD_PER_MILLION")
                    if mode == "live"
                    else "0",
                    "max_input_bytes": max_input,
                    "max_output_tokens": max_output,
                    "rate_source": os.getenv("LLM_RATE_SOURCE", "synthetic-zero"),
                    "input_tokens": None,
                    "output_tokens": None,
                    "estimated_usage_usd": None,
                },
            )
        )
    return identity


def record(identity: str, status: str, usage: dict[str, Any] | None, elapsed: float) -> None:
    with transaction() as session:
        item = session.get(LLMAttempt, identity)
        if item is None:
            return
        data = dict(item.data)
        data.update(status=status, elapsed_ms=round(elapsed, 3))
        if isinstance(usage, dict):
            for target, aliases in (
                ("input_tokens", ("input_tokens", "prompt_tokens")),
                ("output_tokens", ("output_tokens", "completion_tokens")),
            ):
                value = next((usage[key] for key in aliases if key in usage), None)
                if type(value) is int and value >= 0:
                    data[target] = value
        if data["synthetic"]:
            data["estimated_usage_usd"] = "0"
        elif data["input_tokens"] is not None and data["output_tokens"] is not None:
            cost = (
                Decimal(data["input_tokens"]) * Decimal(data["input_rate_usd_per_million"])
                + Decimal(data["output_tokens"]) * Decimal(data["output_rate_usd_per_million"])
            ) / Decimal(1000000)
            data["estimated_usage_usd"] = str(cost)
        item.data = data
        with operation(
            "llm.accounting",
            run_id=item.run_id,
            **{
                key: value
                for key, value in data.items()
                if key
                in {
                    "attempt",
                    "status",
                    "prompt_version",
                    "prompt_hash",
                    "schema_hash",
                    "model",
                    "provider",
                    "input_tokens",
                    "output_tokens",
                    "elapsed_ms",
                    "reserved_usd",
                }
            },
        ):
            pass


def summary(session: Any, run_id: str) -> dict[str, Any]:
    rows = list(session.scalars(select(LLMAttempt).where(LLMAttempt.run_id == run_id)))

    def known_sum(key: str) -> int | None:
        values = [row.data.get(key) for row in rows]
        return sum(values) if all(type(value) is int for value in values) else None

    costs = [row.data.get("estimated_usage_usd") for row in rows]
    return {
        "physical_attempts": len(rows),
        "reserved_usd": str(sum((Decimal(a.data["reserved_usd"]) for a in rows), Decimal(0))),
        "input_tokens": known_sum("input_tokens"),
        "output_tokens": known_sum("output_tokens"),
        "estimated_usage_usd": (
            str(sum((Decimal(value) for value in costs), Decimal(0)))
            if all(value is not None for value in costs)
            else None
        ),
        "elapsed_ms": sum(a.data.get("elapsed_ms", 0) for a in rows),
        "synthetic": all(a.data.get("synthetic", False) for a in rows),
        "attempts": [a.data for a in rows],
    }
