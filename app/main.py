import hmac
import os
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError
from sqlalchemy import delete, select

from app import llm_budget
from app.db import Alert, Journal, LLMAttempt, Run, alert, get_run, init_db, save, snapshot, transaction
from app.models import Booking, Intake
from app.providers import IntegrationError, Providers, Retryable, candidate_slots, parse_time
from app.qualification import attempt_hooks, usage_hook, validate_response
from app.telemetry import operation

providers = Providers()


@asynccontextmanager
async def lifespan(app):
    if providers.mode == "live":
        from app.config import LiveConfiguration

        LiveConfiguration().validate()
    init_db()
    yield
    providers.client.close()


app = FastAPI(title="Lead to Meeting — reference implementation", lifespan=lifespan)


@app.middleware("http")
async def trace_run(request: Request, call_next):
    # Never record paths, query strings, headers or bodies (booking URLs carry secrets).
    with operation("run", stage="http") as span:
        response = await call_next(request)
        span.set_attribute("status", str(response.status_code))
        return response


def authorize(x_service_key: str = Header(default="")):
    expected = os.getenv("SERVICE_KEY", "")
    if not expected or not hmac.compare_digest(expected, x_service_key):
        raise HTTPException(401, "Invalid service key")


def result(run):
    return {"run_id": run.id, **run.data}


@app.get("/health")
def health():
    with transaction() as session:
        session.execute(select(1))
    return {"status": "ok", "mode": providers.mode}


@app.post("/api/intake", dependencies=[Depends(authorize)])
def intake(body: Intake):
    if providers.mode == "live" and body.lead.scenario != "normal":
        raise HTTPException(422, "Fault injection is disabled in live mode")
    with transaction() as session:
        run = session.get(Run, body.lead.key)
        if run:
            if run.fingerprint != body.lead.fingerprint:
                raise HTTPException(409, "Submission ID was already used for different content")
            data = dict(run.data)
            executions = data.get("executions", [])
            if body.execution_id and body.execution_id not in executions:
                data["executions"] = (executions + [body.execution_id])[-100:]
            save(session, run, data, "duplicate_delivery")
            return result(run)
        data = {
            "lead": body.lead.model_dump(),
            "status": "received",
            "done": [],
            "writes": 0,
            "executions": [body.execution_id],
            "booking_token": secrets.token_urlsafe(32),
        }
        run = Run(id=body.lead.key, fingerprint=body.lead.fingerprint, data=data)
        session.add(run)
        session.add(Journal(run_id=run.id, event="received"))
        return result(run)


def stage(run_id, name, requires, action):
    failure = None
    with transaction() as session:
        run = get_run(session, run_id)
        data = dict(run.data)
        if name in data["done"] or data["status"] == "manual_review":
            return result(run)
        if requires and requires not in data["done"]:
            raise HTTPException(409, f"Complete {requires} first")
        try:
            with operation("stage", run_id=run_id, stage=name):
                action(data)
            data["done"] = data["done"] + [name]
            data.pop("failed_step", None)
            data.pop("error", None)
            save(session, run, data, name)
        except (Retryable, IntegrationError) as error:
            data.update(status="failed", failed_step=name, error=type(error).__name__)
            save(session, run, data, f"{name}_failed")
            alert(session, run.id, f"{name}_failed")
            failure = HTTPException(503 if isinstance(error, Retryable) else 502, "Integration step failed")
        response = result(run)
    if failure:
        raise failure
    return response


