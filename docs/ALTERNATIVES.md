# Make and Zapier portability

The executable, verified implementation is n8n. No Make/Zapier account was created and no paid
plan was used. A second hosted scenario is not included in the acceptance claim.

| Concern | n8n reference | Make mapping | Zapier mapping |
|---|---|---|---|
| Intake | Webhook + IMAP trigger | Custom webhook + email module | Webhook/email trigger |
| Orchestration | Separate visible HTTP steps | HTTP modules + router | Multi-step Zap + paths |
| State | Postgres adapter boundary | Reuse the adapter endpoints | Reuse the adapter endpoints |
| Repair | Two attempts + manual review | Router/error handler | Paths/error handling, plan dependent |
| Failure alerts | Error Trigger + scheduled outbox | Error handler + scheduled scenario | Error handler + scheduled Zap |
| Booking | Authenticated second webhook | Separate webhook scenario | Separate booking Zap |

The same transactional adapter endpoints can be reused in either platform, but a hosted service
needs a securely reachable adapter URL. A local-only Compose stack cannot be reached by hosted
Make/Zapier without additional hosting or a tunnel. This reference does not publish a tunnel.

Free-tier operation/credit limits, polling intervals, multi-step support and premium app access
change over time. Verify the current [Make plans](https://www.make.com/en/pricing) and
[Zapier plans](https://zapier.com/pricing) before implementing the full scenario. This project
does not claim a fully equivalent hosted scenario can run within either current free plan.

n8n was chosen because the complete reproducible orchestration, IMAP test and database can run
locally with no account, paid API key or ongoing hosted automation subscription.
