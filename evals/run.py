"""Replay synthetic raw HTTP responses through the actual durable qualification route."""

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import create_engine

from app.models import Lead
from app.qualification import PROMPT_HASH, PROMPT_VERSION, SCHEMA_HASH, validate_response

ROOT = Path(__file__).parent


def evaluate(
    cases_path: Path = ROOT / "cases.json", responses_path: Path = ROOT / "responses.json"
) -> dict[str, Any]:
    # Imported here: offline unit fixtures are the only transport available to this run.
    from fastapi.testclient import TestClient

    from app import db, main
    from app.providers import Providers

    cases = json.loads(cases_path.read_text())
    fixtures = json.loads(responses_path.read_text())
    by_id = {row["id"]: row for row in fixtures}
    if len(cases) < 12 or len(by_id) != len(fixtures) or set(by_id) != {row["id"] for row in cases}:
        raise ValueError("Missing/duplicate evaluation cases or responses")
    results = []
    initial_valid = 0
    engine_before, providers_before = db.engine, main.providers
    old_env = {key: os.environ.get(key) for key in ("SERVICE_KEY", "RETRY_DELAY_SCALE")}
    os.environ.update(SERVICE_KEY="synthetic-eval-service", RETRY_DELAY_SCALE="0")
    try:
        with tempfile.TemporaryDirectory() as directory:
            db.engine = create_engine("sqlite:///" + directory + "/eval.db")
            adapter = Providers()
            adapter.mode = "mock"
            main.providers = adapter
            with TestClient(main.app) as client:
                client.headers["X-Service-Key"] = "synthetic-eval-service"
                for case in cases:
                    fixture = by_id[case["id"]]
                    if (
                        fixture["prompt_sha256"] != PROMPT_HASH
                        or fixture["schema_sha256"] != SCHEMA_HASH
                        or fixture["prompt_version"] != PROMPT_VERSION
                        or fixture["origin"] != "synthetic"
                    ):
                        raise ValueError("Stale or unsupported fixture provenance")
                    sequence = list(fixture["responses"])
                    initial_ok = True
                    try:
                        validate_response(sequence[0])
                    except ValueError:
                        initial_ok = False
                    initial_valid += int(initial_ok)
                    calls: list[str] = []

                    def transport(
                        request: httpx.Request, calls: list[str] = calls, sequence: list[str] = sequence
                    ) -> httpx.Response:
                        calls.append(request.method)
                        if not sequence:
                            raise AssertionError("Unexpected additional physical call")
                        return httpx.Response(
                            200, json={"content": [{"type": "text", "text": sequence.pop(0)}]}
                        )

                    adapter.client = httpx.Client(transport=httpx.MockTransport(transport))
                    lead = Lead.model_validate(case["input"])
                    response = client.post("/api/intake", json={"lead": lead.model_dump()})
                    response.raise_for_status()
                    outcome = client.post(f"/api/runs/{lead.key}/qualify")
                    outcome.raise_for_status()
                    result = outcome.json()
                    expected = case["expected"]
                    fields = {}
                    if expected["status"] == "qualified":
                        actual = result.get("qualification", {})
                        fields = {
                            key: actual.get(key) == expected[key]
                            for key in ("budget", "currency", "request_type", "timeline")
                        }
                        fields["score"] = (
                            expected["score_range"][0]
                            <= actual.get("score", -1)
                            <= expected["score_range"][1]
                        )
                        fields["reason"] = all(
                            term.casefold() in actual.get("reason", "").casefold()
                            for term in expected["reason_terms"]
                        )
                    routed = result["status"] == expected["status"]
                    safe = not result.get("qualification") or set(result["qualification"]) == {
                        "budget",
                        "currency",
                        "timeline",
                        "request_type",
                        "score",
                        "reason",
                    }
                    passed = (
                        routed and safe and all(fields.values()) and len(calls) == len(fixture["responses"])
                    )
                    results.append(
                        {
                            "id": case["id"],
                            "origin": "synthetic",
                            "passed": passed,
                            "initial_schema_valid": initial_ok,
                            "routing_correct": routed,
                            "structural_safety": safe,
                            "fields": fields,
                            "physical_calls": len(calls),
                        }
                    )
                    adapter.client.close()
            db.engine.dispose()
    finally:
        db.engine, main.providers = engine_before, providers_before
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    field_values = [value for row in results for value in row["fields"].values()]
    return {
        "status": "PASS" if all(row["passed"] for row in results) else "FAIL",
        "evidence": "synthetic HTTP replay regression; no model was called",
        "cases": len(results),
        "initial_schema_valid_rate": initial_valid / len(results),
        "field_correctness": sum(field_values) / len(field_values),
        "routing_correct_rate": sum(row["routing_correct"] for row in results) / len(results),
        "structural_safety_rate": sum(row["structural_safety"] for row in results) / len(results),
        "dataset_sha256": hashlib.sha256(cases_path.read_bytes()).hexdigest(),
        "prompt_sha256": PROMPT_HASH,
        "schema_sha256": SCHEMA_HASH,
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", required=True)
    parser.add_argument("--output", type=Path, default=Path("artifacts/evals.json"))
    args = parser.parse_args()
    report = evaluate()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "results"}))
    raise SystemExit(0 if report["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
