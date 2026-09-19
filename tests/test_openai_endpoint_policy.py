from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_coverage_service_edges import _build_comparison, _service
from test_decision_providers import _FakeClient

from ai_work_harness.decision.openai_provider import OpenAIProvider, OpenAIProviderError
from ai_work_harness.errors import HarnessError


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://recipient.invalid/v1",
        "http://api.openai.com/v1",
        "https://api.openai.com.evil.invalid/v1",
        "https://api.openai.com/v1?recipient=other",
        "https://credential@api.openai.com/v1",
        "",
    ],
)
def test_unsupported_environment_endpoint_never_calls_a_client(
    monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", endpoint)
    client = _FakeClient([])
    client.base_url = "https://api.openai.com/v1/"
    with pytest.raises(OpenAIProviderError) as caught:
        OpenAIProvider(client=client)._request({})
    assert caught.value.code == "UNSUPPORTED_OPENAI_ENDPOINT"
    assert caught.value.retryable is False
    assert client.responses.requests == []
    assert endpoint not in str(caught.value.details) or endpoint == ""


@pytest.mark.parametrize("factory", [False, True])
def test_injected_client_endpoint_is_validated_before_a_request(
    monkeypatch: pytest.MonkeyPatch, factory: bool
) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    client = _FakeClient([])
    client.base_url = "https://recipient.invalid/v1"
    provider = (
        OpenAIProvider(client_factory=lambda: client) if factory else OpenAIProvider(client=client)
    )
    with pytest.raises(OpenAIProviderError) as caught:
        provider._request({})
    assert caught.value.code == "UNSUPPORTED_OPENAI_ENDPOINT"
    assert client.responses.requests == []


def test_sdk_client_is_constructed_with_an_explicit_official_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    openai = pytest.importorskip("openai")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    calls: list[dict[str, Any]] = []
    client = _FakeClient([])
    client.base_url = "https://api.openai.com/v1/"

    def create(**kwargs: Any) -> _FakeClient:
        calls.append(kwargs)
        return client

    monkeypatch.setattr(openai, "OpenAI", create)
    assert OpenAIProvider()._get_client() is client
    assert calls[0]["base_url"] == "https://api.openai.com/v1"
    assert client.responses.requests == []


@pytest.mark.parametrize("endpoint", ["https://api.openai.com/v1", "https://api.openai.com/v1/"])
def test_official_endpoint_and_cached_client_are_checked_on_every_request(
    monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", endpoint)
    client = _FakeClient([{"id": "synthetic-response"}])
    client.base_url = endpoint
    provider = OpenAIProvider(client=client)
    assert provider._request({})["id"] == "synthetic-response"
    client.base_url = "https://recipient.invalid/v1"
    with pytest.raises(OpenAIProviderError) as caught:
        provider._request({})
    assert caught.value.code == "UNSUPPORTED_OPENAI_ENDPOINT"
    assert len(client.responses.requests) == 1


def test_endpoint_is_rechecked_before_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    client = _FakeClient([ConnectionError("synthetic transient failure")])
    client.base_url = "https://api.openai.com/v1/"

    def change_endpoint(_delay: float) -> None:
        monkeypatch.setenv("OPENAI_BASE_URL", "https://recipient.invalid/v1")

    with pytest.raises(OpenAIProviderError) as caught:
        OpenAIProvider(client=client, sleeper=change_endpoint)._request({})
    assert caught.value.code == "UNSUPPORTED_OPENAI_ENDPOINT"
    assert len(client.responses.requests) == 1


def test_client_without_an_inspectable_endpoint_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    with pytest.raises(OpenAIProviderError) as caught:
        OpenAIProvider(client=object())._get_client()
    assert caught.value.code == "UNSUPPORTED_OPENAI_ENDPOINT"


def test_endpoint_drift_after_preview_and_consent_never_sends_or_changes_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    service, _clock = _service(tmp_path)
    parent = _build_comparison(service, tmp_path)
    preview = service.preview_agent(operation="evaluations")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://recipient.invalid/v1")
    with pytest.raises(HarnessError) as preview_error:
        service.preview_agent(operation="evaluations")
    assert preview_error.value.code == "UNSUPPORTED_OPENAI_ENDPOINT"
    with pytest.raises(HarnessError) as consent_error:
        service.consent_agent(
            operation="evaluations",
            expected_manifest_sha=preview["outbound_manifest_sha256"],
            expected_parent=parent,
        )
    assert consent_error.value.code == "UNSUPPORTED_OPENAI_ENDPOINT"
    assert service.store.get_current().sha256 == parent

    monkeypatch.delenv("OPENAI_BASE_URL")
    consent = service.consent_agent(
        operation="evaluations",
        expected_manifest_sha=preview["outbound_manifest_sha256"],
        expected_parent=parent,
    )
    before = service.store.paths.current_pointer.read_bytes()
    client = _FakeClient([])
    client.base_url = "https://api.openai.com/v1/"
    monkeypatch.setenv("OPENAI_BASE_URL", "https://recipient.invalid/v1")
    with pytest.raises(HarnessError) as run_error:
        service.run_openai_agent(
            operation="evaluations",
            expected_parent=consent["snapshot_sha256"],
            provider=OpenAIProvider(client=client),
        )
    assert run_error.value.code == "UNSUPPORTED_OPENAI_ENDPOINT"
    assert client.responses.requests == []
    assert service.store.paths.current_pointer.read_bytes() == before
    # Reading existing records must remain independent of outbound configuration.
    assert service.verify()["verified"] is True
