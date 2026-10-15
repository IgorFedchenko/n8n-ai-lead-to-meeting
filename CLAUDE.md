# Lead to Meeting

Reference implementation owned by Igor Fedchenko. Keep the GitHub repository PRIVATE until Igor
reviews and explicitly approves publishing it. MIT applies to original code; dependencies retain their licenses.

- All fixtures are synthetic. No employer/client names, code or data.
- Default INTEGRATION_MODE=mock; never enable real LLM/API spend or add credentials without Igor's approval.
- Secrets go in the ignored host .env, never in workflow JSON, documentation, tests or commits.
- On Studio, every Docker build/test and browser E2E runs through ~/.local/bin/studio-heavy-run.
- Generate workflows with python3 scripts/generate_workflows.py; commit both generator and exports.
- Acceptance: pytest, Ruff, then actual Docker/n8n/IMAP acceptance via scripts/verify_demo.py.
- Do not declare completion with failing acceptance or CI. Record limitations explicitly.
- No Upwork access is required or authorized by this project.
