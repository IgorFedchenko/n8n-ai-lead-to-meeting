# Architecture

The [README diagram](../README.md) shows the runtime flow. n8n owns orchestration: production
webhooks accept inquiries and bookings, IMAP supplies email inquiries, an Error Trigger records
workflow failures, and an independent scheduled monitor discovers stalled work.

The Python service owns transactional contracts. PostgreSQL stores normalized identity, run state,
physical LLM attempt reservations, CRM identity locks, booking offers, SMTP ownership and alert
outbox state. Provider adapters use HTTP/SMTP; default mock mode substitutes synthetic HTTP
providers and captures outbound mail. GreenMail supplies actual local SMTP/IMAP input.

Qualification validates structured output locally. Admission commits attempt/cost reservations
before network I/O, including retries and output repair. Invalid output exhausts into manual review
without CRM writes. CRM identity resolution serializes across processes; booking validates the
offer token and rechecks free/busy. Uncertain SMTP acknowledgements retain ownership for manual
reconciliation rather than authorizing resend.

Production n8n execution payloads are hard-deleted on completion; manual editor retention remains
unverified. Safe terminal application records expire after seven days; active, uncertain, manual
and undelivered-alert records remain until resolved. Optional local Phoenix receives allowlisted
metadata only and is absent from default startup.

See [ADRs](adr/README.md) for design decisions, [operations](OPERATIONS.md) for recovery and
[verification](VERIFICATION.md) for executable coverage. Live provider behavior and model quality
remain unverified; local doubles establish contracts, not external service certification.
