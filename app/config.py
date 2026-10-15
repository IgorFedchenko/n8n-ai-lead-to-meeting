"""Startup validation of live configuration without exposing secret values."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class LiveConfiguration:
    required: tuple[str, ...] = (
        "SERVICE_KEY",
        "WEBHOOK_TOKEN",
        "LLM_MODEL",
        "LLM_API_KEY",
        "LLM_RATE_SOURCE",
        "HUBSPOT_ACCESS_TOKEN",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "GOOGLE_REFRESH_TOKEN",
        "GOOGLE_CALENDAR_ID",
        "SMTP_HOST",
        "SMTP_USER",
        "SMTP_PASSWORD",
        "SMTP_FROM",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    )

    def validate(self) -> None:
        from app.llm_budget import positive_decimal

        missing = [name for name in self.required if not os.getenv(name, "").strip()]
        if missing:
            raise RuntimeError("Missing live configuration: " + ", ".join(missing))
        if os.getenv("LLM_SPEND_APPROVED") != "true":
            raise RuntimeError("Live LLM spend must be explicitly approved")
        for name in ("LLM_INPUT_USD_PER_MILLION", "LLM_OUTPUT_USD_PER_MILLION", "LLM_RUN_BUDGET_USD"):
            positive_decimal(name)
        if os.getenv("LLM_PROVIDER", "anthropic") not in {"anthropic", "openai-compatible"}:
            raise RuntimeError("Unsupported LLM_PROVIDER")
        if os.getenv("LLM_PROVIDER") == "openai-compatible" and not os.getenv("LLM_BASE_URL"):
            raise RuntimeError("Configure LLM_BASE_URL")
        if os.getenv("HUBSPOT_CURRENCY", "USD") not in {"USD", "EUR", "GBP"}:
            raise RuntimeError("Unsupported HUBSPOT_CURRENCY")
