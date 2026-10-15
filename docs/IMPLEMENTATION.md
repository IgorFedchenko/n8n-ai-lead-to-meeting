# Acceptance plan

The terminal artifact is a passing `artifacts/acceptance.json` from actual n8n webhooks,
Postgres, local HTTP provider doubles and an actual IMAP message, plus green GitHub Actions.
All test data is synthetic. No live SaaS accounts or paid LLM calls are used.

| Requirement | Observable acceptance |
|---|---|
| Form | Authenticated POST reaches the n8n form workflow and creates a journal entry |
| Email | SMTP delivery to a dedicated local inbox is consumed by n8n's IMAP trigger |
| Qualification | Schema-valid budget, currency, timeline, request type, score and reason |
| Invalid output | One repair attempt, then durable manual review and a warning |
| CRM | Email/normalized phone identify contact; stable submission key identifies deal |
| Redelivery | Concurrent delivery creates one contact/deal and one meeting |
| Slots | Calendar free/busy filters proposed 30-minute UTC slots |
| Booking | Offered slot and secret token are checked; calendar event and confirmation recorded |
| Retry | HTTP 429/5xx/transport failure uses bounded backoff; provider IDs reconcile writes |
| Errors | Separate n8n Error Trigger records and delivers an alert; no provider bodies in alerts |
| Silent failure | Zero CRM writes and stalled runs produce durable warnings |
| Operations | One-command Compose launch, retention, credentials excluded from Git |
| Delivery | Private MIT repository, CI acceptance, screenshots and portfolio copy |

Implementation order: durable state and provider contracts → n8n exports → local adapters/inbox →
negative controls and acceptance → documentation and screenshots → owner publication review.

The workflow owns the visible orchestration. A small Python boundary owns the transactions and
provider reconciliation which cannot safely be represented as isolated search/create nodes.
This keeps CRM deduplication correct across concurrent n8n executions and process restarts.
