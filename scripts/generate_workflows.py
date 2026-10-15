"""Generate portable, credential-free n8n exports. Keep committed exports in sync."""

import json
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def node(name, kind, parameters, x, version=1, **extra):
    return {
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, name)),
        "name": name,
        "type": "n8n-nodes-base." + kind,
        "typeVersion": version,
        "position": [x, 300],
        "parameters": parameters,
        **extra,
    }


def http(name, path, x, body=None):
    params = {
        "method": "POST",
        "url": "=http://app:8000" + path,
        "sendHeaders": True,
        "headerParameters": {"parameters": [{"name": "X-Service-Key", "value": "={{ $env.SERVICE_KEY }}"}]},
        "options": {"timeout": 120000},
    }
    if body is not None:
        params.update(sendBody=True, specifyBody="json", jsonBody=body)
    return node(name, "httpRequest", params, x, 4.2, retryOnFail=True, maxTries=3, waitBetweenTries=1000)


def webhook(name, path):
    return node(
        name,
        "webhook",
        {
            "httpMethod": "POST",
            "path": path,
            "authentication": "headerAuth",
            "responseMode": "responseNode",
            "options": {},
        },
        0,
        2,
        webhookId=str(uuid.uuid5(uuid.NAMESPACE_URL, path)),
        credentials={"httpHeaderAuth": {"id": "leadWebhookAuth", "name": "Lead webhook token"}},
    )


def workflow(identifier, name, nodes, error=True):
    settings = {
        "executionOrder": "v1",
        "saveDataSuccessExecution": "none",
        "saveDataErrorExecution": "none",
        "saveManualExecutions": False,
        "executionTimeout": 600,
    }
    if error:
        settings["errorWorkflow"] = "leadErrors"
    return {
        "id": identifier,
        "name": name,
        "active": False,
        "nodes": nodes,
        "connections": {
            a["name"]: {"main": [[{"node": b["name"], "type": "main", "index": 0}]]}
            for a, b in zip(nodes, nodes[1:], strict=False)
        },
        "settings": settings,
        "pinData": {},
        "tags": [],
    }


def pipeline(start, email=False):
    nodes = [start]
    if email:
        code = """const m = $json;
const from = typeof m.from === 'string' ? m.from : (m.from?.text || '');
const email = (from.match(/<([^>]+)>/) || from.match(/([^\\s<>]+@[^\\s<>]+)/) || [])[1];
const requestId = m.messageId || m.metadata?.['message-id'];
if (!requestId || !email) throw new Error('Email requires Message-ID and a sender address');
return [{json: {lead: {request_id: String(requestId).replace(/[<>]/g, ''), source: 'email',
name: from.replace(/<[^>]+>/g, '').trim() || email, email, phone: '',
message: String(m.textPlain || m.text || '').trim()}, execution_id: $execution.id}}];"""
        nodes.append(node("Normalize email", "code", {"jsCode": code}, 230, 2))
        body = "={{ $json }}"
    else:
        body = "={{ {lead: $json.body, execution_id: $execution.id} }}"
    nodes.append(http("Accept and deduplicate", "/api/intake", 460, body))
    for i, (name, path) in enumerate(
        [
            ("Qualify — JSON Schema", "qualify"),
            ("Upsert contact and deal", "crm"),
            ("Find available slots", "slots"),
            ("Email slot proposal", "offer-email"),
            ("Verify CRM writes", "finish"),
        ]
    ):
        nodes.append(http(name, "/api/runs/{{ $json.run_id }}/" + path, 690 + i * 240))
    nodes.append(http("Flush warning outbox", "/api/monitor", 1890))
    if not email:
        nodes.append(
            node(
                "Return run",
                "respondToWebhook",
                {
                    "respondWith": "json",
                    "responseBody": "={{ $('Verify CRM writes').item.json }}",
                    "options": {},
                },
                2130,
                1.4,
            )
        )
    return nodes


def generate():
    entries = [
        workflow(
            "leadIntake",
            "01 · Form → qualify → HubSpot → meeting slots",
            pipeline(webhook("Form webhook", "lead")),
        ),
        workflow(
            "leadEmail",
            "02 · Incoming email → qualification pipeline",
            pipeline(
                node(
                    "IMAP inbox",
                    "emailReadImap",
                    {
                        "mailbox": "INBOX",
                        "postProcessAction": "read",
                        "downloadAttachments": False,
                        "format": "simple",
                        "options": {},
                    },
                    0,
                    2,
                    credentials={"imap": {"id": "leadImap", "name": "Dedicated inquiry inbox"}},
                ),
                email=True,
            ),
        ),
        workflow(
            "leadBooking",
            "03 · Book meeting → confirmation email",
            [
                webhook("Booking webhook", "book"),
                http("Reserve calendar event", "/api/book", 260, "={{ $json.body }}"),
                http("Email confirmation", "/api/runs/{{ $json.run_id }}/confirmation-email", 520),
                node(
                    "Return booking",
                    "respondToWebhook",
                    {
                        "respondWith": "json",
                        "responseBody": "={{ $json }}",
                        "options": {},
                    },
                    780,
                    1.4,
                ),
            ],
        ),
        workflow(
            "leadErrors",
            "04 · Execution errors → durable Telegram warning",
            [
                node("Error Trigger", "errorTrigger", {}, 0),
                http("Record and alert", "/api/errors", 260, "={{ $json }}"),
            ],
            error=False,
        ),
        workflow(
            "leadMonitor",
            "05 · Detect stalled runs, retry alerts, prune private data",
            [
                node(
                    "Every minute",
                    "scheduleTrigger",
                    {"rule": {"interval": [{"field": "minutes", "minutesInterval": 1}]}},
                    0,
                    1.2,
                ),
                http("Check runs and retry alerts", "/api/monitor", 260),
                http("Apply retention policy", "/api/maintenance", 520),
            ],
        ),
    ]
    directory = ROOT / "workflows"
    directory.mkdir(exist_ok=True)
    for entry in entries:
        (directory / (entry["id"] + ".json")).write_text(json.dumps(entry, indent=2) + "\n")


if __name__ == "__main__":
    generate()
