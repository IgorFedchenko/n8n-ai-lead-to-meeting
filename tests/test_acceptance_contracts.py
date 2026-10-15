"""Requirements-derived protocol, privacy, configuration and replay controls."""

import copy
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from test_pipeline import lead, pipeline

from app import db, main
from app.config import LiveConfiguration
from app.models import Qualification
from app.providers import IntegrationError
from app.qualification import validate_response


@pytest.mark.parametrize(
    "budget,currency,amount",
    [
        (0, "USD", "0"),
        (12500, "USD", "12500"),
        (12500, "EUR", None),
        (12500, "unknown", None),
        (None, "USD", None),
    ],
)
def test_currency_create_and_patch_wire_and_remote_state(clients, monkeypatch, budget, currency, amount):
    _, remote = clients
    monkeypatch.setenv("HUBSPOT_CURRENCY", "USD")
    adapter = main.providers
    contact = adapter.contact(lead())
    qualification = dict(
        budget=budget,
        currency=currency,
        timeline="unknown",
        request_type="automation",
        score=80,
        reason="Synthetic intake automation request",
    )
    original = adapter.request
    wire = []

    def recording(method, url, **kwargs):
        if "/objects/deals" in url and method in {"POST", "PATCH"}:
            wire.append((method, copy.deepcopy(kwargs["json"]["properties"])))
        return original(method, url, **kwargs)

    monkeypatch.setattr(adapter, "request", recording)
    identifier = adapter.deal("a" * 64, lead(), qualification, contact)
    assert wire[0][0] == "POST"
    assert wire[0][1].get("amount") == amount
    original(
        "PATCH",
        adapter.crm_base + "/crm/v3/objects/deals/" + identifier,
        json={"properties": {"amount": "999"}},
    )
    assert adapter.deal("a" * 64, lead(), qualification, contact) == identifier
    assert wire[-1][0] == "PATCH"
    assert wire[-1][1]["amount"] == (amount if amount is not None else "")
    deal = remote.get("/state").json()["deals"][0]
    assert deal["properties"]["amount"] == (amount if amount is not None else "")
    assert json.loads(deal["properties"]["description"])["currency"] == currency


@pytest.mark.parametrize(
    "change",
    [
        dict(budget=-1),
        dict(budget=float("nan")),
        dict(budget=float("inf")),
        dict(budget="125"),
        dict(score="80"),
        dict(request_type="arbitrary"),
        dict(recipient="attacker@example.test"),
        dict(reason="x" * 801),
    ],
)
def test_strict_qualification_rejects_unsafe_fields(change):
    value = dict(
        budget=0,
        currency="USD",
        timeline="unknown",
        request_type="automation",
        score=50,
        reason="No funding approved yet",
    )
    value.update(change)
    with pytest.raises(ValueError):
        validate_response(json.dumps(value))


@pytest.mark.parametrize("raw", ["null", "{} prose", '{"score":1,"score":2}', "x" * 16001])
def test_parser_rejects_ambiguous_or_oversize_raw(raw):
    with pytest.raises(ValueError):
        validate_response(raw)


def test_intake_validation_response_does_not_echo_inputs(clients):
    client, _ = clients
    sentinel = "PRIVATE_VALIDATION_SENTINEL"
    for payload in (
        {"lead": {"email": sentinel, "message": sentinel}},
        {"lead": sentinel},
        {"extra": sentinel},
    ):
        response = client.post("/api/intake", json=payload)
        assert response.status_code == 422
        assert sentinel not in response.text


@pytest.mark.parametrize("missing", LiveConfiguration().required)
def test_each_live_credential_required_before_io(monkeypatch, missing):
    for name in LiveConfiguration().required:
        monkeypatch.setenv(name, "synthetic-placeholder")
    monkeypatch.delenv(missing)
    with pytest.raises(RuntimeError, match=missing):
        LiveConfiguration().validate()


@pytest.mark.parametrize("value", ["", "usd", "US", "NaN"])
def test_invalid_crm_account_currency_fails_configuration(monkeypatch, value):
    for name in LiveConfiguration().required:
        monkeypatch.setenv(name, "synthetic-placeholder")
    for name in ("LLM_INPUT_USD_PER_MILLION", "LLM_OUTPUT_USD_PER_MILLION", "LLM_RUN_BUDGET_USD"):
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("LLM_SPEND_APPROVED", "true")
    monkeypatch.setenv("HUBSPOT_CURRENCY", value)
    with pytest.raises(RuntimeError, match="CURRENCY"):
        LiveConfiguration().validate()


