import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Lead(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_id: str = Field(min_length=3, max_length=200, pattern=r"^[!-~]+$")
    source: Literal["form", "email"] = "form"
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(max_length=254)
    phone: str = ""
    message: str = Field(min_length=10, max_length=12000)
    scenario: Literal["normal", "invalid_once", "invalid", "transient", "crm_fail"] = "normal"

    @field_validator("email")
    @classmethod
    def email_valid(cls, value: str) -> str:
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("A valid email is required")
        return value.casefold()

    @field_validator("phone")
    @classmethod
    def phone_valid(cls, value: str) -> str:
        value = re.sub(r"[\s().-]", "", value)
        if value and not re.fullmatch(r"\+[1-9][0-9]{6,14}", value):
            raise ValueError("Use an international phone number, including +country code")
        return value

    @property
    def key(self) -> str:
        # A stable submission identifier must survive retries and transport changes.
        return hashlib.sha256(self.request_id.encode()).hexdigest()

    @property
    def fingerprint(self) -> str:
        data = self.model_dump(exclude={"source"})
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


class Qualification(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    budget: float | None = Field(ge=0)
    currency: Literal["USD", "EUR", "GBP", "unknown"]
    timeline: str = Field(min_length=1, max_length=200)
    request_type: Literal["automation", "ai_agent", "rag", "dotnet", "other"]
    score: int = Field(ge=0, le=100)
    reason: str = Field(min_length=5, max_length=800)


class Intake(BaseModel):
    lead: Lead
    execution_id: str = Field(default="", max_length=100)


class Booking(BaseModel):
    run_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    token: str = Field(min_length=20, max_length=200)
    slot: str = Field(max_length=40)
