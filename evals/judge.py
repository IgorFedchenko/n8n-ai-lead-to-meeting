"""Opt-in rubric judge; never controls routing or substitutes for deterministic safety checks."""

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app import db, llm_budget
from app.models import Qualification
from app.providers import IntegrationError, Retryable
from app.qualification import ROOT
from evals.live import arguments, metered, private_write
from evals.outcomes import aggregate, select_rows


class Judgment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    relevance: int = Field(ge=0, le=2)
    grounding: int = Field(ge=0, le=2)
    calibration: int = Field(ge=0, le=2)
    evidence: str = Field(min_length=1, max_length=600)
    invented_budget_or_identity: bool


def evaluate(args: Any) -> dict[str, Any]:
    system = (ROOT / "prompts/judge-v1.txt").read_text().strip()
    schema = Judgment.model_json_schema()
    prompt_hash = hashlib.sha256((ROOT / "prompts/judge-v1.txt").read_bytes()).hexdigest()
    schema_hash = hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()
    report: dict[str, Any] = {
        "origin": "live-judge",
        "rubric_version": "judge-v1",
        "model": args.model,
        "prompt_hash": prompt_hash,
        "schema_hash": schema_hash,
        "partial": args.case_id is not None,
        "results": [],
        "human_review": "Required: all failures and at least three passes",
    }
    try:
        source = json.loads(args.results.read_text())
        rows = source.get("results") if isinstance(source, dict) else None
        candidates = select_rows(rows, args.case_id)
        report.update(source_count=len(source["results"]), required_count=len(candidates))
        with metered(args, prompt_version="judge-v1", prompt_hash=prompt_hash, schema_hash=schema_hash) as (
            adapter,
            run_id,
        ):
            for row in candidates:
                outcome: dict[str, Any] = {"id": row["id"], "passed": False, "evaluated": False}
                if not isinstance(row.get("inquiry"), str) or not row.get("qualification"):
                    outcome["error"] = "missing_required_fields"
                    report["results"].append(outcome)
                    continue
                # Blind to expected scores, annotations and candidate provider/model.
                try:
                    qualification = Qualification.model_validate(row["qualification"])
                    raw = adapter.structured(
                        system, {"inquiry": row["inquiry"], "reason": qualification.reason}, schema
                    )
                    outcome["evaluated"] = True
                    verdict = Judgment.model_validate_json(raw)
                    scores = (verdict.relevance, verdict.grounding, verdict.calibration)
                    outcome.update(
                        judgment=verdict.model_dump(),
                        passed=min(scores) >= 1
                        and sum(scores) / 3 >= 1.7
                        and not verdict.invented_budget_or_identity,
                    )
                except (ValueError, RuntimeError, IntegrationError, Retryable) as error:
                    outcome["error"] = type(error).__name__
                report["results"].append(outcome)
            with db.transaction() as session:
                report["usage"] = llm_budget.summary(session, run_id)
    except (ValueError, OSError, RuntimeError, IntegrationError, Retryable) as error:
        report["error"] = type(error).__name__
    report.update(aggregate(report))
    return report


def main(argv: list[str] | None = None) -> None:
    parser = arguments(__doc__ or "Judge")
    parser.add_argument("--results", type=Path, required=True)
    args = parser.parse_args(argv)
    report = evaluate(args)
    private_write(args.output, report)
    print(
        json.dumps({key: report[key] for key in ("status", "required_count", "result_count", "passed_count")})
    )
    raise SystemExit(0 if report["status"] in {"PASS", "PASS_PARTIAL"} else 1)


if __name__ == "__main__":
    main()