def test_late_error_after_later_completed_execution_preserves_all_business_ids(clients):
    client, _ = clients
    run = pipeline(client)
    client.post("/api/intake", json={"lead": lead(), "execution_id": "later-success"})
    body = {"run_id": run["run_id"], "token": run["booking_token"], "slot": run["slots"][0]}
    booked = client.post("/api/book", json=body).json()
    completed = client.post(f"/api/runs/{run['run_id']}/confirmation-email").json()
    for _ in range(2):
        assert (
            client.post(
                "/api/errors", json={"execution": {"id": "42", "error": {"message": "PRIVATE"}}}
            ).status_code
            == 200
        )
    current = client.get("/api/state").json()["runs"][0]
    assert current["status"] == "completed"
    for key in ("deal_id", "contact_id", "booking_token", "offer_mail", "confirmation_mail"):
        assert current[key] == completed[key]
    assert current["event_id"] == booked["event_id"]
    assert current["execution_outcomes"]["42"] == "failed"


def test_retention_exceptions_and_resolution(clients):
    client, _ = clients
    identities = {}
    for kind in ("safe", "active", "uncertain", "manual", "pending", "fresh"):
        run_id = client.post("/api/intake", json={"lead": lead(request_id="retain-" + kind)}).json()["run_id"]
        identities[kind] = run_id
        with db.transaction() as session:
            run = db.get_run(session, run_id)
            data = {**run.data, "status": "completed"}
            if kind == "active":
                data["qualification_operation"] = {"id": "owner"}
            if kind == "uncertain":
                data["offer_mail"] = "uncertain"
            if kind == "manual":
                data["status"] = "manual_review"
            db.save(session, run, data, "fixture")
            if kind != "fresh":
                run.updated_at = datetime.now(UTC) - timedelta(days=8)
            if kind == "pending":
                db.alert(session, run_id, "undelivered")
    assert client.post("/api/maintenance").json()["purged"] == 1
    assert {r["id"] for r in client.get("/api/state").json()["runs"]} == set(identities.values()) - {
        identities["safe"]
    }
    with db.transaction() as session:
        run = db.get_run(session, identities["uncertain"])
        db.save(session, run, {**run.data, "offer_mail": "sent"}, "reconciled")
        run.updated_at = datetime.now(UTC) - timedelta(days=8)
    assert client.post("/api/maintenance").json()["purged"] == 1


@pytest.mark.parametrize("target", ["create", "patch", "lookup"])
def test_malformed_consumed_crm_id_and_corrected_retry(clients, monkeypatch, target):
    client, remote = clients
    run_id = client.post("/api/intake", json={"lead": lead()}).json()["run_id"]
    client.post(f"/api/runs/{run_id}/qualify")
    adapter = main.providers
    if target != "create":
        adapter.contact(lead())
    if target == "lookup":
        adapter.deal(
            run_id,
            lead(),
            Qualification(
                budget=0,
                currency="USD",
                timeline="unknown",
                request_type="automation",
                score=50,
                reason="Synthetic intake",
            ).model_dump(),
            remote.get("/state").json()["contacts"][0]["id"],
        )
    original = adapter.request

    def broken(method, url, **kwargs):
        if (
            (target == "create" and method == "POST" and url.endswith("/contacts"))
            or (target == "patch" and method == "PATCH" and "/contacts/" in url)
            or (target == "lookup" and method == "GET" and "/deals/" in url)
        ):
            return httpx.Response(200, json={"id": None})
        return original(method, url, **kwargs)

    monkeypatch.setattr(adapter, "request", broken)
    assert client.post(f"/api/runs/{run_id}/crm").status_code == 502
    assert client.get("/api/state").json()["runs"][0]["failed_step"] == "crm"
    monkeypatch.setattr(adapter, "request", original)
    assert client.post(f"/api/runs/{run_id}/crm").status_code == 200
    assert len(remote.get("/state").json()["deals"]) == 1


@pytest.mark.parametrize(
    "shape",
    [
        {"calendars": None},
        {"calendars": {"demo": {"busy": None}}},
        {"calendars": {"demo": {"busy": [{"start": "bad", "end": "bad"}]}}},
        {"calendars": {}},
    ],
)
def test_malformed_calendar_consumed_shapes(clients, monkeypatch, shape):
    monkeypatch.setattr(main.providers, "request", lambda *a, **kw: httpx.Response(200, json=shape))
    with pytest.raises(IntegrationError):
        main.providers.free(["2099-01-01T12:00:00+00:00"])


