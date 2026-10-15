import os
import time
import urllib.error
import urllib.request
from pathlib import Path

env = dict(
    line.split("=", 1) for line in Path(".env").read_text().splitlines() if line and not line.startswith("#")
)
port = os.getenv("N8N_PORT", env.get("N8N_PORT", "5679"))
for _attempt in range(120):
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/healthz/readiness", timeout=2) as response:
            if response.status == 200:
                # Health alone precedes trigger registration on some n8n releases.
                # An unauthenticated request must reach the registered credential guard.
                request = urllib.request.Request(
                    f"http://localhost:{port}/webhook/lead",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                )
                try:
                    urllib.request.urlopen(request, timeout=2)
                except urllib.error.HTTPError as error:
                    if error.code != 403:
                        time.sleep(2)
                        continue
                else:
                    raise SystemExit("Webhook authentication guard did not reject missing credentials")
                print(f"Demo ready: http://localhost:{env.get('DEMO_PORT', '8088')}")
                print(f"n8n editor: http://localhost:{port} (create a local owner account)")
                break
    except (OSError, urllib.error.URLError):
        time.sleep(2)
else:
    raise SystemExit("n8n did not become ready. Inspect: docker compose logs --tail=30 bootstrap n8n")
