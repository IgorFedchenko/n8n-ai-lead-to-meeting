import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String, create_engine, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

engine = create_engine(os.getenv("DATABASE_URL", "sqlite:///./demo.db"), pool_pre_ping=True)


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "lead_runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class Journal(Base):
    __tablename__ = "run_journal"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    event: Mapped[str] = mapped_column(String(100))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class Alert(Base):
    __tablename__ = "run_alerts"
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(String(100))
    delivered: Mapped[bool] = mapped_column(default=False)


def init_db():
    Base.metadata.create_all(engine)


@contextmanager
def transaction() -> Iterator[Session]:
    with Session(engine) as session, session.begin():
        # Serializes identity resolution and slot reservation across API workers.
        # Small-volume reference implementation; see docs before scaling this lock.
        if engine.dialect.name == "postgresql":
            session.execute(text("SELECT pg_advisory_xact_lock(76102416)"))
        elif engine.dialect.name == "sqlite":
            session.execute(text("BEGIN IMMEDIATE"))
        yield session


def get_run(session: Session, run_id: str) -> Run:
    run = session.get(Run, run_id)
    if run is None:
        from fastapi import HTTPException

        raise HTTPException(404, "Unknown run")
    return run


def save(session: Session, run: Run, data: dict[str, Any], event: str) -> None:
    run.data = dict(data)
    run.updated_at = datetime.now(UTC)
    session.add(Journal(run_id=run.id, event=event))


def alert(session: Session, run_id: str, reason: str) -> None:
    key = f"{run_id}:{reason}"
    if session.get(Alert, key) is None:
        session.add(Alert(key=key, run_id=run_id, reason=reason))


def snapshot(session: Session) -> dict[str, Any]:
    from app.llm_budget import summary

    return {
        "runs": [
            {"id": r.id, **r.data, "llm_usage": summary(session, r.id)}
            for r in session.scalars(select(Run).order_by(Run.updated_at.desc()))
        ],
        "journal": [
            {"run_id": j.run_id, "event": j.event, "at": j.at.isoformat()}
            for j in session.scalars(select(Journal).order_by(Journal.id))
        ],
        "alerts": [
            {"run_id": a.run_id, "reason": a.reason, "delivered": a.delivered}
            for a in session.scalars(select(Alert))
        ],
    }


class LLMAttempt(Base):
    """Admission is committed before HTTP. Reservations never refund automatically."""

    __tablename__ = "llm_attempts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    data: Mapped[dict] = mapped_column(JSON)
