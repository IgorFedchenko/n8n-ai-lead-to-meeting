import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app import db, main, mock
from app.models import Qualification
from app.providers import Providers


@pytest.fixture
def clients(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVICE_KEY", "test-service-key")
    monkeypatch.setenv("RETRY_DELAY_SCALE", "0")
    # .env.example deliberately has blank live credentials. Mocks must still work.
    monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", "")
    monkeypatch.setattr(db, "engine", create_engine("sqlite:///" + str(tmp_path / "app.db")))
    monkeypatch.setattr(mock, "engine", create_engine("sqlite:///" + str(tmp_path / "mock.db")))
    providers = Providers()
    monkeypatch.setattr(main, "providers", providers)
    with TestClient(mock.app) as remote, TestClient(main.app) as client:
        providers.client = remote
        client.headers["X-Service-Key"] = "test-service-key"
        yield client, remote


def lead(**overrides):
    return {
        "request_id": "test-001",
        "name": "Alex Example",
        "email": "alex@example.test",
        "phone": "+1 (202) 555-0101",
        "message": "Lead routing and CRM integration within four weeks.",
        **overrides,
    }


def pipeline(client, payload=None):
    response = client.post("/api/intake", json={"lead": payload or lead(), "execution_id": "42"})
    assert response.status_code == 200, response.text
    run_id = response.json()["run_id"]
    for step in ("qualify", "crm", "slots", "offer-email", "finish"):
        response = client.post(f"/api/runs/{run_id}/{step}")
        assert response.status_code == 200, response.text
    return response.json()


def test_end_to_end_with_replay_booking_and_email(clients):
    client, remote = clients
    first = pipeline(client)
    assert first["status"] == "offered"
    Qualification.model_validate(first["qualification"])
    assert pipeline(client)["deal_id"] == first["deal_id"]
    booking = {"run_id": first["run_id"], "token": first["booking_token"], "slot": first["slots"][0]}
    assert client.post("/api/book", json=booking).status_code == 200
    assert client.post("/api/book", json=booking).status_code == 200
    url = f"/api/runs/{first['run_id']}/confirmation-email"
    assert client.post(url).json()["status"] == "completed"
    assert client.post(url).status_code == 200
    state = remote.get("/state").json()
    assert [len(state[k]) for k in ("contacts", "deals", "events", "mail", "associations")] == [1, 1, 1, 2, 1]


@pytest.mark.parametrize(
    "scenario,attempts,status",
    [
        ("invalid_once", 2, "offered"),
        ("invalid", 2, "manual_review"),
        ("transient", 1, "offered"),
    ],
)
def test_llm_validation_and_retry(clients, scenario, attempts, status):
    client, remote = clients
    run = pipeline(client, lead(scenario=scenario))
    assert run["status"] == status
    assert run["llm_attempts"] == attempts
    if status == "manual_review":
        assert not remote.get("/state").json()["deals"]
        client.post("/api/monitor")
        alerts = client.get("/api/state").json()["alerts"]
        assert {a["reason"] for a in alerts} == {"invalid_llm_output", "zero_crm_writes"}
        assert all(a["delivered"] for a in alerts)


def test_identity_phone_upsert_and_payload_collision(clients):
    client, remote = clients
    first = pipeline(client)
    second = pipeline(client, lead(request_id="test-002", email="new@example.test"))
    assert second["contact_id"] == first["contact_id"]
    assert len(remote.get("/state").json()["contacts"]) == 1
    assert (
        client.post("/api/intake", json={"lead": lead(message="Changed submission content.")}).status_code
        == 409
    )


def test_auth_and_booking_guards(clients):
    client, _ = clients
    assert client.get("/api/state", headers={"X-Service-Key": "bad"}).status_code == 401
    run = pipeline(client)
    booking = {"run_id": run["run_id"], "token": "x" * 32, "slot": run["slots"][0]}
    assert client.post("/api/book", json=booking).status_code == 403
    booking["token"] = run["booking_token"]
    booking["slot"] = "2099-01-01T12:00:00+00:00"
    assert client.post("/api/book", json=booking).status_code == 409
    assert client.post(f"/api/runs/{run['run_id']}/confirmation-email").status_code == 409
    assert client.get("/public/offer", params={"run_id": run["run_id"]}).status_code == 403
    public = client.get(
        "/public/offer", params={"run_id": run["run_id"]}, headers={"X-Booking-Token": run["booking_token"]}
    )
    assert public.status_code == 200
    assert set(public.json()) == {"status", "slots", "booked_slot"}
    assert "no-referrer" in client.get("/booking").headers["referrer-policy"]


def test_calendar_conflict_and_alternative_slot(clients):
    client, _ = clients
    one = pipeline(client)
    two = pipeline(client, lead(request_id="test-002", email="two@example.test", phone="+12025550102"))
    assert one["slots"][0] == two["slots"][0]

    def book(run, slot):
        return client.post(
            "/api/book", json={"run_id": run["run_id"], "token": run["booking_token"], "slot": slot}
        )

    assert book(one, one["slots"][0]).status_code == 200
    assert book(two, two["slots"][0]).status_code == 409
    assert book(two, two["slots"][1]).status_code == 200


def test_partial_crm_failure_alert_and_resume_without_duplicate_contact(clients, monkeypatch):
    client, remote = clients
    run_id = client.post("/api/intake", json={"lead": lead(request_id="crm-fail-001")}).json()["run_id"]
    client.post(f"/api/runs/{run_id}/qualify")
    assert client.post(f"/api/runs/{run_id}/crm").status_code == 503
    client.post("/api/monitor")
    state = client.get("/api/state").json()
    assert state["runs"][0]["status"] == "failed"
    assert state["alerts"][0]["delivered"]
    original = main.providers.deal

    def restored(run_id, payload, qualification, contact):
        return original(run_id, {**payload, "request_id": "service-restored"}, qualification, contact)

    monkeypatch.setattr(main.providers, "deal", restored)
    assert client.post(f"/api/runs/{run_id}/crm").status_code == 200
    assert len(remote.get("/state").json()["contacts"]) == 1


def test_uncertain_smtp_is_not_automatically_resent(clients, monkeypatch):
    client, _ = clients
    calls = []

    def lost_ack(*args):
        calls.append(args)
        raise OSError("Connection lost after SMTP acceptance")

    monkeypatch.setattr(main.providers, "email", lost_ack)
    run_id = client.post("/api/intake", json={"lead": lead()}).json()["run_id"]
    for step in ("qualify", "crm", "slots"):
        client.post(f"/api/runs/{run_id}/{step}")
    assert client.post(f"/api/runs/{run_id}/offer-email").status_code == 502
    assert client.post(f"/api/runs/{run_id}/offer-email").json()["status"] == "manual_review"
    assert len(calls) == 1


def test_n8n_error_and_stalled_run_warnings(clients):
    client, _ = clients
    run_id = client.post("/api/intake", json={"lead": lead(), "execution_id": "42"}).json()["run_id"]
    with db.transaction() as session:
        db.get_run(session, run_id).updated_at = datetime.now(UTC) - timedelta(minutes=11)
    client.post("/api/monitor")
    client.post("/api/errors", json={"execution": {"id": "42", "error": {"message": "private body"}}})
    state = client.get("/api/state").json()
    assert state["runs"][0]["status"] == "failed"
    assert {a["reason"] for a in state["alerts"]} == {"stalled_execution", "n8n_execution_failed"}
    assert "private body" not in json.dumps(state)


def test_retention_purges_private_payload_and_journal(clients):
    client, _ = clients
    run = pipeline(client)
    with db.transaction() as session:
        db.get_run(session, run["run_id"]).updated_at = datetime.now(UTC) - timedelta(days=8)
    assert client.post("/api/maintenance").json() == {"purged": 1}
    assert client.get("/api/state").json() == {"runs": [], "journal": [], "alerts": []}


def test_workflows_have_real_error_handler_and_no_secrets():
    from pathlib import Path

    for path in Path("workflows").glob("*.json"):
        workflow = json.loads(path.read_text())
        if workflow["id"] != "leadErrors":
            assert workflow["settings"]["errorWorkflow"] == "leadErrors"
        assert workflow["settings"]["saveDataSuccessExecution"] == "none"
        assert "api-key" not in path.read_text()


@pytest.mark.parametrize("late_failure", [False, True])
def test_mail_owner_survives_active_duplicate_expiry_and_reconciliation(clients, monkeypatch, late_failure):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    client, _ = clients
    run_id = client.post("/api/intake", json={"lead": lead()}).json()["run_id"]
    for step in ("qualify", "crm", "slots"):
        assert client.post(f"/api/runs/{run_id}/{step}").status_code == 200
    entered, release = Event(), Event()
    calls = []

    def paused_sender(*args):
        calls.append(args)
        entered.set()
        assert release.wait(5)
        if late_failure:
            raise OSError("synthetic acknowledgement lost")

    monkeypatch.setattr(main.providers, "email", paused_sender)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(main.send_email, run_id, "offer")
        assert entered.wait(5)
        try:
            active = main.send_email(run_id, "offer")
            assert active["operation_status"] == "in_progress"
            assert active["status"] == "slots_ready"
            with db.transaction() as session:
                run = db.get_run(session, run_id)
                data = dict(run.data)
                data["offer_reservation"] = {
                    **data["offer_reservation"],
                    "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                }
                db.save(session, run, data, "synthetic_expiry")
            main.monitor()
            with db.transaction() as session:
                run = db.get_run(session, run_id)
                assert run.data["offer_mail"] == "uncertain"
                data = {
                    **run.data,
                    "offer_mail": "sent",
                    "status": "booked",
                    "offer_reservation": {"id": "operator-reconciled"},
                    "booked_slot": "synthetic-preserved-slot",
                }
                db.save(session, run, data, "synthetic_reconciliation")
        finally:
            release.set()
        outcome = pending.result(timeout=5)
    assert len(calls) == 1
    assert outcome["status"] == "booked"
    assert outcome["offer_mail"] == "sent"
    assert outcome["booked_slot"] == "synthetic-preserved-slot"
    events = client.get("/api/state").json()["journal"]
    assert events[-1]["event"] == ("offer_mail_late_failure" if late_failure else "offer_mail_late_success")


def test_late_execution_error_keeps_success_and_spans_private(clients):
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from app.telemetry import provider

    client, _ = clients
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    run = pipeline(client)
    client.post("/api/errors", json={"execution": {"id": "42", "error": {"message": "private-error-body"}}})
    state = client.get("/api/state").json()
    assert state["runs"][0]["status"] == "offered"
    assert state["runs"][0]["execution_outcomes"]["42"] == "failed"
    spans = exporter.get_finished_spans()
    assert {s.name for s in spans} >= {"run", "stage", "qualification", "llm.attempt", "mail"}
    attributes = json.dumps([dict(s.attributes) for s in spans])
    for private in (lead()["email"], lead()["message"], run["booking_token"], "private-error-body"):
        assert private not in attributes
    assert run["llm_usage"]["physical_attempts"] == 1
    assert run["llm_usage"]["attempts"][0]["input_tokens"] is None


def test_durable_cap_counts_transient_attempt_before_any_retry(clients, monkeypatch):
    client, remote = clients
    monkeypatch.setenv("LLM_MAX_ATTEMPTS", "1")
    payload = lead(scenario="transient")
    run_id = client.post("/api/intake", json={"lead": payload}).json()["run_id"]
    response = client.post(f"/api/runs/{run_id}/qualify")
    assert response.status_code == 200
    assert response.json()["manual_reason"] == "llm_budget_exhausted"
    assert response.json()["llm_usage"]["physical_attempts"] == 1
    assert response.json()["llm_usage"]["attempts"][0]["status"] == "http_error"
    assert response.json()["llm_usage"]["attempts"][0]["reserved_usd"] == "0"
    client.post(f"/api/runs/{run_id}/qualify")
    state = remote.get("/state").json()
    assert state["llm"][0]["attempts"] == 1
