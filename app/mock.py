"""Synthetic, local HTTP doubles matching the APIs used by the real adapters."""

import json
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import JSON, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

engine = create_engine(os.getenv("DATABASE_URL", "sqlite:///./mock.db"), pool_pre_ping=True)


class Base(DeclarativeBase):
    pass


class Object(Base):
    __tablename__ = "mock_objects"
    key: Mapped[str] = mapped_column(String(300), primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    data: Mapped[dict] = mapped_column(JSON)


@asynccontextmanager
async def lifespan(app):
    Base.metadata.create_all(engine)
    yield


app = FastAPI(title="Synthetic provider doubles", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


def objects(session, kind):
    return list(session.scalars(select(Object).where(Object.kind == kind)))


def put(session, kind, key, data):
    obj = session.get(Object, key)
    if obj:
        obj.data = dict(data)
    else:
        session.add(Object(key=key, kind=kind, data=data))


@app.post("/v1/messages")
def llm(body: dict):
    lead = json.loads(body["messages"][0]["content"])["untrusted_inquiry"]
    scenario = lead.get("scenario", "normal")
    with Session(engine) as session, session.begin():
        key = "llm:" + lead["request_id"]
        obj = session.get(Object, key)
        count = obj.data["attempts"] + 1 if obj else 1
        put(session, "llm", key, {"attempts": count})
    if scenario == "transient" and count == 1:
        from fastapi.responses import JSONResponse

        return JSONResponse({"error": "temporary"}, status_code=429, headers={"Retry-After": "0"})
    if scenario == "invalid" or scenario == "invalid_once" and count == 1:
        output = '{"score": "not-a-number"}'
    else:
        output = json.dumps(
            {
                "budget": 5000.0,
                "currency": "USD",
                "timeline": "Within 4 weeks",
                "request_type": "automation",
                "score": 88,
                "reason": "Recorded synthetic response: clear automation requirements.",
            }
        )
    return {"content": [{"type": "text", "text": output}]}


@app.post("/crm/v3/objects/contacts/search")
def search(body: dict):
    filt = body["filterGroups"][0]["filters"][0]
    with Session(engine) as session:
        matches = [
            o.data
            for o in objects(session, "contacts")
            if o.data["properties"].get(filt["propertyName"]) == filt["value"]
        ]
    return {"results": matches, "total": len(matches)}


@app.get("/crm/v3/objects/{kind}/{identifier}")
def read(kind: str, identifier: str, idProperty: str = ""):
    with Session(engine) as session:
        matches = [
            o.data
            for o in objects(session, kind)
            if (o.data["properties"].get(idProperty) if idProperty else o.data["id"]) == identifier
        ]
    if not matches:
        raise HTTPException(404)
    return matches[0]


@app.post("/crm/v3/objects/{kind}")
def create(kind: str, body: dict):
    properties = body["properties"]
    if kind == "deals" and "crm-fail" in properties["dealname"]:
        raise HTTPException(503, "Injected CRM failure")
    field = "email" if kind == "contacts" else "portfolio_request_id"
    with Session(engine) as session, session.begin():
        if any(o.data["properties"][field] == properties[field] for o in objects(session, kind)):
            raise HTTPException(409)
        identifier = uuid.uuid4().hex
        data = {"id": identifier, "properties": properties}
        put(session, kind, kind + ":" + identifier, data)
        return data


@app.patch("/crm/v3/objects/{kind}/{identifier}")
def patch(kind: str, identifier: str, body: dict):
    with Session(engine) as session, session.begin():
        obj = session.get(Object, kind + ":" + identifier)
        if not obj:
            raise HTTPException(404)
        data = dict(obj.data)
        data["properties"] = {**data["properties"], **body["properties"]}
        obj.data = data
        return data


@app.put("/crm/v3/objects/deals/{deal}/associations/contacts/{contact}/deal_to_contact")
def associate(deal: str, contact: str):
    with Session(engine) as session, session.begin():
        put(session, "associations", f"association:{deal}:{contact}", {"deal": deal, "contact": contact})
    return {"status": "COMPLETE"}


@app.post("/calendar/v3/freeBusy")
def busy(body: dict):
    with Session(engine) as session:
        values = [
            {"start": o.data["start"]["dateTime"], "end": o.data["end"]["dateTime"]}
            for o in objects(session, "events")
        ]
    return {"calendars": {body["items"][0]["id"]: {"busy": values}}}


@app.get("/calendar/v3/calendars/{calendar}/events/{identifier}")
def event(calendar: str, identifier: str):
    with Session(engine) as session:
        obj = session.get(Object, "event:" + identifier)
        if not obj:
            raise HTTPException(404)
        return obj.data


@app.post("/calendar/v3/calendars/{calendar}/events")
def insert_event(calendar: str, body: dict):
    with Session(engine) as session, session.begin():
        if session.get(Object, "event:" + body["id"]):
            raise HTTPException(409)
        put(session, "events", "event:" + body["id"], body)
    return body


@app.post("/mail")
@app.post("/alerts")
async def capture(request: Request):
    body = await request.json()
    kind = request.url.path.strip("/")
    with Session(engine) as session, session.begin():
        put(session, kind, kind + ":" + body["key"], body)
    return {"ok": True}


@app.get("/state")
def state():
    with Session(engine) as session:
        return {
            kind: [o.data for o in objects(session, kind)]
            for kind in (
                "contacts",
                "deals",
                "associations",
                "events",
                "mail",
                "alerts",
                "llm",
            )
        }
