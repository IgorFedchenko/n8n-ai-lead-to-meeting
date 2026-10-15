"""Acceptance against actual n8n webhooks + Postgres + HTTP doubles + IMAP."""

import concurrent.futures
import json
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
env = dict(
    line.split("=", 1)
    for line in (ROOT / ".env").read_text().splitlines()
    if line and not line.startswith("#")
)
APP = "http://localhost:" + env.get("DEMO_PORT", "8088")
N8N = "http://localhost:" + env.get("N8N_PORT", "5679")
PREFIX = "accept-" + uuid.uuid4().hex[:10]


def call(base, path, data=None, key="service"):
    headers = {"Content-Type": "application/json"}
    if key == "service":
        headers["X-Service-Key"] = env["SERVICE_KEY"]
    elif key == "webhook":
        headers["X-Webhook-Token"] = env["WEBHOOK_TOKEN"]
    request = urllib.request.Request(
        base + path, json.dumps(data).encode() if data is not None else None, headers=headers
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.load(response)


def expect_error(fn, status=None):
    try:
        fn()
    except urllib.error.HTTPError as error:
        assert status is None or error.code == status, error.code
    else:
        raise AssertionError("Negative control unexpectedly succeeded")


def main():
    expect_error(lambda: call(N8N, "/webhook/lead", {}, key="none"), 403)
    fixtures = json.loads((ROOT / "fixtures/leads.json").read_text())
    runs = []
    for index, fixture in enumerate(fixtures):
        fixture["request_id"] = PREFIX + "-" + str(index)
        fixture["email"] = PREFIX + "-" + str(index) + "@example.test"
        if fixture.get("phone"):
            fixture["phone"] = "+1202" + str(int(uuid.uuid4().hex[:8], 16)).zfill(10)[:10]
        runs.append(call(N8N, "/webhook/lead", fixture, key="webhook"))
    assert [r["status"] for r in runs] == ["offered", "offered", "manual_review", "offered"]
    assert runs[1]["llm_attempts"] == 2
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        duplicates = list(
            pool.map(lambda _: call(N8N, "/webhook/lead", fixtures[0], key="webhook"), range(3))
        )
    assert all(r["deal_id"] == runs[0]["deal_id"] for r in duplicates)
    expect_error(
        lambda: call(APP, "/api/intake", {"lead": {**fixtures[0], "message": "Changed request body."}}), 409
    )
    booking = {"run_id": runs[0]["run_id"], "token": runs[0]["booking_token"], "slot": runs[0]["slots"][0]}
    expect_error(lambda: call(APP, "/api/book", {**booking, "token": "invalid" * 5}), 403)
    for _ in range(2):
        booked = call(N8N, "/webhook/book", booking, key="webhook")
        assert booked["status"] == "completed"
    by_phone = {**fixtures[0], "request_id": PREFIX + "-phone", "email": PREFIX + "-new@example.test"}
    assert call(N8N, "/webhook/lead", by_phone, key="webhook")["contact_id"] == runs[0]["contact_id"]
    # Deliver a real RFC 5322 email into GreenMail; n8n consumes it using its IMAP trigger.
    email_id = PREFIX + "-email@example.test"
    code = "\n".join(
        [
            "import smtplib",
            "from email.message import EmailMessage",
            "m=EmailMessage()",
            f"m['From']='Email Sample <{PREFIX}-inbox@example.test>'",
            "m['To']='demo@example.test'",
            f"m['Message-ID']='<{email_id}>'",
            "m['Subject']='Synthetic discovery inquiry'",
            "m.set_content('Lead routing and HubSpot automation. Budget USD 5000 within four weeks.')",
            "s=smtplib.SMTP('inbox',3025,timeout=10)",
            "s.send_message(m)",
            "s.quit()",
        ]
    )
    subprocess.run(["docker", "compose", "exec", "-T", "app", "python", "-c", code], cwd=ROOT, check=True)
    for _ in range(45):
        state = call(APP, "/api/state")
        email_runs = [r for r in state["runs"] if r["lead"]["request_id"] == email_id]
        if email_runs and email_runs[0]["status"] == "offered":
            break
        time.sleep(1)
    else:
        raise AssertionError("Actual IMAP-triggered inquiry did not reach offered status")
    failure = {
        **fixtures[0],
        "request_id": PREFIX + "-crm-fail",
        "email": PREFIX + "-fail@example.test",
        "phone": "",
        "scenario": "crm_fail",
    }
    expect_error(lambda: call(N8N, "/webhook/lead", failure, key="webhook"))
    for _ in range(20):
        call(APP, "/api/monitor", {})
        state = call(APP, "/api/state")
        failed = [r for r in state["runs"] if r["lead"]["request_id"] == failure["request_id"]]
        if failed and any(
            a["run_id"] == failed[0]["id"] and a["reason"] == "n8n_execution_failed" and a["delivered"]
            for a in state["alerts"]
        ):
            break
        time.sleep(1)
    else:
        raise AssertionError("Separate n8n Error Trigger workflow did not deliver a warning")
    mock = call(APP, "/demo/mock")
    ids = {r["run_id"] for r in runs}
    assert len([d for d in mock["deals"] if d["properties"]["portfolio_request_id"] in ids]) == 3
    assert len([e for e in mock["events"] if e["id"] == booked["event_id"]]) == 1
    assert len([m for m in mock["mail"] if m["key"] == "confirmation:" + runs[0]["run_id"]]) == 1
    assert any(
        a["run_id"] == runs[2]["run_id"] and a["reason"] == "zero_crm_writes" and a["delivered"]
        for a in state["alerts"]
    )
    summary = {
        "result": "PASS",
        "batch": PREFIX,
        "checks": [
            "authenticated n8n webhooks",
            "structured LLM validation and repair",
            "429 retry",
            "manual review and zero-write warning",
            "concurrent redelivery without duplicate CRM records",
            "email + phone identity upsert",
            "Calendar booking replay and confirmation email",
            "actual IMAP email trigger",
            "separate n8n Error Trigger and warning delivery",
        ],
    }
    (ROOT / "artifacts").mkdir(exist_ok=True)
    (ROOT / "artifacts/acceptance.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
