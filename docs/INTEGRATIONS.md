# Live integration setup and operational limits

Live integration is optional and was not exercised with real accounts. Keep `INTEGRATION_MODE=mock`
until the owner approves the accounts, credentials and any LLM API spend. Never paste credentials
into issues, workflow exports, screenshots or CI variables for the mock acceptance job.

## HubSpot

1. Create a developer test account and private app using the current [HubSpot developer documentation](https://developers.hubspot.com/).
2. Grant only `crm.objects.contacts.read`, `crm.objects.contacts.write`,
   `crm.objects.deals.read`, `crm.objects.deals.write`. Schema administration is a setup-only permission.
3. Create a deal property `portfolio_request_id`, type `string`, field type `text`, with
   `hasUniqueValue: true`, in group `dealinformation`. Configure this once using the property API
   or account tools, then remove schema write permission. If a free test account cannot create
   a unique property, stop: do not silently replace it with a nonunique search/create pattern.
4. Set `HUBSPOT_ACCESS_TOKEN`, `HUBSPOT_PIPELINE`, `HUBSPOT_DEAL_STAGE` in the ignored host `.env`.
   Select stage IDs from the account's pipeline; defaults may differ between accounts.
5. Existing contacts' phone values must use the same E.164 normalization. Multiple matches for
   either email or phone are treated as an identity conflict and stop CRM processing.

The adapter uses contact search/patch/create, a unique email fallback for ambiguous contact
creation, a unique deal property for write reconciliation, and the standard deal-to-contact
association. Currency is preserved in qualification; there is no exchange-rate conversion.
Use a single account currency for demo deal amounts.

References: [contacts](https://developers.hubspot.com/docs/api-reference/legacy/crm/objects/contacts/guide),
[deals](https://developers.hubspot.com/docs/api-reference/legacy/crm/objects/deals/guide),
[properties](https://developers.hubspot.com/docs/api-reference/crm-properties-v3/guide).

## LLM

`LLM_PROVIDER=anthropic` uses the Messages API with `output_config.format.type=json_schema`.
Set an explicit supported `LLM_MODEL` and `LLM_API_KEY` on the host after owner approval.
`LLM_PROVIDER=openai-compatible` additionally requires `LLM_BASE_URL` pointing to a trusted
provider's API root, e.g. an endpoint ending in `/v1`. It uses `response_format.json_schema`.
Provider compatibility and model/schema support must be verified before live use.

The JSON Schema is generated from `app.models.Qualification`; validation enforces strict types,
budget >= 0, score 0–100, allowed request types/currencies, and bounded text. Inquiry text cannot
select tools or alter routing. The LLM has no credentials or tool access. Schema constraints alone
do not prove semantic correctness; operators must review lead qualification quality.

Reference: [Anthropic structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs).

## Google Calendar

Create a dedicated test calendar and OAuth client. Request the smallest scopes that support
free/busy and writing events to that calendar (Calendar events and free/busy), authorize as its
owner, and put `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` and
`GOOGLE_CALENDAR_ID` in the host `.env`. No interactive OAuth flow or token storage is implemented
in this repository. The adapter exchanges the refresh token for an access token at request time.

Slots are 30 minutes, on weekdays at 10:00, 12:00 and 14:00 UTC during the next seven days.
Three free slots are offered. Events have a deterministic ID, a generic summary and no attendee
PII. Confirmation uses SMTP; automatic calendar invitations and conference links are not enabled.

References: [free/busy](https://developers.google.com/workspace/calendar/api/v3/reference/freebusy/query),
[event insertion](https://developers.google.com/workspace/calendar/api/v3/reference/events/insert).

## IMAP and SMTP

The local stack includes GreenMail. Its credentials are synthetic and only available inside the
Docker network. The acceptance script delivers an actual SMTP message and observes n8n consume it
through the Email Trigger (IMAP) node.

For live mode, create an n8n IMAP credential for a **dedicated inquiries mailbox**, bind it to
workflow `02`, and publish that workflow explicitly. Bootstrap never publishes the live email
workflow automatically. Use TLS and an operator-managed application password. The expected email
format is a text/plain inquiry, a sender email address and a stable RFC Message-ID. Attachments
are disabled. The node marks processed messages read. If the mailbox provider changes Message-ID
on redelivery, configure a trusted gateway to preserve a stable submission identifier.

Set SMTP host, port (STARTTLS, normally 587), username, password and sender on the host. The
application sends plain-text offers and confirmations. A persisted sending reservation is made
before SMTP. If an acknowledgement is lost, it records `mail_delivery_uncertain` for manual
reconciliation instead of resending automatically. Operators inspect their mail provider before
deciding whether to retry; there is no automated manual-queue resolution UI.

Reference: [n8n IMAP trigger](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.emailimap/).

## Alerts, authentication and deployment

Live warnings require an operator-created Telegram bot and `TELEGRAM_BOT_TOKEN` /
`TELEGRAM_CHAT_ID`. Alerts contain only a short run ID and an enumerated reason. They never contain
inquiry text, email addresses, phone numbers, provider errors or tokens. The local mode never calls
Telegram. Delivery failure remains in the durable outbox for the independent minute monitor.

External webhooks require `X-Webhook-Token`; adapter endpoints require `X-Service-Key`. Generate
independent random values and rotate both if exposed. n8n expressions access environment variables
to avoid exporting secrets. Only trusted n8n operators should have workflow editing privileges.
The synthetic dashboard proxy is disabled in live mode.

Before live hosting, add TLS and a reverse proxy with rate/body-size limits, proper n8n secure
cookies, restricted operator access, encrypted backups, monitoring for the stack itself, and a
provider-specific retention policy. This repository intentionally does not deploy a public service.
Never publish the local n8n editor, mock server or dedicated demo inbox.

## Failure and throughput bounds

- Provider HTTP operations attempt three times. The first two retry delays are 1 and 2 seconds,
  or a numeric Retry-After capped at 10 seconds. Requests time out after 20 seconds.
- n8n retries each adapter HTTP node three times, one second apart. Node timeout is 120 seconds;
  workflow timeout is 600 seconds. Thus an unavailable provider can receive up to nine attempts
  for one operation; size quotas against the combined budget.
- The JSON repair budget is two schema validation attempts; transient HTTP retry is separate.
- Postgres's coarse advisory lock covers remote identity/booking work, favoring correctness at
  low volume. Multiple n8n workers cannot race contact resolution within this application.
  Other HubSpot writers remain outside this lock. A unique deal property is mandatory.
- Free/busy plus event insert is not an atomic Calendar reservation. External calendar writers
  can race between those calls. An exclusive booking calendar reduces that risk.
- The journal and local idempotency records expire after seven days of inactivity. Stale replay
  after purging is outside the deduplication guarantee. Retention does not delete remote records.
- The monitor detects in-progress runs older than ten minutes. It cannot alert if the entire
  stack is offline; a hosted deployment needs an external availability monitor.
- Mock mail is idempotent by delivery key. Live SMTP uses at-most-one automatic attempt with
  manual reconciliation for ambiguity. Telegram delivery is at least once and may duplicate a
  warning after an acknowledgement loss.

## Workflow version

The stack pins n8n **2.43.6**, upstream release tag SHA
`ffbb9ec54fc02e7f8b54a0b6f41d0a84d0a817f5`, resolved when the project was created.
Workflow imports and publishing use the documented [n8n server CLI](https://docs.n8n.io/hosting/cli-commands/).
Keep exports and their generator together; CI verifies they match.
