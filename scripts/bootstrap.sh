#!/bin/sh
set -eu
umask 077
credential_file=$(mktemp)
trap 'rm -f "$credential_file"' EXIT
export CREDENTIAL_FILE="$credential_file"
node - <<'JS'
const fs = require('fs');
const credentials = [{id: 'leadWebhookAuth', name: 'Lead webhook token', type: 'httpHeaderAuth',
  data: {name: 'X-Webhook-Token', value: process.env.WEBHOOK_TOKEN}}];
if (process.env.INTEGRATION_MODE === 'mock') credentials.push({id: 'leadImap',
  name: 'Dedicated inquiry inbox', type: 'imap', data: {
    user: 'demo', password: 'demo', host: 'inbox', port: 3143, secure: false, allowUnauthorizedCerts: false}});
fs.writeFileSync(process.env.CREDENTIAL_FILE, JSON.stringify(credentials), {mode: 0o600});
JS
n8n import:credentials --input="$credential_file"
n8n import:workflow --separate --input=/workflows
for workflow in leadErrors leadIntake leadBooking leadMonitor; do
  n8n publish:workflow --id="$workflow"
done
if [ "$INTEGRATION_MODE" = mock ]; then
  n8n publish:workflow --id=leadEmail
fi
