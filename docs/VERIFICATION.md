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

The fast tests use SQLite and HTTP/SMTP protocol doubles. They do not establish real n8n,
Postgres lock behavior, IMAP delivery or live provider compatibility. The synthetic eval runner
injects raw HTTP completion responses through the actual qualification route and database accounting;
it compares results to separate annotations, not fixture-to-fixture equality.

Actual stack verification starts n8n, Postgres, adapter service, synthetic HTTP providers and
GreenMail. It submits authenticated webhooks, delivers SMTP into the real IMAP trigger and
observes persisted outcomes, captured mail, warnings and booking. Chromium checks form/replay,
booking and the recipient link. HubSpot, Calendar, LLM and alert delivery remain local doubles.

```sh
./scripts/demo.sh
python3 scripts/verify_demo.py
python scripts/capture_demo.py
```

On a host with a shared heavy-job queue, prefix Docker/build/browser commands with its queue wrapper.
CI runs the real stack on an isolated hosted runner. Machine-readable receipts and browser videos
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
fixes packages. Findings, lookup failures and tool errors fail CI. JSON reports are uploaded under
`dependency-audit/`. A successful audit means no known advisories at the query time, not a guarantee
that dependencies have no vulnerabilities. No advisories are suppressed.
