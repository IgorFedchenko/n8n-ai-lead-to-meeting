"""Exercise optional CLI exit contracts using only intercepted synthetic HTTP."""

import json
from pathlib import Path

import httpx
import pytest

from app.providers import Providers
from evals import judge, live

QUALIFICATION = {
    "budget": 6000,
    "currency": "USD",
    "timeline": "3 weeks",
    "request_type": "automation",
    "score": 95,
    "reason": "Synthetic n8n CRM integration requirement.",
}


@pytest.fixture
def cli(tmp_path, monkeypatch):
    for key, value in {
        "LLM_API_KEY": "synthetic-fixture",
        "LLM_SPEND_APPROVED": "true",
        "LLM_RATE_SOURCE": "synthetic-rates",
        "LLM_INPUT_USD_PER_MILLION": "1",
        "LLM_OUTPUT_USD_PER_MILLION": "1",
        "LLM_PROVIDER": "anthropic",
        "LLM_MODEL": "synthetic",
        "LLM_MAX_ATTEMPTS": "6",
        "LLM_RUN_BUDGET_USD": "1",
        "RETRY_DELAY_SCALE": "0",
    }.items():
        monkeypatch.setenv(key, value)
    calls = []

    def configure(raw, status=200):
        def transport(request):
            calls.append(request)
            return httpx.Response(status, json={"content": [{"type": "text", "text": raw}]})

        monkeypatch.setattr(Providers, "client", httpx.Client(transport=httpx.MockTransport(transport)))

    def arguments(cost="1"):
        return [
            "--approve-spend",
            "--provider",
            "anthropic",
            "--model",
            "synthetic",
            "--max-calls",
            "2",
            "--max-cost",
            cost,
            "--ledger",
            str(tmp_path / "ledger.db"),
            "--output",
            str(tmp_path / "output.json"),
        ]

    return tmp_path, calls, configure, arguments


@pytest.mark.parametrize("scenario", ["empty", "invalid", "admission", "provider", "valid", "partial"])
def test_generation_exit_and_coverage(cli, scenario):
    directory, calls, configure, arguments = cli
    rows = json.loads(Path("evals/cases.json").read_text())[: 2 if scenario == "partial" else 1]
    if scenario == "empty":
        rows = []
    source = directory / "input.json"
    source.write_text(json.dumps(rows))
    configure(
        "not JSON" if scenario == "invalid" else json.dumps(QUALIFICATION),
        status=400 if scenario == "provider" else 200,
    )
    args = arguments("0.00001" if scenario == "admission" else "1") + ["--dataset", str(source)]
    if scenario == "partial":
        args += ["--case-id", "E01"]
    with pytest.raises(SystemExit) as stopped:
        live.main(args)
    success = scenario in {"valid", "partial"}
    assert stopped.value.code == (0 if success else 1)
    report = json.loads((directory / "output.json").read_text())
    assert report["status"] == ("PASS_PARTIAL" if scenario == "partial" else "PASS" if success else "FAIL")
    if scenario in {"empty", "admission"}:
        assert not calls
    if scenario == "partial":
        assert report["source_count"] == 2
        assert report["required_count"] == report["passed_count"] == 1
    if scenario == "invalid":
        assert len(calls) == 2


@pytest.mark.parametrize("scenario", ["empty", "missing", "rubric", "admission", "valid"])
def test_judge_exit_and_required_candidates(cli, scenario):
    directory, calls, configure, arguments = cli
    rows = [{"id": "E01", "inquiry": "Synthetic n8n inquiry", "qualification": QUALIFICATION}]
    if scenario == "empty":
        rows = []
    if scenario == "missing":
        rows[0].pop("qualification")
    source = directory / "input.json"
    source.write_text(json.dumps({"results": rows}))
    score = 0 if scenario == "rubric" else 2
    configure(
        json.dumps(
            {
                "relevance": score,
                "grounding": score,
                "calibration": score,
                "evidence": "Synthetic inquiry evidence",
                "invented_budget_or_identity": False,
            }
        )
    )
    args = arguments("0.00001" if scenario == "admission" else "1") + ["--results", str(source)]
    with pytest.raises(SystemExit) as stopped:
        judge.main(args)
    assert stopped.value.code == (0 if scenario == "valid" else 1)
    report = json.loads((directory / "output.json").read_text())
    assert report["status"] == ("PASS" if scenario == "valid" else "FAIL")
    if scenario in {"empty", "missing", "admission"}:
        assert not calls
    if scenario == "missing":
        assert report["failed_count"] == 1
        assert report["results"][0]["error"] == "missing_required_fields"


@pytest.mark.parametrize("exit_code", [1, 2])
def test_dependency_audit_findings_and_tool_errors_fail(monkeypatch, tmp_path, exit_code):
    import subprocess

    from scripts import audit_dependencies

    monkeypatch.chdir(tmp_path)
    commands = []

    def failed_audit(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, exit_code)

    monkeypatch.setattr(audit_dependencies.subprocess, "run", failed_audit)
    with pytest.raises(SystemExit) as stopped:
        audit_dependencies.main()
    assert stopped.value.code != 0
    assert len(commands) == 2
    assert {command[command.index("-r") + 1] for command in commands} == set(audit_dependencies.LOCKS)
