"""Configure the actual app exporter; prove n8n-run child spans reached local Phoenix."""

import json
import os
import subprocess
import time
from pathlib import Path

import httpx
from verify_demo import APP, N8N, ROOT, call, env


def compose(*args, **kwargs):
    return subprocess.run(
        ["docker", "compose", *args], cwd=ROOT, check=True, capture_output=True, text=True, **kwargs
    )


def main():
    endpoint = "http://phoenix:6006/v1/traces"
    compose(
        "up",
        "-d",
        "--force-recreate",
        "--no-deps",
        "app",
        env={**os.environ, "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": endpoint},
    )
    sentinel = "PRIVATE_PHOENIX_SENTINEL"
    with httpx.Client(base_url="http://localhost:6006", timeout=5) as phoenix:
        for _ in range(90):
            try:
                if (
                    phoenix.get("/v1/projects").status_code == 200
                    and httpx.get(APP + "/health", timeout=2).status_code == 200
                ):
                    break
            except httpx.TransportError:
                pass
            time.sleep(1)
        else:
            raise RuntimeError("Actual app/Phoenix did not become ready")
        payload = dict(
            request_id="phoenix-" + str(time.time_ns()),
            name=sentinel,
            email="private-phoenix@example.test",
            phone="",
            message=sentinel + " CRM automation",
        )
        run = call(N8N, "/webhook/lead", payload, key="webhook")
        assert run["status"] == "offered"
        # Error responses can be embedded in n8n errors: require generic, body-free 422.
        response = httpx.post(
            APP + "/api/intake",
            headers={"X-Service-Key": env["SERVICE_KEY"]},
            json={"lead": {"email": sentinel, "message": sentinel}},
        )
        assert response.status_code == 422 and sentinel not in response.text
        response = httpx.post(
            N8N + "/webhook/lead",
            headers={"X-Webhook-Token": env["WEBHOOK_TOKEN"]},
            json={"email": sentinel, "message": sentinel},
            timeout=30,
        )
        assert response.is_error
        spans = []
        for _ in range(45):
            response = phoenix.get("/v1/projects/default/spans", params={"limit": 1000})
            if response.status_code == 200:
                spans = response.json()["data"]
                qualification = [
                    s
                    for s in spans
                    if s["name"] == "qualification" and s["attributes"].get("run_id") == run["run_id"]
                ]
                if qualification:
                    trace_id = qualification[0]["context"]["trace_id"]
                    correlated = [s for s in spans if s["context"]["trace_id"] == trace_id]
                    if {"run", "qualification", "llm.attempt", "llm.accounting"} <= {
                        s["name"] for s in correlated
                    }:
                        break
            time.sleep(1)
        else:
            raise AssertionError("Actual application correlated spans missing from Phoenix")
        by_id = {s["context"]["span_id"]: s for s in correlated}
        root = next(s for s in correlated if s["name"] == "run")
        qualification_span = next(s for s in correlated if s["name"] == "qualification")
        attempt = next(s for s in correlated if s["name"] == "llm.attempt")
        assert qualification_span["parent_id"] == root["context"]["span_id"]
        assert attempt["parent_id"] == qualification_span["context"]["span_id"]
        assert all(s["parent_id"] in by_id or s["parent_id"] is None for s in correlated)
        private_values = [
            sentinel,
            payload["email"],
            run["booking_token"],
            env["SERVICE_KEY"],
            env["WEBHOOK_TOKEN"],
        ]
        serialized = json.dumps(spans)
        # n8n temporarily persists in-flight work for crash recovery. Require its lifecycle
        # to finish and remove ALL retained copies, including soft-deleted rows; no row exclusions.
        for drain_attempt in range(30):
            lifecycle_drains = drain_attempt + 1
            stored = compose(
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "demo",
                "-d",
                "n8n",
                "-At",
                "-c",
                "SELECT data FROM execution_data",
            ).stdout
            remaining = int(
                compose(
                    "exec",
                    "-T",
                    "postgres",
                    "psql",
                    "-U",
                    "demo",
                    "-d",
                    "n8n",
                    "-At",
                    "-c",
                    "SELECT count(*) FROM execution_entity",
                ).stdout.strip()
            )
            if remaining == 0 and all(private not in stored for private in private_values):
                break
            time.sleep(1)
        else:
            labels = ["inquiry", "contact", "booking token", "service credential", "webhook credential"]
            categories = [
                label for label, value in zip(labels, private_values, strict=True) if value in stored
            ]
            metadata = compose(
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "demo",
                "-d",
                "n8n",
                "-At",
                "-c",
                "SELECT status, mode, count(*) FROM execution_entity GROUP BY status, mode",
            ).stdout
            raise AssertionError(
                "Retained n8n private categories: " + repr(categories) + "; status counts: " + metadata
            )
        logs = compose("logs", "--no-color", "app", "mock", "n8n", "bootstrap").stdout
        final_spans = phoenix.get("/v1/projects/default/spans", params={"limit": 1000})
        final_spans.raise_for_status()
        serialized = json.dumps(final_spans.json()["data"])
        state = call(APP, "/api/state")
        for private in private_values:
            assert private not in serialized, "Private value in exported spans"
            assert private not in logs, "Private value in container logs"
            assert private not in stored, "Private value in persisted n8n execution data"
            assert private not in json.dumps(state["alerts"]), "Private value in alert outbox"
        report = dict(
            status="PASS",
            git_sha=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            boundary="actual n8n → application OTLP HTTP exporter → local Phoenix",
            exporter_endpoint=endpoint,
            synthetic=True,
            run_id=run["run_id"],
            trace_id=trace_id,
            span_count=len(correlated),
            lifecycle_drain_attempts=lifecycle_drains,
            retained_n8n_execution_rows=remaining,
            spans=[
                dict(name=s["name"], span_id=s["context"]["span_id"], parent_id=s["parent_id"])
                for s in correlated
            ],
            privacy_surfaces=[
                "Phoenix spans",
                "app/mock/n8n/bootstrap logs",
                "n8n execution_data",
                "alert outbox",
            ],
        )
    Path("artifacts").mkdir(exist_ok=True)
    Path("artifacts/phoenix.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
