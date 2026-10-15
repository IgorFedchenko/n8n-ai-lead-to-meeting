# Changelog

## Unreleased

- Return generic schema-validation errors and suppress URL-bearing transport logs to protect private input.
- Remove n8n execution payloads on completion and wait for registered, authenticated webhooks at startup.

- Accept printable RFC Message-ID punctuation while retaining replay/collision checks.
- Preserve business progress when an old n8n execution reports a late error.
- Separate active mail reservations from uncertainty and require persisted ownership to finish.
- Admit each physical LLM request against durable call/cost limits; record usage, prompt hashes and latency.
- Map CRM amounts only in the configured account currency and clear stale unsafe amounts.
- Retain unresolved deliveries and pending warnings beyond the normal cleanup window.
- Add synthetic HTTP replay evaluations, opt-in metered live/judge runners, private tracing,
  local Phoenix profile, static/security checks and architecture decisions.

## 0.1.0

- n8n form/IMAP intake, structured qualification, durable HubSpot/Calendar adapters and local demo.
