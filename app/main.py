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

from app.db import Alert, Journal, Run, alert, get_run, init_db, save, snapshot, transaction
from app.models import Booking, Intake, Qualification
from app.providers import IntegrationError, Providers, Retryable, candidate_slots, parse_time

providers = Providers()


@asynccontextmanager
async def lifespan(app):
    init_db()
    yield
    providers.client.close()


app = FastAPI(title="Lead to Meeting — reference implementation", lifespan=lifespan)


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
    def action(data):
        for attempt in range(2):
            try:
                raw = providers.qualify(data["lead"])
                data["qualification"] = Qualification.model_validate_json(raw).model_dump()
                data.update(status="qualified", llm_attempts=attempt + 1)
                return
            except (ValidationError, ValueError, KeyError, IndexError):
                if attempt == 1:
                    data.update(status="manual_review", llm_attempts=2, manual_reason="invalid_llm_output")

    response = stage(run_id, "qualify", None, action)
    if response["status"] == "manual_review":
        with transaction() as session:
            alert(session, run_id, "invalid_llm_output")
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


def send_email(run_id, kind):
    key = f"{kind}:{run_id}"
    with transaction() as session:
        run = get_run(session, run_id)
        data = dict(run.data)
        if data["status"] == "manual_review" or data.get(kind + "_mail") == "sent":
            return result(run)
        required = "slots" if kind == "offer" else "book"
        if required not in data["done"]:
            raise HTTPException(409, f"Complete {required} first")
        if data.get(kind + "_mail") in {"sending", "uncertain"}:
            data.update(status="manual_review", manual_reason="mail_delivery_uncertain")
            alert(session, run_id, "mail_delivery_uncertain")
            save(session, run, data, "mail_reconciliation_required")
            return result(run)
        data[kind + "_mail"] = "sending"
        save(session, run, data, kind + "_mail_reserved")
        recipient = data["lead"]["email"]
        if kind == "offer":
            base = os.getenv("PUBLIC_DEMO_URL", "http://localhost:8088")
            body = "Choose a 30-minute discovery slot (UTC), valid for 7 days:\n" + "\n".join(data["slots"])
            body += f"\nBook: {base}/?run={run_id}#token={data['booking_token']}"
            subject = "Choose your discovery meeting"
        else:
            body = f"Your 30-minute discovery meeting is confirmed for {data['booked_slot']} (UTC)."
            subject = "Your discovery meeting is confirmed"
    try:
        providers.email(key, recipient, subject, body)
    except Exception:
        with transaction() as session:
            run = get_run(session, run_id)
            data = dict(run.data)
            data.update(
                {
                    kind + "_mail": "uncertain",
                    "status": "manual_review",
                    "manual_reason": "mail_delivery_uncertain",
                }
            )
            save(session, run, data, kind + "_mail_uncertain")
            alert(session, run_id, "mail_delivery_uncertain")
        raise HTTPException(502, "Email delivery requires reconciliation") from None
    with transaction() as session:
        run = get_run(session, run_id)
        data = dict(run.data)
        data[kind + "_mail"] = "sent"
        data["status"] = "offered" if kind == "offer" else "completed"
        save(session, run, data, kind + "_mail_sent")
        return result(run)


@app.post("/api/runs/{run_id}/offer-email", dependencies=[Depends(authorize)])
def offer_email(run_id: str):
    return send_email(run_id, "offer")


@app.post("/api/book", dependencies=[Depends(authorize)])
def book(body: Booking):
    with transaction() as session:
        run = get_run(session, body.run_id)
        data = dict(run.data)
        if not hmac.compare_digest(data["booking_token"], body.token):
            raise HTTPException(403, "Invalid booking token")
        if "book" in data["done"]:
            if body.slot != data["booked_slot"]:
                raise HTTPException(409, "A meeting is already booked")
            return result(run)
        if data["status"] != "offered" or body.slot not in data.get("slots", []):
            raise HTTPException(409, "Slot was not offered")
        if parse_time(body.slot) <= datetime.now(UTC) or (
            datetime.now(UTC) - parse_time(data["offered_at"]) > timedelta(days=7)
        ):
            raise HTTPException(410, "Offer expired")
        # A deterministic remote ID and the transaction lock protect concurrent/redelivered bookings.
        event_id = providers.book(body.run_id, body.slot)
        data.update(booked_slot=body.slot, event_id=event_id, status="booked", done=data["done"] + ["book"])
        save(session, run, data, "calendar_booked")
        return result(run)


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
            except Exception:
                # Durable alert outbox is retried by the independent monitor workflow.
                continue
    return {"delivered": delivered}


@app.post("/api/maintenance", dependencies=[Depends(authorize)])
def maintenance():
    cutoff = datetime.now(UTC) - timedelta(days=int(os.getenv("RETENTION_DAYS", "7")))
    with transaction() as session:
        expired = list(session.scalars(select(Run.id).where(Run.updated_at < cutoff)))
        if expired:
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
