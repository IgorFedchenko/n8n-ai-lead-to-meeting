"""Real PostgreSQL multi-process admission and SMTP ownership schedules.

Run with TEST_POSTGRES_URL; CI provides a dedicated ephemeral database. Each test uses a
private schema. External LLM transport is intercepted; SMTP uses a loopback wire server.
"""

import multiprocessing
import os
import smtplib
import socketserver
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from threading import Event

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app import db, llm_budget, main

pytestmark = pytest.mark.postgres
CTX = multiprocessing.get_context("spawn")


def connect(url, schema):
    return create_engine(url, connect_args={"options": "-csearch_path=" + schema}, pool_pre_ping=True)


@pytest.fixture
def postgres(monkeypatch):
    url = os.getenv("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_URL required; dedicated CI PostgreSQL job runs this group")
    schema = "contract_" + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text("CREATE SCHEMA " + schema))
    engine = connect(url, schema)
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    yield url, schema
    engine.dispose()
    with admin.begin() as connection:
        connection.execute(text("DROP SCHEMA " + schema + " CASCADE"))
    admin.dispose()


def seeded(identity="a" * 64, owner="qualification-owner"):
    with db.transaction() as session:
        session.add(
            db.Run(
                id=identity,
                fingerprint="synthetic",
                data={
                    "lead": {"email": "synthetic@example.test"},
                    "status": "slots_ready",
                    "done": ["slots"],
                    "slots": ["2099-01-01T12:00:00+00:00"],
                    "booking_token": "synthetic-private-token",
                    "qualification_operation": {
                        "id": owner,
                        "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                    },
                },
            )
        )
    return identity


def budget_environment():
    return dict(
        LLM_SPEND_APPROVED="true",
        LLM_RATE_SOURCE="synthetic-test-rates",
        LLM_MODEL="synthetic-model",
        LLM_API_KEY="synthetic",
        LLM_MAX_INPUT_BYTES="10000",
        LLM_MAX_OUTPUT_TOKENS="1000",
        LLM_INPUT_USD_PER_MILLION="2",
        LLM_OUTPUT_USD_PER_MILLION="20",
        LLM_RUN_BUDGET_USD="0.10",
        LLM_MAX_ATTEMPTS="2",
    )


def admission_worker(url, schema, run_id, ready, start, results):
    db.engine = connect(url, schema)
    os.environ.update(budget_environment())
    ready.put(True)
    if not start.wait(10):
        results.put("barrier-timeout")
        return
    try:
        identity = llm_budget.admit(run_id, "live", "qualification-owner")
        # This observer connection models the physical outbound boundary, after committed admission.
        with Session(connect(url, schema)) as observer:
            assert observer.get(db.LLMAttempt, identity) is not None
        results.put(identity)
    except llm_budget.BudgetDenied:
        results.put("denied")
    finally:
        db.engine.dispose()


def race_admissions(postgres, run_ids):
    ready, results, start = CTX.Queue(), CTX.Queue(), CTX.Event()
    workers = [
        CTX.Process(target=admission_worker, args=(*postgres, run_id, ready, start, results))
        for run_id in run_ids
    ]
    for worker in workers:
        worker.start()
    for _ in workers:
        assert ready.get(timeout=15)
    start.set()
    outcomes = [results.get(timeout=15) for _ in workers]
    for worker in workers:
        worker.join(15)
        assert worker.exitcode == 0
    return outcomes


def test_committed_admission_concurrent_last_slot_restart_and_cross_run(postgres, monkeypatch):
    for name, value in budget_environment().items():
        monkeypatch.setenv(name, value)
    run_id = seeded()
    first = llm_budget.admit(run_id, "live", "qualification-owner")
    llm_budget.record(first, "ambiguous", None, 1)
    outcomes = race_admissions(postgres, [run_id, run_id])
    assert outcomes.count("denied") == 1
    # A fresh process has no fresh allowance, even after both previous processes have exited.
    assert race_admissions(postgres, [run_id]) == ["denied"]
    with db.transaction() as session:
        usage = llm_budget.summary(session, run_id)
        assert usage["physical_attempts"] == 2
        assert float(usage["reserved_usd"]) == 0.08
        assert usage["input_tokens"] is None and usage["estimated_usage_usd"] is None
    other, third = seeded("b" * 64), seeded("c" * 64)
    assert all(item != "denied" for item in race_admissions(postgres, [other, third]))


def test_cost_denial_before_first_io_and_exact_unknown_usage(postgres, monkeypatch):
    for name, value in budget_environment().items():
        monkeypatch.setenv(name, value)
    run_id = seeded()
    monkeypatch.setenv("LLM_RUN_BUDGET_USD", "0.03")
    with pytest.raises(llm_budget.BudgetDenied):
        llm_budget.admit(run_id, "live", "qualification-owner")
    with db.transaction() as session:
        assert llm_budget.summary(session, run_id)["physical_attempts"] == 0
    monkeypatch.setenv("LLM_RUN_BUDGET_USD", "0.10")
    first = llm_budget.admit(run_id, "live", "qualification-owner")
    llm_budget.record(first, "response", {"input_tokens": 100, "output_tokens": 20}, 5)
    with db.transaction() as session:
        usage = llm_budget.summary(session, run_id)
        assert (usage["input_tokens"], usage["output_tokens"]) == (100, 20)
        assert float(usage["estimated_usage_usd"]) == 0.0006
    second = llm_budget.admit(run_id, "live", "qualification-owner")
    llm_budget.record(second, "response", {"input_tokens": True, "output_tokens": -1}, 1)
    with db.transaction() as session:
        usage = llm_budget.summary(session, run_id)
        assert usage["input_tokens"] is None and usage["output_tokens"] is None
        assert usage["estimated_usage_usd"] is None


def smtp_worker(url, schema, run_id, port, results):
    db.engine = connect(url, schema)

    def wire_send(key, recipient, subject, body):
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = "sender@example.test", recipient, subject
        message.set_content(body)
        with smtplib.SMTP("127.0.0.1", port, timeout=15) as smtp:
            smtp.send_message(message)

    main.providers.email = wire_send
    try:
        results.put(main.send_email(run_id, "offer"))
    except Exception as error:
        results.put({"failure": type(error).__name__})
    finally:
        db.engine.dispose()


@pytest.mark.parametrize("late_failure", [False, True])
@pytest.mark.parametrize("resolution", ["active", "expired", "delivered", "not_delivered"])
def test_smtp_physical_send_owner_cas_across_processes(postgres, monkeypatch, late_failure, resolution):
    run_id = seeded()
    entered, release = Event(), Event()
    physical_messages = []

    class SMTP(socketserver.StreamRequestHandler):
        def handle(self):
            self.wfile.write(b"220 synthetic SMTP\r\n")
            while line := self.rfile.readline():
                command = line.split()[0].upper()
                if command == b"DATA":
                    self.wfile.write(b"354 send data\r\n")
                    data = []
                    while (line := self.rfile.readline()) != b".\r\n":
                        if not line:
                            return
                        data.append(line)
                    physical_messages.append(len(b"".join(data)))
                    entered.set()
                    assert release.wait(15)
                    if late_failure:
                        return
                    self.wfile.write(b"250 accepted\r\n")
                elif command == b"QUIT":
                    self.wfile.write(b"221 bye\r\n")
                    return
                else:
                    self.wfile.write(b"250 ok\r\n")

    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), SMTP) as server:
        with ThreadPoolExecutor(max_workers=1) as pool:
            serving = pool.submit(server.serve_forever, 0.05)
            results = CTX.Queue()
            sender = CTX.Process(
                target=smtp_worker, args=(*postgres, run_id, server.server_address[1], results)
            )
            sender.start()
            try:
                assert entered.wait(15)
                with Session(connect(*postgres)) as observer:
                    before = observer.get(db.Run, run_id).data
                    assert before["offer_mail"] == "sending"
                    assert before["offer_reservation"]["id"]
                duplicate = main.send_email(run_id, "offer")
                assert duplicate["operation_status"] == "in_progress"
                assert duplicate["status"] == "slots_ready"
                if resolution != "active":
                    with db.transaction() as session:
                        run = db.get_run(session, run_id)
                        data = {
                            **run.data,
                            "offer_reservation": {
                                **run.data["offer_reservation"],
                                "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                            },
                        }
                        db.save(session, run, data, "test_expiry")
                    with db.transaction() as session:
                        run = db.get_run(session, run_id)
                        main.expire_mail(session, run, dict(run.data), "offer")
                        assert run.data["offer_mail"] == "uncertain"
                    if resolution in {"delivered", "not_delivered"}:
                        with db.transaction() as session:
                            run = db.get_run(session, run_id)
                            db.save(
                                session,
                                run,
                                {
                                    **run.data,
                                    "offer_mail": "sent" if resolution == "delivered" else "uncertain",
                                    "status": "booked",
                                    "booked_slot": "preserve-later-booking",
                                    "offer_reservation": {"id": "operator-reconciled"},
                                },
                                "reconciled",
                            )
            finally:
                release.set()
            outcome = results.get(timeout=15)
            sender.join(15)
            server.shutdown()
            serving.result(timeout=5)
    assert sender.exitcode == 0
    assert len(physical_messages) == 1
    with db.transaction() as session:
        run = db.get_run(session, run_id)
        if resolution == "active":
            assert run.data["offer_mail"] == ("uncertain" if late_failure else "sent")
        else:
            assert run.data["offer_mail"] == ("sent" if resolution == "delivered" else "uncertain")
            events = list(session.scalars(select(db.Journal.event).where(db.Journal.run_id == run_id)))
            assert events[-1] == "offer_mail_late_" + ("failure" if late_failure else "success")
            if resolution != "expired":
                assert run.data["status"] == "booked"
                assert run.data["booked_slot"] == "preserve-later-booking"
    if resolution != "active" or not late_failure:
        assert "failure" not in outcome
    # A new invocation after the sender exited cannot resend retained sent/ambiguous delivery.
    monkeypatch.setattr(main.providers, "email", lambda *args: pytest.fail("unexpected second physical send"))
    main.send_email(run_id, "offer")
