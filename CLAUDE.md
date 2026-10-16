# Lead to Meeting contributor guide

This is a synthetic, single-tenant reference implementation. Keep all examples free of employer
or client code, personal data and credentials. Original code is MIT licensed.

## Structure

- `app/`: transactional state, provider adapters, local doubles and browser UI.
- `workflows/`: five generated n8n workflow exports; source is `scripts/generate_workflows.py`.
- `tests/`: fast contracts and separate real PostgreSQL process/wire contracts.
- `evals/`: independently annotated synthetic HTTP replay corpus and runner.
- `scripts/`: bootstrap, local verification, browser capture and diagnostics.
- `docs/`: architecture, operational boundaries, integration instructions and ADRs.

## Build and verify

Use Python 3.12+ and Docker Compose. Install `requirements-dev.lock` in `.venv`.
`./scripts/demo.sh` builds and starts the local demo. See `docs/VERIFICATION.md` for individual
verification commands, prerequisites and tested boundaries. On a shared host, invoke each heavy
Docker/build/browser command through its resource queue, taking a separate ticket per test boundary.
Generate exports with `.venv/bin/python scripts/generate_workflows.py`; commit generator and exports
together and verify `git diff --exit-code workflows`.

## Collaboration rules

Default to `INTEGRATION_MODE=mock`; no live providers, LLM calls, keys or expenditure without
explicit owner approval. Keep synthetic local secrets in ignored `.env`; never print them.
Preserve retained identity, budget admission before I/O, uncertain SMTP ownership and privacy
boundaries. Keep provider doubles and live verification limits explicit. Never report skipped
checks as passing. Record failures and diagnose before repeating an unchanged failed scenario.
Repository visibility and publication require owner approval. GitHub Actions is disabled while
private: do not enable, dispatch or rerun workflows; include `[skip ci]` in commits/pushes.
The pinned workflows are optional manually invoked public examples. Preserve history and attribution.
