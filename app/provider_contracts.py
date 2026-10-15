"""Validate only consumed provider fields; tolerate unrelated upstream additions."""

from datetime import UTC, datetime
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")]


class ProviderPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")


class ObjectReference(ProviderPayload):
    id: Identifier


class SearchResults(ProviderPayload):
    results: list[ObjectReference]


class AccessToken(ProviderPayload):
    access_token: str = Field(min_length=1, max_length=4096, pattern=r"^[A-Za-z0-9._~+/-]+=*$")


def instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Provider timestamp needs a timezone")
    return parsed.astimezone(UTC)


class BusyPeriod(ProviderPayload):
    start: str
    end: str

    @model_validator(mode="after")
    def valid_interval(self) -> Self:
        if instant(self.start) >= instant(self.end):
            raise ValueError("Provider busy interval is invalid")
        return self


class CalendarAvailability(ProviderPayload):
    busy: list[BusyPeriod]
    errors: list[dict] = Field(default_factory=list)


class AvailabilityResponse(ProviderPayload):
    calendars: dict[str, CalendarAvailability]


class EventTime(ProviderPayload):
    dateTime: str

    @field_validator("dateTime")
    @classmethod
    def valid_timestamp(cls, value: str) -> str:
        instant(value)
        return value


class CalendarEvent(ObjectReference):
    start: EventTime
    end: EventTime
