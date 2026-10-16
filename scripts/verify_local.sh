#!/bin/sh
# Each heavy boundary takes a separate ticket when the host queue is installed.
set -eu
cd "$(dirname "$0")/.."
PATH="$PWD/.venv/bin:$PATH"
export PATH
heavy() {
  if command -v studio-heavy-run >/dev/null 2>&1; then
    studio-heavy-run -- "$@"
  else
    "$@"
  fi
}
[ ! -e .env ] || { echo 'Use a clean checkout without .env for synthetic verification.' >&2; exit 1; }
python - <<'PY'
import socket
for port in (8088, 5679, 6006):
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', port))
PY
export COMPOSE_PROJECT_NAME="lead-verify-$(date +%s)-$$"
mkdir -p artifacts
image_backup=$(mktemp -d)
cp docs/images/demo.png docs/images/booked.png "$image_backup/"
cleanup() {
  status=$?
  trap - EXIT
  heavy docker compose --profile observability down -v > artifacts/cleanup.log 2>&1 || status=1
  cp "$image_backup/demo.png" "$image_backup/booked.png" docs/images/
  rm -rf "$image_backup"
  rm -f .env
  exit "$status"
}
trap cleanup EXIT
ruff check app tests scripts evals
ruff format --check app tests scripts evals
mypy app evals
bandit -r app evals -q
python scripts/audit_dependencies.py
python scripts/scan_secrets.py
pre-commit run --all-files
pytest -q -m 'not postgres' --junitxml=artifacts/tests.xml
python -m evals.run --offline --output artifacts/evals.json
python scripts/generate_workflows.py
git diff --exit-code workflows
heavy ./scripts/demo.sh
# A disposable test database shares this driver's uniquely owned Postgres instance.
heavy docker compose exec -T postgres createdb -U demo contracts
# Execute with installed container dependencies and the same exact mounted test source.
heavy docker compose run --rm --no-deps --user root -v "$PWD:/srv" app sh -c \
  'export TEST_POSTGRES_URL="${DATABASE_URL%/n8n}/contracts"; pip install --no-cache-dir -r requirements-dev.lock >/dev/null; pytest -q -m postgres --junitxml=artifacts/postgres.xml'
heavy python scripts/capture_demo.py
heavy python scripts/verify_demo.py
heavy docker compose --profile observability up -d phoenix
heavy python scripts/verify_phoenix.py