@pytest.mark.parametrize("mutation", ["hash", "semantic", "missing", "corrupt"])
def test_eval_negatives_are_rejected(tmp_path, mutation):
    from evals.run import evaluate

    responses = json.loads(Path("evals/responses.json").read_text())
    if mutation == "hash":
        responses[0]["prompt_sha256"] = "0" * 64
    elif mutation == "semantic":
        value = json.loads(responses[0]["responses"][0])
        value["request_type"] = "other"
        responses[0]["responses"][0] = json.dumps(value)
    elif mutation == "missing":
        responses.pop()
    path = tmp_path / "responses.json"
    path.write_text("broken" if mutation == "corrupt" else json.dumps(responses))
    if mutation == "semantic":
        assert evaluate(responses_path=path)["status"] == "FAIL"
    else:
        with pytest.raises(ValueError):
            evaluate(responses_path=path)


def test_real_secret_scanner_positive_negative_controls(tmp_path):
    import shutil
    import subprocess
    import sys

    script = Path("scripts/scan_secrets.py").resolve()
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".secrets.baseline").write_text('{"results": {}}')
    (tmp_path / "clean.txt").write_text("Ordinary product documentation\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    clean = subprocess.run([sys.executable, str(script)], cwd=tmp_path, capture_output=True)
    assert clean.returncode == 0
    # Deliberately synthetic AWS-shaped credential; never a functional key.
    (tmp_path / "control.txt").write_text('aws_access_key_id = "' + "AKIA" + "QWERTYUIOPASDFGH" + '"\n')
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    found = subprocess.run([sys.executable, str(script)], cwd=tmp_path, capture_output=True)
    assert found.returncode == 1
    assert b"control.txt" in found.stdout
    assert shutil.which("git")


def test_conflicting_email_phone_never_creates_a_deal(clients):
    client, remote = clients
    adapter = main.providers
    adapter.contact(lead(email="one@example.test", phone="+12025550101"))
    adapter.contact(lead(email="two@example.test", phone="+12025550102"))
    run_id = client.post(
        "/api/intake", json={"lead": lead(email="one@example.test", phone="+12025550102")}
    ).json()["run_id"]
    client.post(f"/api/runs/{run_id}/qualify")
    assert client.post(f"/api/runs/{run_id}/crm").status_code == 502
    assert remote.get("/state").json()["deals"] == []


def test_booking_other_identity_expired_and_past_slot_guards(clients):
    client, remote = clients
    run = pipeline(client)
    other = pipeline(client, lead(request_id="other-identity", email="other@example.test", phone=""))
    body = {"run_id": run["run_id"], "token": other["booking_token"], "slot": run["slots"][0]}
    assert client.post("/api/book", json=body).status_code == 403
    assert client.post("/api/book", json={**body, "token": ""}).status_code == 422
    with db.transaction() as session:
        row = db.get_run(session, run["run_id"])
        db.save(
            session,
            row,
            {**row.data, "offered_at": (datetime.now(UTC) - timedelta(days=8)).isoformat()},
            "expired",
        )
    assert client.post("/api/book", json={**body, "token": run["booking_token"]}).status_code == 410
    assert remote.get("/state").json()["events"] == []


def test_live_demo_routes_disabled_recipient_page_remains_token_guarded(clients, monkeypatch):
    client, _ = clients
    monkeypatch.setattr(main.providers, "mode", "live")
    assert client.get("/").status_code == 404
    for method, path in [
        ("GET", "/demo/state"),
        ("GET", "/demo/mock"),
        ("POST", "/demo/lead"),
        ("POST", "/demo/book"),
    ]:
        assert client.request(method, path, json={}).status_code == 404
    assert client.get("/booking").status_code == 200
    assert client.get("/public/offer", params={"run_id": "a" * 64}).status_code in {403, 404}


def test_notification_outbox_failure_then_success_and_private_provider_body(clients, monkeypatch):
    client, remote = clients
    run = pipeline(client, lead(scenario="invalid"))
    original = main.providers.notify
    calls = []

    def failed_notify(*args):
        calls.append(args)
        raise IntegrationError("PRIVATE_PROVIDER_ERROR_BODY")

    monkeypatch.setattr(main.providers, "notify", failed_notify)
    client.post("/api/monitor")
    state = client.get("/api/state").json()
    assert state["alerts"] and all(not a["delivered"] for a in state["alerts"])
    assert "PRIVATE_PROVIDER_ERROR_BODY" not in json.dumps(state)
    monkeypatch.setattr(main.providers, "notify", original)
    client.post("/api/monitor")
    assert all(a["delivered"] for a in client.get("/api/state").json()["alerts"])
    assert calls and remote.get("/state").json()["alerts"]
    assert run["status"] == "manual_review"


@pytest.mark.parametrize("token", [None, "", [], "invalid token with spaces"])
def test_malformed_oauth_token_fails_without_calendar_request(monkeypatch, respx_mock, token):
    from app.providers import Providers

    monkeypatch.setenv("INTEGRATION_MODE", "live")
    for name in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"):
        monkeypatch.setenv(name, "synthetic-placeholder")
    route = respx_mock.post("https://oauth2.googleapis.com/token").respond(200, json={"access_token": token})
    adapter = Providers()
    try:
        with pytest.raises(IntegrationError):
            adapter.free(["2099-01-01T12:00:00+00:00"])
        assert route.call_count == 1 and len(respx_mock.calls) == 1
    finally:
        adapter.client.close()


@pytest.mark.parametrize("status,calls", [(400, 1), (401, 1), (429, 3), (503, 3)])
def test_bounded_retry_permanent_and_transient_http(monkeypatch, respx_mock, status, calls):
    from app.providers import Providers, Retryable

    monkeypatch.setenv("RETRY_DELAY_SCALE", "0")
    route = respx_mock.get("https://synthetic.invalid/endpoint").respond(status, json={"error": "PRIVATE"})
    adapter = Providers()
    try:
        if status in {429, 503}:
            with pytest.raises(Retryable):
                adapter.request("GET", "https://synthetic.invalid/endpoint")
        else:
            with pytest.raises(IntegrationError):
                adapter.checked(adapter.request("GET", "https://synthetic.invalid/endpoint"))
        assert route.call_count == calls
    finally:
        adapter.client.close()


def test_type_checker_negative_control(tmp_path):
    import subprocess
    import sys

    invalid = tmp_path / "invalid.py"
    invalid.write_text("def checked() -> str:\n    return 42\n")
    result = subprocess.run([sys.executable, "-m", "mypy", str(invalid)], capture_output=True, text=True)
    assert result.returncode == 1
    assert "return-value" in result.stdout


def test_actual_span_parentage_accounting_metadata_and_all_private_surfaces(clients):
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from app.qualification import PROMPT_HASH, SCHEMA_HASH
    from app.telemetry import provider

    client, _ = clients
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    payload = lead(
        name="PRIVATE_TRACE_NAME",
        message="PRIVATE_TRACE_INQUIRY CRM automation",
        email="trace-private@example.test",
    )
    run = pipeline(client, payload)
    spans = exporter.get_finished_spans()
    qualification = next(s for s in spans if s.name == "qualification")
    attempt = next(s for s in spans if s.name == "llm.attempt")
    root = next(s for s in spans if s.name == "run" and s.context.trace_id == qualification.context.trace_id)
    assert qualification.parent.span_id == root.context.span_id
    assert attempt.parent.span_id == qualification.context.span_id
    accounting = next(s for s in spans if s.name == "llm.accounting")
    assert accounting.attributes["prompt_hash"] == PROMPT_HASH
    assert accounting.attributes["schema_hash"] == SCHEMA_HASH
    assert accounting.attributes["elapsed_ms"] >= 0
    assert accounting.attributes["attempt"] == 1
    assert all(s.end_time >= s.start_time for s in spans)
    serialized = json.dumps([s.to_json() for s in spans])
    for private in (payload["name"], payload["email"], payload["message"], run["booking_token"]):
        assert private not in serialized


@pytest.mark.parametrize("kind", ["contacts", "deals"])
def test_lost_crm_create_ack_recovers_remote_unique_identity(clients, monkeypatch, kind):
    client, remote = clients
    original = main.providers.client.request
    dropped = []

    def lost_ack(method, url, **kwargs):
        response = original(method, url, **kwargs)
        if method == "POST" and str(url).endswith("/objects/" + kind) and not dropped:
            dropped.append(True)
            raise httpx.ReadError("synthetic acknowledgement lost")
        return response

    monkeypatch.setattr(main.providers.client, "request", lost_ack)
    run = pipeline(client)
    assert run["status"] == "offered" and len(dropped) == 1
    state = remote.get("/state").json()
    assert [len(state[k]) for k in ("contacts", "deals", "associations")] == [1, 1, 1]


@pytest.mark.parametrize(
    "name,value",
    [
        ("LLM_MAX_ATTEMPTS", "0"),
        ("LLM_MAX_ATTEMPTS", "13"),
        ("LLM_MAX_INPUT_BYTES", "1"),
        ("LLM_MAX_OUTPUT_TOKENS", "0"),
        ("LLM_MAX_OUTPUT_TOKENS", "4001"),
    ],
)
def test_hard_bounds_fail_before_physical_io(clients, monkeypatch, name, value):
    client, remote = clients
    monkeypatch.setenv(name, value)
    run_id = client.post("/api/intake", json={"lead": lead()}).json()["run_id"]
    result = client.post(f"/api/runs/{run_id}/qualify").json()
    assert result["status"] == "manual_review"
    assert result["llm_usage"]["physical_attempts"] == 0
    assert remote.get("/state").json()["llm"] == []


@pytest.mark.parametrize("retry_after,expected_wait", [("999", 10), ("-5", 0), ("invalid", None)])
def test_retry_after_is_bounded_and_invalid_value_falls_back(
    monkeypatch, respx_mock, retry_after, expected_wait
):
    from app.providers import Providers, Retryable

    waits = []
    monkeypatch.setenv("RETRY_DELAY_SCALE", "1")
    monkeypatch.setattr("app.providers.time.sleep", waits.append)
    respx_mock.get("https://synthetic.invalid/backoff").respond(429, headers={"Retry-After": retry_after})
    adapter = Providers()
    try:
        with pytest.raises(Retryable):
            adapter.request("GET", "https://synthetic.invalid/backoff")
    finally:
        adapter.client.close()
    assert waits == ([1, 2] if expected_wait is None else [expected_wait, expected_wait])


@pytest.mark.parametrize("elapsed_delta,expected", [(-1, 200), (0, 410), (1, 410)])
def test_offer_expiry_exact_boundary_with_timezone_offset(clients, monkeypatch, elapsed_delta, expected):
    from datetime import timezone

    client, remote = clients
    run = pipeline(client)
    fixed = datetime.now(UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz)

    monkeypatch.setattr(main, "datetime", Clock)
    with db.transaction() as session:
        row = db.get_run(session, run["run_id"])
        offered = (fixed - timedelta(days=7, seconds=elapsed_delta)).astimezone(timezone(timedelta(hours=4)))
        db.save(session, row, {**row.data, "offered_at": offered.isoformat()}, "boundary")
    response = client.get(
        "/public/offer", params={"run_id": run["run_id"]}, headers={"X-Booking-Token": run["booking_token"]}
    )
    assert response.status_code == expected
    response = client.post(
        "/api/book", json={"run_id": run["run_id"], "token": run["booking_token"], "slot": run["slots"][0]}
    )
    assert response.status_code == expected
    assert len(remote.get("/state").json()["events"]) == int(expected == 200)


@pytest.mark.parametrize("remaining,expected", [(1, "sending"), (0, "uncertain"), (-1, "uncertain")])
def test_mail_reservation_expiry_exact_boundary_never_resends(clients, monkeypatch, remaining, expected):
    client, remote = clients
    run = pipeline(client)
    fixed = datetime.now(UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz)

    monkeypatch.setattr(main, "datetime", Clock)
    with db.transaction() as session:
        row = db.get_run(session, run["run_id"])
        db.save(
            session,
            row,
            {
                **row.data,
                "offer_mail": "sending",
                "status": "slots_ready",
                "offer_reservation": {
                    "id": "synthetic-owner",
                    "expires_at": (fixed + timedelta(seconds=remaining)).isoformat(),
                },
            },
            "boundary",
        )
    before = len(remote.get("/state").json()["mail"])
    result = main.send_email(run["run_id"], "offer")
    assert result["offer_mail"] == expected
    assert len(remote.get("/state").json()["mail"]) == before
    if remaining > 0:
        assert result["operation_status"] == "in_progress"
    else:
        assert result["status"] == "manual_review"


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://external.invalid/v1/traces",
        "http://identity@localhost/v1/traces",
        "http://:PRIVATE_ENDPOINT_SENTINEL@localhost/v1/traces",
        "http://localhost/v1/traces?token=PRIVATE_ENDPOINT_SENTINEL",
        "http://localhost/v1/traces#token=PRIVATE_ENDPOINT_SENTINEL",
        "file://localhost/v1/traces",
    ],
)
def test_exporter_rejects_remote_userinfo_token_urls_and_non_http_before_io(endpoint):
    import os
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", "import app.telemetry"],
        env={**os.environ, "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": endpoint},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "Telemetry exporter must be a local endpoint" in result.stderr
    assert "PRIVATE_ENDPOINT_SENTINEL" not in result.stdout + result.stderr


