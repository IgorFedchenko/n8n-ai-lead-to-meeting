# 001 — Keep orchestration visible; persist side-effect ownership

Status: accepted.

n8n owns visible form/IMAP, qualification, CRM, slot, booking, error and monitor workflow steps.
Python/Postgres own retained identity, schema validation, alert outbox and side-effect reconciliation.
Independent search/create workflow nodes cannot serialize concurrent requests safely.

A coarse Postgres advisory transaction lock protects local CRM identity/booking resolution. HubSpot
must also enforce a unique deal property; Calendar IDs are deterministic. The lock is deliberately
small-volume and does not coordinate external writers or make Calendar free/busy and insert atomic.

LLM and SMTP admission commit before outbound I/O and release the transaction during that I/O.
LLM attempts have independent durable rows; retries consume the same lifetime budget. SMTP has one
owner/expiry per delivery; completion checks ownership under the persistence lock. Expiry never
makes an ambiguous send eligible for automatic retry. Late results are journal evidence only.
SQLite's immediate transactions serialize fast tests; real Postgres behavior needs stack tests.

Cleanup preserves unresolved state. Retention-bounded guarantees stop after explicit purge; remote
records have their own lifecycle. This avoids claiming permanent or end-to-end exactly-once delivery.
