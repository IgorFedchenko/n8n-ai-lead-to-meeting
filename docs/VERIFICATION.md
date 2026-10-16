# Local final verification policy

Private GitHub Actions is disabled. Do not enable, dispatch or rerun it. The pinned workflow
files are optional manually invoked public examples; historical hosted receipts do not establish
final-source CI status. The current local fast checks cover lint/format, types, security scans, 131 fast tests and 14
synthetic replay cases. The actual n8n/Postgres/GreenMail/browser/Phoenix checks were executed
successfully on the preceding runtime revision using isolated hosted runners. Subsequent packaging
changes leave app, build, dependency, workflow and test inputs unchanged; those actual-system receipts
are reused rather than described as a new local or final-source CI execution. Mac/ARM full-stack
startup remains unverified.

Run each heavy Docker/build/browser operation through the host's resource queue. On hosts with
`studio-heavy-run`, prefix each individual heavy command with `studio-heavy-run --`; do not wrap
a series of suites in one ticket. Keep stack namespaces isolated and clean up only owned resources.
Do not count skipped PostgreSQL tests as acceptance.

# Reproduce verification

All default verification is synthetic and free of SaaS/LLM calls. Install `requirements-dev.lock`
into a Python 3.12 virtualenv. CI commands are visible in `.github/workflows/ci.yml`.

```sh
.venv/bin/ruff check app tests scripts evals
.venv/bin/ruff format --check app tests scripts evals
.venv/bin/mypy app evals
.venv/bin/bandit -r app evals -q
.venv/bin/python scripts/audit_dependencies.py
.venv/bin/python scripts/scan_secrets.py
.venv/bin/pytest -q
.venv/bin/python -m evals.run --offline --output artifacts/evals.json
.venv/bin/python scripts/generate_workflows.py
git diff --exit-code workflows
```

The fast tests use SQLite and HTTP protocol doubles; separate process tests use PostgreSQL
and a loopback SMTP wire server. They do not establish real n8n,
Postgres lock behavior, IMAP delivery or live provider compatibility. The synthetic eval runner
injects raw HTTP completion responses through the actual qualification route and database accounting;
it compares results to separate annotations, not fixture-to-fixture equality.

Actual stack verification starts n8n, Postgres, adapter service, synthetic HTTP providers and
GreenMail. It submits authenticated webhooks, delivers SMTP into the real IMAP trigger and
observes persisted outcomes, captured mail, warnings and booking. Chromium checks form/replay,
booking and the recipient link. HubSpot, Calendar, LLM and alert delivery remain local doubles.

```sh
./scripts/demo.sh
python scripts/capture_demo.py
python3 scripts/verify_demo.py
```

On a host with a shared heavy-job queue, prefix Docker/build/browser commands with its queue wrapper.
Historical hosted acceptance ran the real stack in an isolated Compose project. Machine-readable receipts and browser videos
are uploaded as artifacts; only synthetic screenshots/GIF belong in the source tree.
Convert a passing current browser recording with `scripts/encode_demo.sh CAPTURE.webm`; on a queued
host run encoding through the same heavy-job wrapper. Screenshots/recording illustrate the synthetic
local workflow, not live SaaS certification.

Optional local Phoenix ingestion requires its [separate smoke check](OBSERVABILITY.md).
Live services and model/judge quality need operator-owned credentials, approval and a new explicit
verification record. No live quality score is implied by a synthetic replay pass.

CodeQL runs only when the repository is public. Private paid Code Security/native secret-scanning
entitlements are unavailable for this reference; Bandit and detect-secrets run without those products.

The pinned free `pip-audit` command checks both complete committed locks against public PyPI
advisories. It performs advisory lookup only (`--no-deps --disable-pip`); it neither installs nor
fixes packages. Findings, lookup failures and tool errors fail verification. JSON reports are uploaded under
`dependency-audit/`. A successful audit means no known advisories at the query time, not a guarantee
that dependencies have no vulnerabilities. No advisories are suppressed.

## Coverage and command mapping

The checks below exercise synthetic data. The actual boundary in the second column matters:
HTTP doubles establish provider wire contracts, not a live service certification.

