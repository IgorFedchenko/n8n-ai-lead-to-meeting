#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ ! -f .env ]; then
  python3 - <<'PY'
import os
import secrets
from pathlib import Path
data = Path('.env.example').read_text()
for name in ('POSTGRES_PASSWORD', 'N8N_ENCRYPTION_KEY', 'SERVICE_KEY', 'WEBHOOK_TOKEN'):
    data = data.replace(name + '=\n', name + '=' + secrets.token_hex(32) + '\n')
fd = os.open('.env', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as stream:
    stream.write(data)
PY
fi
# Bound transient registry failures without hiding a failed pull or changing image versions.
pull_attempt=1
until docker compose pull --ignore-buildable; do
  if [ "$pull_attempt" -ge 3 ]; then
    exit 1
  fi
  sleep "$((pull_attempt * 2))"
  pull_attempt="$((pull_attempt + 1))"
done
docker compose up -d --build --pull never
python3 scripts/wait_ready.py
