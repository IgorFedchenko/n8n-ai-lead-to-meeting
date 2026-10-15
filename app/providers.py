import hashlib
import json
import math
import os
import smtplib
import time
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from functools import cached_property
from typing import Any
from urllib.parse import quote

import httpx
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from app.models import Qualification
from app.qualification import SYSTEM_PROMPT, attempt_hooks, output_limit, usage_hook
from app.telemetry import operation


class Retryable(Exception):
    pass


class IntegrationError(Exception):
    pass


class Providers:
    def __init__(self):
        self.mode = os.getenv("INTEGRATION_MODE", "mock")
        if self.mode not in {"mock", "live"}:
            raise RuntimeError("INTEGRATION_MODE must be mock or live")
        self.mock_url = os.getenv("MOCK_URL", "http://mock:8001")

    @cached_property
    def client(self):
        return httpx.Client(timeout=20, follow_redirects=False)

    def request(self, method, url, **kwargs):
        for attempt in range(3):
            if kwargs.pop("_llm", False):
                raise AssertionError("Use request_llm for metered calls")
            try:
                response = self.client.request(method, url, **kwargs)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt == 2:
                        raise Retryable("Remote service unavailable after three attempts")
                    try:
                        wait = min(max(float(response.headers.get("retry-after", 2**attempt)), 0), 10)
                    except ValueError:
                        wait = 2**attempt
                    time.sleep(wait * float(os.getenv("RETRY_DELAY_SCALE", "1")))
                    continue
                return response
            except httpx.TransportError:
                if attempt == 2:
                    # Ambiguous writes are reconciled by provider-specific stable identifiers.
                    raise Retryable("Remote transport failed") from None
                time.sleep(2**attempt * float(os.getenv("RETRY_DELAY_SCALE", "1")))
        raise AssertionError("unreachable")

    def request_llm(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(3):
            admit = attempt_hooks.get()
            if admit is None:
                raise IntegrationError("LLM requires durable attempt admission")
            if len(json.dumps(kwargs.get("json", {}), ensure_ascii=False).encode()) > int(
                os.getenv("LLM_MAX_INPUT_BYTES", "20000")
            ):
                raise ValueError("LLM serialized input exceeds byte limit")
            attempt_id = admit()
            started = time.monotonic()
            outcome = "ambiguous"
            usage = None
            try:
                with operation(
                    "llm.attempt", stage="qualify", provider=os.getenv("LLM_PROVIDER", "anthropic")
                ):
                    headers = dict(kwargs.get("headers", {}))
                    TraceContextTextMapPropagator().inject(headers)
                    kwargs["headers"] = headers
                    response = self.client.request(method, url, **kwargs)
                outcome = "http_error" if response.is_error else "response"
                if not response.is_error:
                    body = response.json()
                    usage = body.get("usage") if isinstance(body, dict) else None
                if response.status_code != 429 and response.status_code < 500:
                    return response
            except httpx.TransportError:
                pass
            finally:
                record = usage_hook.get()
                if record:
                    record(attempt_id, outcome, usage, (time.monotonic() - started) * 1000)
            if attempt < 2:
                time.sleep(2**attempt * float(os.getenv("RETRY_DELAY_SCALE", "1")))
        raise Retryable("LLM attempt failed; reservation retained")

    def checked(self, response):
        if response.is_error:
            # Never include provider bodies, request URLs or credentials in logs/alerts.
            raise IntegrationError(f"Remote service returned HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            raise IntegrationError("Remote service returned invalid JSON") from None
        if not isinstance(body, dict):
            raise IntegrationError("Remote service returned an invalid envelope")
        return body

    def qualify(self, lead: dict[str, Any]) -> str:
        data = dict(lead)
        if self.mode == "live":
            data.pop("scenario", None)
        return self.structured(SYSTEM_PROMPT, {"untrusted_inquiry": data}, Qualification.model_json_schema())

    def structured(self, system: str, data: dict[str, Any], schema: dict[str, Any]) -> str:
        provider = os.getenv("LLM_PROVIDER", "anthropic")
        base = (
            self.mock_url
            if self.mode == "mock"
            else "https://api.anthropic.com"
            if provider == "anthropic"
            else os.environ["LLM_BASE_URL"]
        )
        prompt = json.dumps(data, ensure_ascii=False)
        # Anthropic supports a subset: range/length checks remain enforced locally.
        api_schema = json.loads(json.dumps(schema))
        for prop in api_schema["properties"].values():
            for key in ("minimum", "maximum", "minLength", "maxLength"):
                prop.pop(key, None)
            for item in prop.get("anyOf", []):
                item.pop("minimum", None)
        model = os.getenv("LLM_MODEL", "")
        if self.mode == "live" and not model:
            raise IntegrationError("Configure LLM_MODEL before enabling live mode")
        if provider == "anthropic":
            response = self.request_llm(
                "POST",
                f"{base}/v1/messages",
                headers={"x-api-key": os.getenv("LLM_API_KEY", "mock"), "anthropic-version": "2023-06-01"},
                json={
                    "model": model or "recorded-demo",
                    "max_tokens": output_limit(),
                    "system": system,
                    "messages": [{"role": "user", "content": prompt}],
                    "output_config": {"format": {"type": "json_schema", "schema": api_schema}},
                },
            )
            body = self.checked(response)
            content = body.get("content", [])
            if not isinstance(content, list) or any(not isinstance(part, dict) for part in content):
                raise IntegrationError("LLM returned an invalid content envelope")
            texts = [part.get("text", "") for part in content if part.get("type") == "text"]
            if any(not isinstance(value, str) for value in texts):
                raise IntegrationError("LLM returned invalid text")
            return "".join(texts)
        if provider != "openai-compatible":
            raise IntegrationError("Unsupported LLM_PROVIDER")
        body = self.checked(
            self.request_llm(
                "POST",
                f"{base}/chat/completions",
                headers={"Authorization": "Bearer " + os.environ["LLM_API_KEY"]},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": output_limit(),
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {"name": "qualification", "strict": True, "schema": api_schema},
                    },
                },
            )
        )
        try:
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise IntegrationError("LLM returned an invalid choice envelope") from None
        if not isinstance(text, str):
            raise IntegrationError("LLM returned invalid text")
        return text

    @property
    def crm_base(self):
        return self.mock_url if self.mode == "mock" else "https://api.hubapi.com"

    @property
    def crm_headers(self):
        token = "mock" if self.mode == "mock" else os.getenv("HUBSPOT_ACCESS_TOKEN", "")
        if not token.strip():
            raise IntegrationError("Configure HUBSPOT_ACCESS_TOKEN before using live CRM")
        return {"Authorization": "Bearer " + token}

    def contact(self, lead):
        matches = {}
        for field in ("email", "phone"):
            if not lead[field]:
                continue
            body = self.checked(
                self.request(
                    "POST",
                    f"{self.crm_base}/crm/v3/objects/contacts/search",
                    headers=self.crm_headers,
                    json={
                        "filterGroups": [
                            {"filters": [{"propertyName": field, "operator": "EQ", "value": lead[field]}]}
                        ],
                        "limit": 2,
                    },
                )
            )
            for item in body["results"]:
                matches[item["id"]] = item
        if len(matches) > 1:
            raise IntegrationError("Email and phone identify conflicting contacts; manual review required")
        properties = {"email": lead["email"], "phone": lead["phone"], "firstname": lead["name"]}
        if matches:
            contact_id = next(iter(matches))
            return self.checked(
                self.request(
                    "PATCH",
                    f"{self.crm_base}/crm/v3/objects/contacts/{contact_id}",
                    headers=self.crm_headers,
                    json={"properties": properties},
                )
            )["id"]
        response = self.request(
            "POST",
            f"{self.crm_base}/crm/v3/objects/contacts",
            headers=self.crm_headers,
            json={"properties": properties},
        )
        if response.status_code == 409:
            # Email is a HubSpot unique identifier; recover an ambiguous successful create.
            return self.checked(
                self.request(
                    "GET",
                    f"{self.crm_base}/crm/v3/objects/contacts/{quote(lead['email'], safe='')}",
                    params={"idProperty": "email"},
                    headers=self.crm_headers,
                )
            )["id"]
        return self.checked(response)["id"]

    def deal(self, run_id, lead, qualification, contact_id):
        properties = {
            "dealname": f"Inquiry {lead['request_id']}",
            "portfolio_request_id": run_id,
            "pipeline": os.getenv("HUBSPOT_PIPELINE", "default"),
            "dealstage": os.getenv("HUBSPOT_DEAL_STAGE", "appointmentscheduled"),
            "description": json.dumps(qualification),
        }
        budget = qualification["budget"]
        if budget is not None and (not math.isfinite(budget) or budget < 0):
            raise IntegrationError("Budget must be finite and nonnegative")
        safe_amount = budget is not None and qualification["currency"] == os.getenv("HUBSPOT_CURRENCY", "USD")
        if safe_amount:
            properties["amount"] = str(budget)
        url = f"{self.crm_base}/crm/v3/objects/deals"
        lookup = self.request(
            "GET", f"{url}/{run_id}", headers=self.crm_headers, params={"idProperty": "portfolio_request_id"}
        )
        if lookup.status_code == 404:
            response = self.request("POST", url, headers=self.crm_headers, json={"properties": properties})
            if response.status_code == 409:
                response = self.request(
                    "GET",
                    f"{url}/{run_id}",
                    headers=self.crm_headers,
                    params={"idProperty": "portfolio_request_id"},
                )
                recovered_id = self.checked(response)["id"]
                if not safe_amount:
                    properties["amount"] = ""
                self.checked(
                    self.request(
                        "PATCH",
                        f"{url}/{recovered_id}",
                        headers=self.crm_headers,
                        json={"properties": properties},
                    )
                )
            deal_id = self.checked(response)["id"]
        else:
            deal_id = self.checked(lookup)["id"]
            if not safe_amount:
                properties["amount"] = ""  # HubSpot clears property values with an empty string.
            self.checked(
                self.request(
                    "PATCH", f"{url}/{deal_id}", headers=self.crm_headers, json={"properties": properties}
                )
            )
        self.checked(
            self.request(
                "PUT",
                f"{url}/{deal_id}/associations/contacts/{contact_id}/deal_to_contact",
                headers=self.crm_headers,
            )
        )
        return deal_id

    def calendar_config(self):
        if self.mode == "mock":
            return self.mock_url + "/calendar/v3", "demo", {}
        token = self.checked(
            self.request(
                "POST",
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": os.environ["GOOGLE_CLIENT_ID"],
                    "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                    "refresh_token": os.environ["GOOGLE_REFRESH_TOKEN"],
                    "grant_type": "refresh_token",
                },
            )
        )["access_token"]
        return (
            "https://www.googleapis.com/calendar/v3",
            quote(os.environ["GOOGLE_CALENDAR_ID"], safe=""),
            {
                "Authorization": "Bearer " + token,
            },
        )

    def free(self, slots):
        base, calendar, headers = self.calendar_config()
        body = self.checked(
            self.request(
                "POST",
                base + "/freeBusy",
                headers=headers,
                json={
                    "timeMin": slots[0],
                    "timeMax": end_of(slots[-1]),
                    "items": [{"id": os.getenv("GOOGLE_CALENDAR_ID", "demo")}],
                },
            )
        )
        info = body["calendars"][os.getenv("GOOGLE_CALENDAR_ID", "demo")]
        if info.get("errors"):
            raise IntegrationError("Calendar availability cannot be verified")
        return [
            s
            for s in slots
            if not any(
                parse_time(s) < parse_time(b["end"]) and parse_time(end_of(s)) > parse_time(b["start"])
                for b in info["busy"]
            )
        ]

    def book(self, run_id, slot):
        base, calendar, headers = self.calendar_config()
        event_id = hashlib.sha256(("meeting:" + run_id).encode()).hexdigest()  # Google base32hex subset
        url = f"{base}/calendars/{calendar}/events"
        existing = self.request("GET", f"{url}/{event_id}", headers=headers)
        if existing.status_code != 404:
            event = self.checked(existing)
            if parse_time(event["start"]["dateTime"]) != parse_time(slot):
                raise IntegrationError("A different slot is already reserved for this lead")
            return event_id
        if slot not in self.free([slot]):
            from fastapi import HTTPException

            raise HTTPException(409, "Slot is no longer available; request fresh slots")
        response = self.request(
            "POST",
            url,
            headers=headers,
            params={"sendUpdates": "none"},
            json={
                "id": event_id,
                "summary": "Discovery meeting",
                "start": {"dateTime": slot},
                "end": {"dateTime": end_of(slot)},
                "description": "Reference implementation booking " + run_id,
            },
        )
        if response.status_code == 409:
            self.checked(self.request("GET", f"{url}/{event_id}", headers=headers))
        else:
            self.checked(response)
        return event_id

    def email(self, key, recipient, subject, body):
        message = EmailMessage()
        message["From"] = os.getenv("SMTP_FROM", "demo@example.test")
        message["To"] = recipient
        message["Subject"] = subject
        message["Message-ID"] = f"<{hashlib.sha256(key.encode()).hexdigest()}@lead-demo.invalid>"
        message.set_content(body)
        if self.mode == "mock":
            self.checked(
                self.request(
                    "POST",
                    self.mock_url + "/mail",
                    json={
                        "key": key,
                        "to": recipient,
                        "subject": subject,
                        "body": body,
                    },
                )
            )
            return
        # SMTP has no exactly-once acknowledgement. Ambiguous deliveries require operator reconciliation.
        with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.getenv("SMTP_PORT", "587")), timeout=20) as smtp:
            smtp.starttls()
            smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
            smtp.send_message(message)

    def notify(self, key, run_id, reason):
        if self.mode == "mock":
            self.checked(
                self.request(
                    "POST",
                    self.mock_url + "/alerts",
                    json={
                        "key": key,
                        "run_id": run_id,
                        "reason": reason,
                    },
                )
            )
        else:
            token = os.environ["TELEGRAM_BOT_TOKEN"]
            self.checked(
                self.request(
                    "POST",
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={
                        "chat_id": os.environ["TELEGRAM_CHAT_ID"],
                        "text": f"Lead automation: {reason}; run {run_id[:12]}. "
                        "Open the private run journal.",
                    },
                )
            )


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def end_of(slot):
    return (parse_time(slot) + timedelta(minutes=30)).isoformat()


def candidate_slots():
    day = datetime.now(UTC).replace(hour=10, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return [
        (day + timedelta(days=d, hours=h)).isoformat()
        for d in range(7)
        if (day + timedelta(days=d)).weekday() < 5
        for h in (0, 2, 4)
    ]
