"""Print state metadata only; never print inquiry bodies, credentials or booking tokens."""

import json
import urllib.request
from pathlib import Path

env = dict(
    line.split("=", 1) for line in Path(".env").read_text().splitlines() if line and not line.startswith("#")
)
request = urllib.request.Request(
    "http://localhost:" + env.get("DEMO_PORT", "8088") + "/api/state",
    headers={"X-Service-Key": env["SERVICE_KEY"]},
)
try:
    with urllib.request.urlopen(request, timeout=10) as response:
        state = json.load(response)
    print(
        json.dumps(
            {
                "runs": [
                    {
                        "id": r["id"][:12],
                        "status": r["status"],
                        "done": r["done"],
                        "failed_step": r.get("failed_step"),
                        "error": r.get("error"),
                    }
                    for r in state["runs"]
                ],
                "journal": [
                    {"event": j["event"], "run_id": j["run_id"][:12]} for j in state["journal"][-20:]
                ],
                "alerts": [{"reason": a["reason"], "delivered": a["delivered"]} for a in state["alerts"]],
            },
            indent=2,
        )
    )
except OSError:
    print("State endpoint unavailable; inspect container startup diagnostics")
