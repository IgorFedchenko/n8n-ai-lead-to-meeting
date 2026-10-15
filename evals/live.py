"""Opt-in paid evaluation infrastructure. No live runs are part of default verification."""

import argparse
import hashlib
import json
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import create_engine

from app import db, llm_budget
from app.models import Lead
from app.providers import IntegrationError, Providers, Retryable
from app.qualification import (
    PROMPT_HASH,
    PROMPT_VERSION,
    SCHEMA_HASH,
    attempt_hooks,
    usage_hook,
    validate_response,
)
from evals.outcomes import aggregate, select_rows


def arguments(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--approve-spend", action="store_true", required=True)
    parser.add_argument("--provider", choices=("anthropic", "openai-compatible"), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-calls", type=int, required=True)
    parser.add_argument(
        "--max-cost", type=Decimal, required=True, help="USD configured-rate reservation ceiling"
    )
    parser.add_argument(
        "--ledger", type=Path, required=True, help="Persistent private SQLite admission ledger"
    )
    parser.add_argument("--output", type=Path, required=True, help="Private result path")
    parser.add_argument(
        "--case-id", action="append", help="Explicit partial coverage; repeat to select case IDs"
    )
    return parser


@contextmanager
def metered(
    args: Any,
    *,
    prompt_version: str = PROMPT_VERSION,
    prompt_hash: str = PROMPT_HASH,
    schema_hash: str = SCHEMA_HASH,
) -> Iterator[tuple[Providers, str]]:
    if not (1 <= args.max_calls <= 12) or not args.max_cost.is_finite() or args.max_cost <= 0:
        raise ValueError("Use 1–12 physical calls and a positive finite max cost")
    if os.getenv("LLM_SPEND_APPROVED") != "true":
        raise ValueError("Operator must set LLM_SPEND_APPROVED=true after approving credentials/spend")
    # Never reads host keys. Each HTTP retry uses the same committed ledger and ceiling.
    os.environ.update(
        LLM_PROVIDER=args.provider,
        LLM_MODEL=args.model,
        LLM_MAX_ATTEMPTS=str(args.max_calls),
        LLM_RUN_BUDGET_USD=str(args.max_cost),
    )
    for rate in ("LLM_INPUT_USD_PER_MILLION", "LLM_OUTPUT_USD_PER_MILLION"):
        llm_budget.positive_decimal(rate)
    if not os.getenv("LLM_API_KEY") or not os.getenv("LLM_RATE_SOURCE"):
        raise ValueError("Operator must configure LLM_API_KEY and LLM_RATE_SOURCE")
    args.ledger.parent.mkdir(parents=True, exist_ok=True)
    before = db.engine
    db.engine = create_engine("sqlite:///" + str(args.ledger.resolve()))
    db.init_db()
    os.chmod(args.ledger, 0o600)
    run_id = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    owner = secrets.token_hex(16)
    with db.transaction() as session:
        session.add(
            db.Run(
                id=run_id,
                fingerprint=run_id,
                data={
                    "status": "evaluating",
                    "qualification_operation": {
                        "id": owner,
                        "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
                    },
                },
            )
        )
    adapter = Providers()
    adapter.mode = "live"
    admission = attempt_hooks.set(
        lambda: llm_budget.admit(
            run_id,
            "live",
            owner,
            prompt_version=prompt_version,
            prompt_hash=prompt_hash,
            schema_hash=schema_hash,
        )
    )
    recording = usage_hook.set(llm_budget.record)
    try:
        yield adapter, run_id
    finally:
        attempt_hooks.reset(admission)
        usage_hook.reset(recording)
        adapter.client.close()
        db.engine.dispose()
        db.engine = before


def private_write(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(report, stream, indent=2)


class GenerationCase(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    id: str
    input: Lead


def evaluate(args: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "origin": "live",
        "model": args.model,
        "prompt_hash": PROMPT_HASH,
        "partial": args.case_id is not None,
        "results": [],
    }
    try:
        rows = json.loads(args.dataset.read_text())
        selected = select_rows(rows, args.case_id)
        report.update(
            source_count=len(rows),
            required_count=len(selected),
            dataset_hash=hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
        )
        cases = [GenerationCase.model_validate(row) for row in selected]
        with metered(args) as (adapter, run_id):
            for case in cases:
                outcome: dict[str, Any] = {
                    "id": case.id,
                    "inquiry": case.input.message,
                    "origin": "live",
                    "passed": False,
                    "evaluated": False,
                }
                try:
                    for _repair in range(2):
                        raw = adapter.qualify(case.input.model_dump())
                        outcome["evaluated"] = True
                        try:
                            outcome["qualification"] = validate_response(raw).model_dump()
                            outcome.update(status="qualified", passed=True)
                            break
                        except ValueError:
                            outcome["status"] = "invalid_output"
                except (ValueError, RuntimeError, IntegrationError, Retryable) as error:
                    outcome["status"] = type(error).__name__
                report["results"].append(outcome)
            with db.transaction() as session:
                report["usage"] = llm_budget.summary(session, run_id)
    except (ValueError, OSError, RuntimeError, IntegrationError, Retryable) as error:
        report["error"] = type(error).__name__
    report.update(aggregate(report))
    return report


def main(argv: list[str] | None = None) -> None:
    parser = arguments(__doc__ or "Live evaluation")
    parser.add_argument("--dataset", type=Path, default=Path("evals/cases.json"))
    args = parser.parse_args(argv)
    report = evaluate(args)
    private_write(args.output, report)
    print(
        json.dumps({key: report[key] for key in ("status", "required_count", "result_count", "passed_count")})
    )
    raise SystemExit(0 if report["status"] in {"PASS", "PASS_PARTIAL"} else 1)


if __name__ == "__main__":
    main()
