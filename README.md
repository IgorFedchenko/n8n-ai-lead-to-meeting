# AI Lead Qualification to CRM and Meeting Booking

[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A working **reference implementation** for businesses that receive inquiries through web forms
and email, then manually qualify leads, update their CRM and arrange discovery calls.
Five n8n workflows turn an inquiry into a qualified HubSpot contact and deal, propose available
meeting slots, and record a Google Calendar event with an email confirmation.

The local demo costs **$0 in API usage**: synthetic LLM responses, HTTP doubles for HubSpot and
Google Calendar, a synthetic mail outbox, and a real local IMAP inbox. All names and records are
synthetic. No employer/client code or data is included.

![Running synthetic local demo](docs/images/demo.png)

![Synthetic workflow recording](docs/images/demo.gif)

[Recorded booking](docs/images/booked.png) · [Reproduce verification](docs/VERIFICATION.md)

```mermaid
flowchart LR
    F[Web form / authenticated webhook] --> N[n8n intake workflow]
    I[Dedicated IMAP inbox] --> E[n8n email workflow]
    E --> A
    N --> A[Normalize + deduplicate]
    A --> L[LLM: structured JSON]
    L --> V{Local schema validation}
    V -->|retry once| L
    V -->|invalid twice| Q[Manual review queue]
    V -->|valid| H[HubSpot contact + deal upsert]
    H --> S[Calendar free/busy + email slot proposal]
    S --> B[n8n booking webhook]
    B --> G[Calendar event + email confirmation]
    A -. durable state .-> P[(Postgres journal / idempotency / alert outbox)]
    G -. durable state .-> P
    X[n8n Error Trigger + independent minute monitor] --> P
    P --> T[Telegram warning / local capture]
```

## Integration status

| Status | Components |
| --- | --- |
| Runs locally | Five n8n workflows, Postgres, adapter service and GreenMail IMAP/SMTP. |
| Local doubles | LLM completions, HubSpot, Google Calendar and notification delivery. |
| Implemented, live-unverified | LLM, HubSpot, Google OAuth/Calendar, SMTP and Telegram adapters. |
| Optional, off by default | Local Phoenix tracing; paid calls require separate approval. |

## Run it

Requires Docker with Compose and Python 3.12+. No API keys or SaaS registration required.

```sh
git clone git@github.com:IgorFedchenko/n8n-ai-lead-to-meeting.git
cd n8n-ai-lead-to-meeting
./scripts/demo.sh
```

Open **http://localhost:8088**. Send an inquiry, click **Replay** to check deduplication, and
book one of the displayed slots. Inspect CRM counts, structured qualification, run journal
and captured email/warning delivery. Choose the invalid-response scenario to see manual review.
The UI calls actual n8n production webhooks rather than bypassing the workflows.

The n8n editor is **http://localhost:5679**. Create a local owner account on first visit; this is
separate from any n8n Cloud account. Workflows and local credentials are imported and published
by the bootstrap container. Workflow `04` is an error handler and is invoked by the other workflows.

`demo.sh` creates an ignored `.env` with random local service/webhook/database secrets (mode 0600).
Only loopback ports are published. Postgres, IMAP and provider doubles stay on the Docker network.
On a resource-constrained or shared host, run Docker/browser jobs through its resource queue.
The first launch downloads images and installs pinned Python packages. Startup may take several minutes.

```sh
# Actual n8n + Postgres + IMAP acceptance, with concurrent replay and negative controls:
python3 scripts/verify_demo.py

# Stop containers; keep the local database:
docker compose down
# To intentionally erase synthetic records and the local n8n owner account:
docker compose down -v
```

## Repository

```text
workflows/     Five importable n8n JSON exports (no secrets)
app/           Transactional state, adapters, synthetic HTTP doubles, demo UI
fixtures/      Synthetic requests, including repair/manual-review/retry scenarios
scripts/       Bootstrap, workflow generator and actual-stack acceptance
tests/         Provider-contract, state, validation, failure and security tests
docs/          Integration setup, architecture and reliability boundaries
compose.yaml   n8n + Postgres + adapter service + doubles + local IMAP/SMTP
```

## Reliability and security

- **Submission identity:** callers supply a stable `request_id`; SHA-256 indexes the run. Reusing
  it with a changed payload returns 409. Source transport may change without changing identity.
- **CRM identity:** normalize email and E.164 phone, resolve both before writing. Conflicting
  matches fail for operator review. Deals use a unique `portfolio_request_id` HubSpot property.
  A Postgres advisory transaction lock serializes identity resolution across workers.
- **Retries:** 429, 5xx and transport errors get three attempts with exponential delays (1/2 s,
  bounded Retry-After); n8n retries a failed HTTP node three times with a one-second pause.
  LLM physical attempts share a durable per-run admission budget (including retries/repair);
  CRM/calendar retries remain nested. Read [operational limits](docs/INTEGRATIONS.md).
- **Structured AI:** Anthropic Claude is the default live adapter; provider/model are configuration.
  An OpenAI-compatible structured-output adapter is also available. Local validation remains
  mandatory. Invalid responses get one further attempt, then manual review with no CRM writes.
- **Booking:** only offered, future slots with the matching secret token are accepted. Free/busy
  is checked again. A deterministic Google event ID prevents duplicate meetings on replay.
- **Email:** a durable sending reservation prevents automatic resends after uncertain SMTP
  acknowledgement. Such deliveries require operator reconciliation; SMTP cannot promise exactly once.
- **Warnings:** separate error workflow, zero-write checks, stalled-run monitoring and a durable
  alert outbox. Live notifications use Telegram; local mode captures them without contacting anyone.
- **Privacy:** webhook authentication, service authentication, no attachments, bounded input lengths,
  plain-text emails, prompt inputs treated as data, no provider bodies in alerts, no completed production-webhook/IMAP/scheduled n8n execution
  payload persistence. Editor/manual retention is unverified and may retain payloads; use only
  synthetic data there and erase local volumes after experiments. Seven-day cleanup covers safe terminal runs only; active/uncertain/manual
  and undelivered-alert records remain until resolved. See [operator procedures](docs/OPERATIONS.md).

## Connect real services

The adapters contain real HTTP/SMTP integration code. Follow [integration setup](docs/INTEGRATIONS.md)
for a free HubSpot developer test account/private app, a dedicated IMAP mailbox, Google Calendar
OAuth, SMTP and Telegram. The operator supplies credentials on the host. **Do not enable live mode
or incur LLM API charges without the owner's approval.** No real credentials or live calls were
needed to build or verify the reference implementation.

## Tests and CI

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.lock
.venv/bin/ruff check app tests scripts evals
.venv/bin/mypy app evals
.venv/bin/python -m evals.run --offline
.venv/bin/pre-commit install
.venv/bin/pytest -q
```

The commands above run local fast checks and synthetic replay. For browser checks install
`.venv/bin/pip install playwright==1.58.0`, then
`.venv/bin/python -m playwright install chromium`. Separate actual-stack, PostgreSQL and
application-to-Phoenix commands and their verified boundaries are in [verification](docs/VERIFICATION.md).
Run heavy Docker/build/browser commands through the host's resource queue when available.

GitHub Actions remains disabled after publication; no runner minutes are used for
final verification. Pinned workflows remain optional manual examples guarded to public repositories.
Earlier hosted passes are historical evidence, not a claim that final-source CI ran or is green.

## AI accounting, evaluation and traces

Each physical LLM HTTP attempt is admitted in a committed Postgres record before network I/O.
Retries and one output-repair share a retained run's attempt/cost ceiling; ambiguous errors consume
the reservation. Usage returned by providers, elapsed time, prompt/schema hashes and explicitly
configured cost estimates appear in the private run state. Missing usage stays unknown. Rates are
operator inputs, not a price guarantee; set provider account spend caps separately.

[Offline evals](evals/README.md) replay 14 separately annotated synthetic cases through the real HTTP
qualification/repair route. They test regression behavior, schema rejection and routing; **no live
model quality or injection resistance has been measured**. Optional live generation and rubric-judge
commands are gated by explicit approval, credentials and durable call/cost limits. They never run in CI.

[Manual OpenTelemetry spans and optional local Phoenix](docs/OBSERVABILITY.md) export allowlisted
metadata only. Default execution makes no telemetry network calls. Prompt text, response text,
contacts, booking tokens, headers and query strings are excluded from spans and structured events.

Local verification runs lint/format, mypy, tests, synthetic replay, a locked-dependency vulnerability
audit, Bandit and a local secret scanner. An inactive [Dependabot example](docs/examples/dependabot.yml)
covers Python, Actions and Docker; there is no active automatic-update configuration. Public opt-in
requires explicit owner approval. CodeQL is guarded to public repositories; private paid Code
Security/native secret-scanning entitlements are not enabled or claimed. See [verification](docs/VERIFICATION.md)
[architecture](docs/ARCHITECTURE.md) and [architecture decisions](docs/adr/README.md).

## Boundaries

This is a small-volume, single-tenant reference implementation. Live HubSpot/Google/SMTP/LLM
acceptance requires operator-owned accounts and credentials; the committed automated proof uses
local doubles. The demo UI is synthetic-only and is disabled in live mode. Do not expose the
local Compose setup to the Internet; add TLS, a reverse proxy, rate limiting and operator RBAC
before hosting it. Booking offers last seven days; expired offers require operator follow-up.

The coarse transaction lock favors correctness over throughput. Calendar free/busy and insert
are separate API calls: another system can still create an overlapping event between them.
Idempotency and durable budget enforcement cover retained identity/accounting records; a replay after deletion can create a new inquiry.
Provider-owned CRM and Calendar records are not deleted by local retention. Backups and external
provider retention remain operator responsibilities. Currency conversion, rescheduling, spam
screening, calendar invitations/video links and an operator editing UI are outside this demo.

See [Make/Zapier comparison](docs/ALTERNATIVES.md) for portability and free-tier constraints.

MIT license for original source. n8n and other dependencies retain their respective licenses;
the MIT license does not relicense the bundled runtime dependencies.
