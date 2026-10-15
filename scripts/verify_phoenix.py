"""Explicit local OTLP export + Phoenix ingestion smoke. Run only with local Phoenix started."""

import json
import secrets
import time
from pathlib import Path

import httpx
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor


def main():
    with httpx.Client(base_url="http://localhost:6006", timeout=2) as client:
        for _ in range(45):
            try:
                if client.get("/v1/projects").status_code == 200:
                    break
            except httpx.TransportError:
                time.sleep(1)
        else:
            raise RuntimeError("Local Phoenix did not become ready")
    provider = TracerProvider()
    provider.add_span_processor(
        SimpleSpanProcessor(OTLPSpanExporter(endpoint="http://localhost:6006/v1/traces"))
    )
    name = "synthetic-smoke-" + secrets.token_hex(6)
    with provider.get_tracer("local-smoke").start_as_current_span(name):
        pass
    provider.force_flush()
    found = False
    with httpx.Client(base_url="http://localhost:6006", timeout=5) as client:
        for _ in range(15):
            response = client.get("/v1/projects/default/spans", params={"name": name, "limit": 10})
            if response.status_code == 200 and name in response.text:
                found = True
                break
            time.sleep(1)
    provider.shutdown()
    report = {
        "status": "PASS" if found else "FAIL",
        "boundary": "local OTLP HTTP to Phoenix ingestion",
        "synthetic": True,
    }
    Path("artifacts").mkdir(exist_ok=True)
    Path("artifacts/phoenix.json").write_text(json.dumps(report) + "\n")
    print(json.dumps(report))
    raise SystemExit(0 if found else 1)


if __name__ == "__main__":
    main()
