# 002 — Instrument explicit safe metadata

Status: accepted.

Automatic HTTP/LLM instrumentation can capture sensitive URLs, prompt bodies and responses.
Use manual OpenTelemetry spans and structured JSON events with an attribute allowlist, disabled
exception recording and no default network exporter. Operational payloads stay in authenticated
state; telemetry contains correlation and bounded accounting metadata only.

Optional local Phoenix receives OTLP HTTP without a cloud account. It is outside default startup;
an exporter-to-ingestion smoke check is separate from in-memory span assertions. No second tracing
backend or paid observability account is needed. Operators still own access control and retention.