| Contract | Executable check and boundary |
|---|---|
| Bootstrap, generated exports, loopback ports, restart | `scripts/demo.sh`, generator diff, `scripts/verify_demo.py`; real Docker/n8n/Postgres/GreenMail |
| Intake identity and RFC Message-ID punctuation | `test_pipeline.py` identity tests; concurrent actual n8n redelivery and SMTP → GreenMail → n8n IMAP in `verify_demo.py` |
| CRM identity, currency create/PATCH, malformed shapes, lost acknowledgement | `test_acceptance_contracts.py`; actual provider adapter HTTP requests into isolated doubles, recorded payload and resulting state assertions |
| CRM process serialization | `test_postgres_contracts.py::test_two_process_crm_replay_one_contact_deal_and_association`; two app processes, real Postgres and loopback HTTP provider |
| Qualification schema, repair, routing, limits | `test_pipeline.py`, `test_acceptance_contracts.py`, `evals.run`; actual parser/routes/accounting with synthetic raw HTTP responses |
| Booking and recipient page | token/expiry/slot negatives in fast tests; real n8n booking and Chromium recipient-link navigation in `capture_demo.py` |
| SMTP reservation ownership | PostgreSQL process tests pause a loopback SMTP server after DATA, race active duplicates, expire ownership, and release success/failure after reconciliation; one physical DATA per schedule, with the verified-delivery reconciliation CLI exercised before late completion |
| Error workflow and independent monitor | `verify_demo.py`; actual n8n Error Trigger, scheduled stale-run discovery without direct monitor invocation, captured alert double |
| Retained limits, admission before I/O, crash | PostgreSQL tests observe committed reservations from an independent connection at actual loopback HTTP arrival; lost acknowledgement + nested retry + repair + restarted caller; process exit after admission retains charge |
| Usage and observability | exact/unknown usage PostgreSQL tests, in-memory span parentage/privacy; `verify_phoenix.py` configures the actual app and validates received run/child spans in real Phoenix |
| Retention exceptions | fast and PostgreSQL tests retain active/uncertain/manual/pending-alert state and purge eligible old state |
| Evals and tool failure controls | replay corruption, stale provenance, missing coverage and semantic-change negatives; real secret scanner clean/detected controls; mypy negative; dependency-audit finding/tool-error controls |

Run database process contracts separately against a disposable PostgreSQL database:

```sh
TEST_POSTGRES_URL="$DISPOSABLE_DATABASE_URL" \
  .venv/bin/pytest -q -m postgres --junitxml=artifacts/postgres.xml
.venv/bin/pytest -q -m 'not postgres' --junitxml=artifacts/tests.xml
```

Each database test creates and removes its own schema. The database user must have schema creation
permission. With no `TEST_POSTGRES_URL`, those tests explicitly skip locally; the historical dedicated PostgreSQL job supplied it. A local skip is not evidence of database acceptance.
The SMTP server in these tests is a minimal loopback test server; live TLS/authenticated SMTP
compatibility remains unverified. A "not delivered" reconciliation fixture preserves uncertainty
and never authorizes automatic resend; the documented operator command supports verified delivery only.

The verified actual-stack sequence built the stack, used Chromium, recorded `capture_demo.py`
before bulk acceptance fixtures, ran `verify_demo.py`, then started optional Phoenix and ran
`verify_phoenix.py`. These are separate commands, not a one-command verification driver.
Default startup does not start Phoenix. The existing genuine committed GIF remains unchanged.
Its receipt includes trace/span IDs,
parent IDs and counts, without raw prompts, completions, headers, contacts or booking URLs.
Privacy checks cover the actual app/mock/n8n/bootstrap logs, Phoenix data, n8n `execution_data`
and alert metadata, including malformed intake/schema errors. These execution-data checks cover
production webhooks, IMAP and scheduled workflows. Editor/manual execution privacy and retention
remain unverified; use only synthetic editor data and erase local volumes after experiments. Synthetic operational state and
mailbox bodies are intentionally private application data, not telemetry artifacts.

`detect-secrets==1.5.0` is the free secret scanner used here. The reviewed baseline allows only exact
synthetic/public fingerprints, including local CI database fixtures; it does not exclude directories.
The positive control adds a deliberately nonfunctional credential-shaped value in an isolated Git
repository and requires a nonzero scan exit, while an ordinary file must pass. Historical CI recorded native
private CodeQL/secret scanning as `SKIPPED_UNAVAILABLE`; free scans still must succeed.

The [Dependabot configuration example](examples/dependabot.yml) is inactive outside `.github/`.
Automatic dependency updates are disabled; enabling the example for a public repository requires
explicit owner approval. Repository Actions settings alone do not prevent Dependabot jobs.