def test_transport_namespace_discards_real_http_debug_headers_and_future_children(caplog):
    import io
    from concurrent.futures import ThreadPoolExecutor
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from app.telemetry import logger, operation, suppress_transport_logs

    caplog.set_level(logging.DEBUG)
    old_sink = io.StringIO()
    for namespace in ("httpx", "httpcore"):
        existing = logging.getLogger(namespace + ".preexisting.protocol")
        existing.disabled = False
        existing.handlers = [logging.StreamHandler(old_sink)]
    suppress_transport_logs()
    for namespace in ("httpx", "httpcore"):
        for name in ("preexisting.protocol", "future.protocol"):
            transport = logging.getLogger(namespace + "." + name)
            for severity in (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL):
                transport.log(severity, "PRIVATE_DESCENDANT_SENTINEL")

    class Server(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("X-Private", "PRIVATE_RESPONSE_HEADER_SENTINEL")
            self.send_header("Content-Length", "0")
            self.end_headers()

    with ThreadingHTTPServer(("127.0.0.1", 0), Server) as server:
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(server.serve_forever, 0.05)
            try:
                url = (
                    f"http://synthetic-user:PRIVATE_URL_CREDENTIAL_SENTINEL@127.0.0.1:{server.server_port}"
                    "/private-path/PRIVATE_URL_PATH_SENTINEL"
                )
                with httpx.Client(timeout=2, trust_env=False) as client:
                    assert client.get(url).status_code == 200
            finally:
                server.shutdown()
                running.result(timeout=5)
    safe_sink = io.StringIO()
    safe_handler = logging.StreamHandler(safe_sink)
    logger.addHandler(safe_handler)
    try:
        with operation("privacy.control", stage="verify", inquiry="PRIVATE_INQUIRY_SENTINEL"):
            pass
    finally:
        logger.removeHandler(safe_handler)
    logging.getLogger("application.control").warning("root capture still enabled")
    assert "root capture still enabled" in caplog.text
    assert not any(
        r.name == n or r.name.startswith(n + ".") for r in caplog.records for n in ("httpx", "httpcore")
    )
    assert not old_sink.getvalue()
    assert json.loads(safe_sink.getvalue()) == {"event": "privacy.control", "stage": "verify"}
    for private in (
        "PRIVATE_RESPONSE_HEADER_SENTINEL",
        "PRIVATE_URL_CREDENTIAL_SENTINEL",
        "PRIVATE_URL_PATH_SENTINEL",
        "PRIVATE_DESCENDANT_SENTINEL",
        "PRIVATE_INQUIRY_SENTINEL",
    ):
        assert private not in caplog.text + old_sink.getvalue() + safe_sink.getvalue()


def test_one_attempt_invalid_output_refuses_repair_before_second_physical_request(clients, monkeypatch):
    client, remote = clients
    monkeypatch.setenv("LLM_MAX_ATTEMPTS", "1")
    run_id = client.post("/api/intake", json={"lead": lead(scenario="invalid")}).json()["run_id"]
    result = client.post(f"/api/runs/{run_id}/qualify").json()
    assert result["status"] == "manual_review"
    assert result["manual_reason"] == "llm_budget_exhausted"
    assert result["llm_usage"]["physical_attempts"] == 1
    assert remote.get("/state").json()["llm"][0]["attempts"] == 1
    for step in ("crm", "slots", "offer-email"):
        assert client.post(f"/api/runs/{run_id}/{step}").json()["status"] == "manual_review"
    state = remote.get("/state").json()
    assert all(not state[kind] for kind in ("contacts", "deals", "events", "mail"))
