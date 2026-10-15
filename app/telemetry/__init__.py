"""Manual instrumentation: allowlisted metadata only, no automatic HTTP/body capture."""

import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

provider = TracerProvider(resource=Resource.create({"service.name": "lead-to-meeting"}))
endpoint = os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "")
if endpoint:
    # Explicit local endpoint only; credentials, prompts and request URLs are never exported.
    from urllib.parse import urlparse

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    parsed = urlparse(endpoint)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"localhost", "127.0.0.1", "phoenix"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("Telemetry exporter must be a local endpoint without credentials/query")
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
tracer = provider.get_tracer("lead-to-meeting", "1")
ALLOWED = {
    "run_id",
    "stage",
    "status",
    "attempt",
    "provider",
    "model",
    "prompt_version",
    "prompt_hash",
    "schema_hash",
    "elapsed_ms",
    "input_tokens",
    "output_tokens",
    "reserved_usd",
}
logger = logging.getLogger("lead-to-meeting.events")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False


def suppress_transport_logs() -> None:
    """Discard transport records before they can reach ancestor/root handlers.

    Ancestor disabled flags and logger filters do not apply to propagated child records.
    The namespace boundary also consumes records from descendants created after startup.
    Existing descendants may already own handlers, so remove those sinks as well.
    """
    namespaces = ("httpx", "httpcore")
    names = {*namespaces, *logging.Logger.manager.loggerDict}
    for name in names:
        if any(name == namespace or name.startswith(namespace + ".") for namespace in namespaces):
            transport = logging.getLogger(name)
            transport.handlers = [logging.NullHandler()]
            transport.propagate = False
            transport.disabled = True


suppress_transport_logs()


@contextmanager
def operation(name: str, **metadata: Any) -> Iterator[Any]:
    safe = {key: value for key, value in metadata.items() if key in ALLOWED and value is not None}
    with tracer.start_as_current_span(
        name, attributes=safe, record_exception=False, set_status_on_exception=False
    ) as span:
        try:
            yield span
        except Exception:
            span.set_attribute("status", "error")
            raise
        finally:
            logger.info(json.dumps({"event": name, **safe}, sort_keys=True))
