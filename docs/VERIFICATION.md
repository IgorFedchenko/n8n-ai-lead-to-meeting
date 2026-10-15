# Verification evidence

Acceptance is only complete with green actual-stack and browser checks on the committed revision.
Real SaaS accounts and paid LLM APIs are excluded from this verification.

## Diagnostic history

- Initial CI: unit/contract checks passed; actual n8n flow stopped at CRM with HTTP 503.
  The error workflow was also not published, which n8n 2.43.6 requires before invoking it.
- Second CI: metadata-only diagnostic probe confirmed fingerprint `done=[qualify], failed_step=crm,
  error=Retryable`, with no mock-server exception. The separately published Error Trigger delivered
  the expected warning. Cause: an empty live HubSpot token from `.env.example` produced the illegal
  HTTP header `Authorization: Bearer ` before any mock CRM request was sent.
- Changed variable: mock authentication now always uses a synthetic token; live authentication rejects
  blank configuration. The service test fixture reproduces blank host credentials, and a direct
  regression test checks both modes. No unchanged failing scenario was blindly rerun.

Final passing receipts and screenshots are produced by GitHub Actions and recorded below after acceptance.
