"""Opt-in rubric judge; never controls routing or substitutes for deterministic safety checks."""

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.providers import IntegrationError, Retryable
from app.qualification import ROOT
from evals.live import arguments, metered, private_write


class Judgment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    relevance: int = Field(ge=0, le=2)
    grounding: int = Field(ge=0, le=2)
    calibration: int = Field(ge=0, le=2)
    evidence: str = Field(min_length=1, max_length=600)
    invented_budget_or_identity: bool


def main() -> None:
    parser = arguments(__doc__ or "Judge")
    parser.add_argument("--results", type=Path, required=True)
    args = parser.parse_args()
    system = (ROOT / "prompts/judge-v1.txt").read_text().strip()
    schema = Judgment.model_json_schema()
    prompt_hash = hashlib.sha256((ROOT / "prompts/judge-v1.txt").read_bytes()).hexdigest()
    schema_hash = hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()
    candidates = json.loads(args.results.read_text())["results"][: args.max_calls]
    results = []
    with metered(args, prompt_version="judge-v1", prompt_hash=prompt_hash, schema_hash=schema_hash) as (
        adapter,
        run_id,
    ):
        for row in candidates:
            if "qualification" not in row:
                continue
            # Blind to expected score, annotations and candidate provider/model.
            try:
                raw = adapter.structured(
                    system, {"inquiry": row["inquiry"], "reason": row["qualification"]["reason"]}, schema
                )
                verdict = Judgment.model_validate_json(raw)
                scores = (verdict.relevance, verdict.grounding, verdict.calibration)
                results.append(
                    {
                        "id": row["id"],
                        "judgment": verdict.model_dump(),
                        "passed": min(scores) >= 1
                        and sum(scores) / 3 >= 1.7
                        and not verdict.invented_budget_or_identity,
                    }
                )
            except (ValueError, RuntimeError, IntegrationError, Retryable) as error:
                results.append({"id": row["id"], "passed": False, "error": type(error).__name__})
        from app import db, llm_budget

        with db.transaction() as session:
            usage = llm_budget.summary(session, run_id)
    private_write(
        args.output,
        {
            "origin": "live-judge",
            "rubric_version": "judge-v1",
            "prompt_hash": prompt_hash,
            "schema_hash": schema_hash,
            "model": args.model,
            "usage": usage,
            "results": results,
            "human_review": "Required: all failures and at least three passes",
        },
    )


if __name__ == "__main__":
    main()
