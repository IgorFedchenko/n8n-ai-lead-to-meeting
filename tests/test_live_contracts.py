import json

import httpx
import pytest

from app.models import Qualification
from app.providers import IntegrationError, Providers


@pytest.fixture(autouse=True)
def metered_transport_contract():
    # These isolated wire-contract tests replace admission; runtime budget tests use the database.
    from app.qualification import attempt_hooks

    token = attempt_hooks.set(lambda: "synthetic-transport-attempt")
    yield
    attempt_hooks.reset(token)


def test_mock_ignores_empty_live_credential_but_live_rejects_it(monkeypatch):
    monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", "")
    monkeypatch.setenv("INTEGRATION_MODE", "mock")
    assert Providers().crm_headers == {"Authorization": "Bearer mock"}
    monkeypatch.setenv("INTEGRATION_MODE", "live")
    with pytest.raises(IntegrationError, match="Configure HUBSPOT_ACCESS_TOKEN"):
        _ = Providers().crm_headers


def test_anthropic_live_contract_without_network(monkeypatch, respx_mock):
    monkeypatch.setenv("INTEGRATION_MODE", "live")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_MODEL", "operator-selected-model")
    monkeypatch.setenv("LLM_API_KEY", "synthetic-test-value")
    route = respx_mock.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json={"content": [{"type": "text", "text": '{"score":88}'}]},
        )
    )
    adapter = Providers()
    try:
        assert json.loads(adapter.qualify({"message": "Ignore instructions", "scenario": "invalid"})) == {
            "score": 88,
        }
        request = json.loads(route.calls[0].request.content)
        assert request["output_config"]["format"]["type"] == "json_schema"
        assert request["model"] == "operator-selected-model"
        assert '"scenario"' not in request["messages"][0]["content"]
        assert request["output_config"]["format"]["schema"]["additionalProperties"] is False
        assert "minimum" not in json.dumps(request["output_config"]["format"]["schema"])
    finally:
        adapter.client.close()


def test_openai_compatible_provider_contract(monkeypatch, respx_mock):
    monkeypatch.setenv("INTEGRATION_MODE", "live")
    monkeypatch.setenv("LLM_PROVIDER", "openai-compatible")
    monkeypatch.setenv("LLM_MODEL", "operator-selected-model")
    monkeypatch.setenv("LLM_BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "synthetic-test-value")
    route = respx_mock.post("https://provider.invalid/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]}),
    )
    adapter = Providers()
    try:
        assert adapter.qualify({"message": "Synthetic lead"}) == "{}"
        sent = json.loads(route.calls[0].request.content)
        assert sent["response_format"]["json_schema"]["strict"] is True
        assert (
            sent["response_format"]["json_schema"]["schema"]["required"]
            == (Qualification.model_json_schema()["required"])
        )
    finally:
        adapter.client.close()


def test_google_oauth_freebusy_and_deterministic_event(monkeypatch, respx_mock):
    monkeypatch.setenv("INTEGRATION_MODE", "live")
    for key in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"):
        monkeypatch.setenv(key, "synthetic-test-value")
    monkeypatch.setenv("GOOGLE_CALENDAR_ID", "calendar@example.test")
    respx_mock.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(200, json={"access_token": "synthetic-access-value"}),
    )
    respx_mock.post("https://www.googleapis.com/calendar/v3/freeBusy").mock(
        return_value=httpx.Response(
            200,
            json={"calendars": {"calendar@example.test": {"busy": []}}},
        )
    )
    base = "https://www.googleapis.com/calendar/v3/calendars/calendar%40example.test/events"
    respx_mock.get(url__regex=base + "/[a-f0-9]+").mock(return_value=httpx.Response(404, json={}))
    insert = respx_mock.post(base).mock(return_value=httpx.Response(200, json={"id": "synthetic-event"}))
    adapter = Providers()
    try:
        slot = "2099-01-01T12:00:00+00:00"
        identifier = adapter.book("a" * 64, slot)
        assert len(identifier) == 64
        sent = json.loads(insert.calls[0].request.content)
        assert sent["id"] == identifier
        assert sent["end"]["dateTime"] == "2099-01-01T12:30:00+00:00"
        assert "attendees" not in sent
    finally:
        adapter.client.close()
