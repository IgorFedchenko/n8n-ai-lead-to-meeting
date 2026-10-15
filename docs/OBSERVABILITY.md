# Private observability

Manual OpenTelemetry run, stage, qualification, LLM-attempt and mail spans use an allowlist of IDs,
stages, status, provider/model, prompt version/hash, token counts, configured reservations and latency.
Structured events use the same allowlist. Exception messages/tracebacks are not recorded on spans.
No automatic HTTP instrumentation captures paths or query strings; prompts, completions, inquiry
text, email/phone, credentials, authorization headers and booking tokens are never attributes.
Schema-validation responses omit rejected input, and third-party HTTP URL logging is disabled.
For production webhook, IMAP and scheduled executions, n8n saves neither successful nor failed
payloads; pruning is disabled so its `save=none` lifecycle
hard-deletes completed execution data immediately instead of retaining soft-deleted copies until
a pruning cycle. In-flight n8n payloads are temporary private operational data during execution.
This guarantee is tested only for those automated production modes. Editor/manual executions are
untested and have a separate soft-delete lifecycle that may retain payloads with this configuration.
Use synthetic data only in the local editor, and remove the local stack volumes after experiments
(`docker compose --profile observability down -v`, intentionally erasing all local synthetic state).
Do not treat the editor as a verified private live-data debugging environment.
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

Run heavy Docker/browser operations through your host's queue. `verify_phoenix.py` configures the application exporter, recreates the application, submits an
actual n8n inquiry and polls Phoenix for its correlated run/qualification/LLM spans, checking
trace IDs and parentage. It also probes malformed intake and checks container logs, n8n stored
execution data, spans and alert metadata for synthetic privacy sentinels, writing
`artifacts/phoenix.json`. Merely creating a span or starting Phoenix is insufficient evidence.
This optional integration must be checked separately from in-memory tracing; no cloud account is
used. Only localhost/127.0.0.1/phoenix exporter hosts without credentials/query are accepted.
Phoenix stores operational metadata on its own local volume; remove that synthetic volume when
appropriate and restrict access before any live use.

API references: [OTel Python](https://opentelemetry.io/docs/languages/python/instrumentation/),
[OTLP HTTP exporter](https://opentelemetry.io/docs/languages/python/exporters/),
[Phoenix self-hosting](https://arize.com/docs/phoenix/self-hosting/deploying-phoenix).
