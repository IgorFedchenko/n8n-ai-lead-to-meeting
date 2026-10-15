# Private observability

Manual OpenTelemetry run, stage, qualification, LLM-attempt and mail spans use an allowlist of IDs,
stages, status, provider/model, prompt version/hash, token counts, configured reservations and latency.
Structured events use the same allowlist. Exception messages/tracebacks are not recorded on spans.
No automatic HTTP instrumentation captures paths or query strings; prompts, completions, inquiry
text, email/phone, credentials, authorization headers and booking tokens are never attributes.
`traceparent` propagates from application spans to LLM requests; baggage is not forwarded.

By default the SDK has no network exporter. Tests can attach an in-memory span exporter. To inspect
traces with the free, self-hosted Phoenix profile:

```sh
docker compose --profile observability up -d phoenix
# Add to the ignored host .env, then recreate app:
# OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://phoenix:6006/v1/traces
docker compose up -d --force-recreate app
# Open http://localhost:6006 and submit a synthetic lead.
python scripts/verify_phoenix.py
```

Run heavy Docker/browser operations through your host's queue. `verify_phoenix.py` sends an actual
OTLP HTTP span, flushes it and polls the local Phoenix spans endpoint for ingestion, writing
`artifacts/phoenix.json`. Merely creating a span or starting Phoenix is insufficient evidence.
This optional integration must be checked separately from in-memory tracing; no cloud account is
used. Only localhost/127.0.0.1/phoenix exporter hosts without credentials/query are accepted.
Phoenix stores operational metadata on its own local volume; remove that synthetic volume when
appropriate and restrict access before any live use.

API references: [OTel Python](https://opentelemetry.io/docs/languages/python/instrumentation/),
[OTLP HTTP exporter](https://opentelemetry.io/docs/languages/python/exporters/),
[Phoenix self-hosting](https://arize.com/docs/phoenix/self-hosting/deploying-phoenix).
