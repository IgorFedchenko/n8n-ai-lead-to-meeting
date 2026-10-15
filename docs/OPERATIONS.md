# Durable state and reconciliation

Business progression is received → qualified → crm_written → slots_ready → offered → booked → completed.
A failed integration stage can be retried through the same authenticated stage endpoint; stable remote
IDs reconcile partial writes. Manual review stops downstream work. n8n execution failures are recorded
separately: a late notification cannot regress later successful business progress.

LLM qualification has a five-minute owner lease. A concurrent caller gets `operation_status=in_progress`.
An expired/crashed operation becomes `qualification_interrupted` manual review: inspect the retained
attempt ledger before making any operator decision. Each physical request consumes a committed
reservation; a missing outcome is ambiguous and remains charged. Never delete/reset budgets to retry
an unresolved request. The read-only private state endpoint shows per-attempt accounting.

Mail stores a reservation ID and expiry before SMTP. Active duplicate callers neither send nor
mark uncertainty. Expiry/failure transitions to uncertain/manual review, without automatic resend.
The monitor also expires old mail reservations. Late successes/failures append event evidence and
cannot overwrite an expired/reconciled reservation. Ordinary successful send completion requires
matching persisted ownership and an unexpired reservation.

For an uncertain delivery, check the mail provider's logs and recipient delivery evidence using
the deterministic Message-ID (`sha256(kind:run_id)@lead-demo.invalid`). After verifying delivery,
run this **locally as the operator**, using the application's database environment:

```sh
python -m scripts.reconcile_mail RUN_ID offer --verified-delivered
# Use confirmation for a confirmation message.
```

This command does not send mail. It atomically replaces sender ownership, marks verified delivery,
restores only the corresponding mail progression, and adds an audit event/warning. If delivery
cannot be verified, leave it unresolved and arrange human follow-up outside the automation; no
"mark unsent and retry" command is provided. Do not directly edit a pending reservation to allow resends.

The private `/api/state` endpoint and demo journal provide the read-only manual queue. Qualification
text/contact data are private operational data; the endpoint needs the service key. There is no
public editing/admin UI. Other manual-review reasons require investigation and explicit operator
resolution, never an automatic retry that can bypass budgets or delivery ambiguity.

Cleanup defaults to seven days of inactivity **only for safe offered/completed runs**. Active
operations, uncertain/manual/failed state and undelivered alerts remain until resolved. This retention
exception can keep PII beyond seven days; monitor and resolve the queue under your own retention
policy. Purge removes local identity, journal, alerts and attempt accounting together. Replays after
purge no longer have local dedup/budget guarantees. Remote CRM/calendar records and backups require
separate deletion policies. Do not purge unresolved records as a recovery shortcut.