@app.post("/api/runs/{run_id}/qualify", dependencies=[Depends(authorize)])
def qualify(run_id: str):
    owner = secrets.token_hex(16)
    with transaction() as session:
        run = get_run(session, run_id)
        data = dict(run.data)
        if "qualify" in data["done"] or data["status"] == "manual_review":
            return result(run)
        active = data.get("qualification_operation")
        if active:
            if parse_time(active["expires_at"]) > datetime.now(UTC):
                return {**result(run), "operation_status": "in_progress"}
            data.update(status="manual_review", manual_reason="qualification_interrupted")
            save(session, run, data, "qualification_interrupted")
            alert(session, run_id, "qualification_interrupted")
            return result(run)
        data["qualification_operation"] = {
            "id": owner,
            "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        }
        save(session, run, data, "qualification_reserved")
        lead = data["lead"]
    # No transaction/identity lock spans LLM I/O: each HTTP admission must commit independently.
    admission = attempt_hooks.set(lambda: llm_budget.admit(run_id, providers.mode, owner))
    recording = usage_hook.set(llm_budget.record)
    outcome: dict = {}
    try:
        with operation("qualification", run_id=run_id, stage="qualify"):
            for _attempt in range(2):
                try:
                    raw = providers.qualify(lead)
                    outcome = {"qualification": validate_response(raw).model_dump(), "status": "qualified"}
                    break
                except (ValidationError, ValueError, KeyError, IndexError) as error:
                    if isinstance(error, llm_budget.BudgetDenied):
                        outcome = {"status": "manual_review", "manual_reason": "llm_budget_exhausted"}
                        break
                    outcome = {"status": "manual_review", "manual_reason": "invalid_llm_output"}
                except (Retryable, IntegrationError):
                    outcome = {"status": "failed", "failed_step": "qualify", "error": "llm_unavailable"}
                    break
    finally:
        attempt_hooks.reset(admission)
        usage_hook.reset(recording)
    with transaction() as session:
        run = get_run(session, run_id)
        data = dict(run.data)
        active = data.get("qualification_operation", {})
        if (
            active.get("id") != owner
            or data["status"] == "manual_review"
            or parse_time(active["expires_at"]) <= datetime.now(UTC)
        ):
            save(session, run, data, "qualification_late_result")
            return result(run)
        data.pop("qualification_operation", None)
        data.update(outcome, llm_attempts=_attempt + 1, llm_usage=llm_budget.summary(session, run_id))
        if outcome["status"] in {"qualified", "manual_review"}:
            data["done"] = data["done"] + ["qualify"]
        if outcome["status"] != "qualified":
            alert(session, run_id, outcome.get("manual_reason", "qualify_failed"))
        save(session, run, data, "qualification_finished")
        response = result(run)
    if outcome["status"] == "failed":
        raise HTTPException(503, "Qualification unavailable; committed attempt budget retained")
    return response


@app.post("/api/runs/{run_id}/crm", dependencies=[Depends(authorize)])
def crm(run_id: str):
    def action(data):
        contact_id = providers.contact(data["lead"])
        deal_id = providers.deal(run_id, data["lead"], data["qualification"], contact_id)
        data.update(contact_id=contact_id, deal_id=deal_id, writes=2, status="crm_written")

    return stage(run_id, "crm", "qualify", action)


@app.post("/api/runs/{run_id}/slots", dependencies=[Depends(authorize)])
def slots(run_id: str):
    def action(data):
        available = providers.free(candidate_slots())[:3]
        if not available:
            raise IntegrationError("No free slots; operator review required")
        data.update(slots=available, offered_at=datetime.now(UTC).isoformat(), status="slots_ready")

    return stage(run_id, "slots", "crm", action)


def expire_mail(session, run, data, kind):
    """Called under the transaction lock; expiry permanently removes sender ownership."""
    reservation = data.get(kind + "_reservation", {})
    if data.get(kind + "_mail") == "sending" and (
        not reservation or parse_time(reservation["expires_at"]) <= datetime.now(UTC)
    ):
        data[kind + "_mail"] = "uncertain"
        data.update(status="manual_review", manual_reason="mail_delivery_uncertain")
        save(session, run, data, kind + "_mail_expired")
        alert(session, run.id, "mail_delivery_uncertain")


def send_email(run_id, kind):
    key = f"{kind}:{run_id}"
    owner = secrets.token_hex(16)
    with transaction() as session:
        run = get_run(session, run_id)
        data = dict(run.data)
        expire_mail(session, run, data, kind)
        if data["status"] == "manual_review" or data.get(kind + "_mail") == "sent":
            return result(run)
        required = "slots" if kind == "offer" else "book"
        if required not in data["done"]:
            raise HTTPException(409, f"Complete {required} first")
        if data.get(kind + "_mail") == "sending":
            return {**result(run), "operation_status": "in_progress"}
        if data.get(kind + "_mail") == "uncertain":
            return result(run)
        data[kind + "_mail"] = "sending"
        data[kind + "_reservation"] = {
            "id": owner,
            "expires_at": (
                datetime.now(UTC) + timedelta(seconds=int(os.getenv("SMTP_RESERVATION_SECONDS", "120")))
            ).isoformat(),
        }
        save(session, run, data, kind + "_mail_reserved")
        recipient = data["lead"]["email"]
        if kind == "offer":
            base = os.getenv("PUBLIC_DEMO_URL", "http://localhost:8088")
            body = "Choose a 30-minute discovery slot (UTC), valid for 7 days:\n" + "\n".join(data["slots"])
            body += f"\nBook: {base}/booking?run={run_id}#token={data['booking_token']}"
            subject = "Choose your discovery meeting"
        else:
            body = f"Your 30-minute discovery meeting is confirmed for {data['booked_slot']} (UTC)."
            subject = "Your discovery meeting is confirmed"
    succeeded = False
    try:
        with operation("mail", run_id=run_id, stage=kind):
            providers.email(key, recipient, subject, body)
        succeeded = True
    except Exception:
        succeeded = False  # Any SMTP failure is ambiguous, including acknowledgement loss.
    with transaction() as session:
        run = get_run(session, run_id)
        data = dict(run.data)
        expire_mail(session, run, data, kind)
        # Persisted ownership CAS, serialized by the transaction's advisory/SQLite write lock.
        owned = data.get(kind + "_mail") == "sending" and (
            data.get(kind + "_reservation", {}).get("id") == owner
        )
        if not owned:
            save(session, run, data, kind + ("_mail_late_success" if succeeded else "_mail_late_failure"))
            return result(run)
        data[kind + "_mail"] = "sent" if succeeded else "uncertain"
        if succeeded:
            if kind == "confirmation":
                data["status"] = "completed"
            elif data["status"] == "slots_ready":
                data["status"] = "offered"
        else:
            data.update(status="manual_review", manual_reason="mail_delivery_uncertain")
            alert(session, run_id, "mail_delivery_uncertain")
        save(session, run, data, kind + ("_mail_sent" if succeeded else "_mail_uncertain"))
        response = result(run)
    if not succeeded:
        raise HTTPException(502, "Email delivery requires reconciliation")
    return response


@app.post("/api/runs/{run_id}/offer-email", dependencies=[Depends(authorize)])
def offer_email(run_id: str):
    return send_email(run_id, "offer")


@app.post("/api/book", dependencies=[Depends(authorize)])
def book(body: Booking):
    failure = None
    with transaction() as session:
        run = get_run(session, body.run_id)
        data = dict(run.data)
        if not hmac.compare_digest(data["booking_token"], body.token):
            raise HTTPException(403, "Invalid booking token")
        if "book" in data["done"]:
            if body.slot != data["booked_slot"]:
                raise HTTPException(409, "A meeting is already booked")
            return result(run)
        retrying_booking = data["status"] == "failed" and data.get("failed_step") == "book"
        if (data["status"] != "offered" and not retrying_booking) or body.slot not in data.get("slots", []):
            raise HTTPException(409, "Slot was not offered")
        if parse_time(body.slot) <= datetime.now(UTC) or (
            datetime.now(UTC) - parse_time(data["offered_at"]) > timedelta(days=7)
        ):
            raise HTTPException(410, "Offer expired")
        # A deterministic remote ID and the transaction lock protect concurrent/redelivered bookings.
        try:
            event_id = providers.book(body.run_id, body.slot)
            data.update(
                booked_slot=body.slot, event_id=event_id, status="booked", done=data["done"] + ["book"]
            )
            data.pop("failed_step", None)
            data.pop("error", None)
            save(session, run, data, "calendar_booked")
        except (IntegrationError, Retryable) as error:
            data.update(status="failed", failed_step="book", error=type(error).__name__)
            save(session, run, data, "book_failed")
            alert(session, run.id, "book_failed")
            failure = HTTPException(
                503 if isinstance(error, Retryable) else 502, "Booking integration failed"
            )
        response = result(run)
    if failure:
        raise failure
    return response


@app.post("/api/runs/{run_id}/confirmation-email", dependencies=[Depends(authorize)])
def confirmation_email(run_id: str):
    return send_email(run_id, "confirmation")


@app.post("/api/runs/{run_id}/finish", dependencies=[Depends(authorize)])
def finish(run_id: str):
    with transaction() as session:
        run = get_run(session, run_id)
        if not run.data["writes"]:
            alert(session, run_id, "zero_crm_writes")
        save(session, run, run.data, "execution_finished")
        return result(run)


@app.post("/api/errors", dependencies=[Depends(authorize)])
def errors(payload: dict):
    execution_id = str(payload.get("execution", {}).get("id", "unknown"))[:100]
    with transaction() as session:
        matched = False
        for run in session.scalars(select(Run)):
            if execution_id in run.data["executions"]:
                data = dict(run.data)
                outcomes = dict(data.get("execution_outcomes", {}))
                outcomes[execution_id] = "failed"
                data["execution_outcomes"] = outcomes
                # An execution notification is not authoritative about later business progress.
                if data["status"] == "received" and execution_id == data["executions"][-1]:
                    data.update(status="failed", error="n8n_execution_failed")
                save(session, run, data, "n8n_error_workflow")
                alert(session, run.id, "n8n_execution_failed")
                matched = True
        if not matched:
            alert(session, "execution-" + execution_id, "n8n_execution_failed")
    return monitor()


@app.post("/api/monitor", dependencies=[Depends(authorize)])
def monitor():
    delivered = 0
    with transaction() as session:
        for run in session.scalars(select(Run)):
            data = dict(run.data)
            for kind in ("offer", "confirmation"):
                expire_mail(session, run, data, kind)
            updated = run.updated_at.replace(tzinfo=UTC) if run.updated_at.tzinfo is None else run.updated_at
            if run.data["status"] in {"received", "qualified", "crm_written", "slots_ready", "booked"} and (
                datetime.now(UTC) - updated > timedelta(minutes=10)
            ):
                alert(session, run.id, "stalled_execution")
        session.flush()
        for item in session.scalars(select(Alert).where(Alert.delivered.is_(False))):
            try:
                providers.notify(item.key, item.run_id, item.reason)
                item.delivered = True
                delivered += 1
            except Exception:  # nosec B112
                # Keep failed deliveries in the durable outbox for the next monitor pass.
                # Durable alert outbox is retried by the independent monitor workflow.
                continue
    return {"delivered": delivered}


@app.post("/api/maintenance", dependencies=[Depends(authorize)])
def maintenance():
    cutoff = datetime.now(UTC) - timedelta(days=int(os.getenv("RETENTION_DAYS", "7")))
    with transaction() as session:
        pending = set(session.scalars(select(Alert.run_id).where(Alert.delivered.is_(False))))
        expired = [
            r.id
            for r in session.scalars(select(Run).where(Run.updated_at < cutoff))
            if r.data["status"] in {"offered", "completed"}
            and r.id not in pending
            and not r.data.get("qualification_operation")
            and not any(
                r.data.get(k + "_mail") in {"sending", "uncertain"} for k in ("offer", "confirmation")
            )
        ]
        if expired:
            session.execute(delete(LLMAttempt).where(LLMAttempt.run_id.in_(expired)))
            session.execute(delete(Journal).where(Journal.run_id.in_(expired)))
            session.execute(delete(Alert).where(Alert.run_id.in_(expired)))
            session.execute(delete(Run).where(Run.id.in_(expired)))
    return {"purged": len(expired)}


@app.get("/api/state", dependencies=[Depends(authorize)])
def state():
    with transaction() as session:
        return snapshot(session)


@app.get("/", response_class=HTMLResponse)
def demo():
    if providers.mode != "mock":
        raise HTTPException(404)
    return Path(__file__).with_name("demo.html").read_text()


@app.get("/booking", response_class=HTMLResponse)
def booking_page():
    return HTMLResponse(
        Path(__file__).with_name("booking.html").read_text(),
        headers={
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/public/offer")
def public_offer(run_id: str, x_booking_token: str = Header(default="")):
    with transaction() as session:
        run = get_run(session, run_id)
        data = run.data
        if not hmac.compare_digest(data["booking_token"], x_booking_token):
            raise HTTPException(403, "Invalid booking token")
        if "offered_at" not in data or datetime.now(UTC) - parse_time(data["offered_at"]) > timedelta(days=7):
            raise HTTPException(410, "Offer unavailable or expired")
        return {
            "status": data["status"],
            "slots": data.get("slots", []),
            "booked_slot": data.get("booked_slot"),
        }


@app.post("/public/book")
async def public_book(body: Booking):
    # Validate before forwarding; this public boundary never returns PII or internal credentials.
    public_offer(body.run_id, body.token)
    url = os.getenv("N8N_INTERNAL_URL", "http://n8n:5678") + "/webhook/book"
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            url, headers={"X-Webhook-Token": os.getenv("WEBHOOK_TOKEN", "")}, json=body.model_dump()
        )
    if response.is_error:
        raise HTTPException(
            409, "Booking could not be confirmed; check availability or contact the organizer"
        )
    outcome = response.json()
    return {"status": outcome["status"], "booked_slot": outcome.get("booked_slot")}


@app.api_route("/demo/{path:path}", methods=["GET", "POST"])
async def proxy(path: str, request: Request):
    # Deliberately local, synthetic-only demo. Never expose it in live mode.
    if providers.mode != "mock":
        raise HTTPException(404)
    routes = {
        ("GET", "state"): ("http://127.0.0.1:8000/api/state", "service"),
        ("GET", "mock"): (providers.mock_url + "/state", "service"),
        ("POST", "lead"): (os.getenv("N8N_INTERNAL_URL", "http://n8n:5678") + "/webhook/lead", "webhook"),
        ("POST", "book"): (os.getenv("N8N_INTERNAL_URL", "http://n8n:5678") + "/webhook/book", "webhook"),
    }
    if (request.method, path) not in routes:
        raise HTTPException(404)
    url, kind = routes[(request.method, path)]
    headers = (
        {"X-Service-Key": os.getenv("SERVICE_KEY", "")}
        if kind == "service"
        else {
            "X-Webhook-Token": os.getenv("WEBHOOK_TOKEN", ""),
        }
    )
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.request(
            request.method,
            url,
            headers=headers,
            json=await request.json() if request.method == "POST" else None,
        )
    if response.is_error:
        raise HTTPException(response.status_code, "Workflow request failed")
    return response.json()
