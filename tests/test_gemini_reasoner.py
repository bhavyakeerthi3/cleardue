from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

import app.reasoning as reasoning
from app.config import ROOT
from app.models import ReasoningProposal
from app.reasoning import (
    GEMINI_MAX_OUTPUT_TOKENS,
    FixtureReasoner,
    GeminiReasoner,
    RecoverableReasoningError,
    gemini_compatible_schema,
)
from app.workflow import WorkflowEngine


class FakeUsage:
    def model_dump(self, **_kwargs):
        return {
            "prompt_token_count": 120,
            "candidates_token_count": 80,
            "total_token_count": 200,
        }


class FakeResponse:
    def __init__(self, proposal, model_version="gemini-3.6-flash-actual", finish_reason="STOP"):
        self.text = proposal.model_dump_json() if isinstance(proposal, ReasoningProposal) else proposal
        self.model_version = model_version
        self.usage_metadata = FakeUsage()
        self.candidates = [type("Candidate", (), {"finish_reason": finish_reason})()]


class FakeModels:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FakeClient:
    def __init__(self, response):
        self.models = FakeModels(response)


def fixture_bundle_and_proposal():
    fixture = json.loads((ROOT / "evals" / "fixtures" / "main_case.json").read_text(encoding="utf-8"))
    bundle = {"evidence": fixture["evidence"]}
    proposal, _ = FixtureReasoner().reason(bundle)
    return bundle, proposal


def test_gemini_client_uses_api_key(monkeypatch):
    captured = {}

    def client_factory(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(reasoning.genai, "Client", client_factory)
    GeminiReasoner("test-key", "gemini-3.6-flash")
    assert captured == {"api_key": "test-key"}


def test_gemini_request_uses_structured_output_without_tools():
    bundle, proposal = fixture_bundle_and_proposal()
    client = FakeClient(FakeResponse(proposal))
    reasoner = GeminiReasoner("test-key", "gemini-3.6-flash", client=client)

    parsed, _ = reasoner.reason(bundle)

    call = client.models.calls[0]
    config = call["config"]
    assert parsed == proposal
    assert call["model"] == "gemini-3.6-flash"
    assert json.loads(call["contents"]) == bundle
    assert config.response_mime_type == "application/json"
    assert config.response_schema is None
    assert config.response_json_schema == gemini_compatible_schema()
    assert config.tools is None
    assert config.max_output_tokens == GEMINI_MAX_OUTPUT_TOKENS
    assert config.thinking_config.thinking_level.value == "LOW"


def test_gemini_schema_removes_every_additional_properties_keyword():
    original = ReasoningProposal.model_json_schema()
    compatible = gemini_compatible_schema()

    def occurrences(value):
        if isinstance(value, dict):
            return int("additionalProperties" in value) + sum(
                occurrences(child) for child in value.values()
            )
        if isinstance(value, list):
            return sum(occurrences(child) for child in value)
        return 0

    assert occurrences(original) == 7
    assert occurrences(compatible) == 0


def test_gemini_json_is_still_validated_by_reasoning_proposal():
    bundle, proposal = fixture_bundle_and_proposal()
    response = FakeResponse(proposal)
    parsed, _ = GeminiReasoner(
        "test-key", "gemini-3.6-flash", client=FakeClient(response)
    ).reason(bundle)
    assert ReasoningProposal.model_validate_json(response.text) == parsed


def test_gemini_malformed_structured_output_is_rejected():
    client = FakeClient(FakeResponse('{"condition_evaluations":[]}'))
    reasoner = GeminiReasoner("test-key", "gemini-3.6-flash", client=client)
    with pytest.raises(RecoverableReasoningError, match="malformed"):
        reasoner.reason({"evidence": []})


def test_gemini_truncated_json_is_rejected_with_response_metadata():
    client = FakeClient(FakeResponse('{"condition_evaluations":[', finish_reason="MAX_TOKENS"))
    reasoner = GeminiReasoner("test-key", "gemini-3.6-flash", client=client)
    with pytest.raises(RecoverableReasoningError, match="truncated") as caught:
        reasoner.reason({"evidence": []})
    assert caught.value.metadata["finish_reason"] == "MAX_TOKENS"
    assert caught.value.metadata["response_text_length"] > 0
    assert caught.value.metadata["max_output_tokens"] == GEMINI_MAX_OUTPUT_TOKENS


def test_failed_gemini_reasoning_creates_no_assessment_or_actions(db, settings, tmp_path):
    engine = WorkflowEngine(db, settings)
    engine.fixture.state_path = tmp_path / "provider.json"
    engine.fixture.reset()
    engine.reasoner = GeminiReasoner(
        "test-key",
        "gemini-3.6-flash",
        client=FakeClient(FakeResponse('{"condition_evaluations":[', finish_reason="MAX_TOKENS")),
    )

    with pytest.raises(RecoverableReasoningError):
        engine.investigate(settings.demo_case_id)

    assert db.latest_assessment(settings.demo_case_id) is None
    assert db.list_actions(settings.demo_case_id) == []


def test_gemini_key_is_required():
    with pytest.raises(ValueError, match="GEMINI_API_KEY is required in live mode"):
        GeminiReasoner("", "gemini-3.6-flash")


def test_actual_gemini_model_and_usage_are_recorded_in_assessment(db, settings, tmp_path):
    engine = WorkflowEngine(db, settings)
    engine.fixture.state_path = tmp_path / "provider.json"
    engine.fixture.reset()
    _, proposal = fixture_bundle_and_proposal()
    engine.reasoner = GeminiReasoner(
        "test-key",
        "gemini-3.6-flash",
        client=FakeClient(FakeResponse(proposal)),
    )

    engine.investigate(settings.demo_case_id)

    assessment = db.latest_assessment(settings.demo_case_id)
    assert assessment["model_id"] == "gemini-3.6-flash-actual"
    assert assessment["usage"]["reasoning_provider"] == "gemini"
    assert assessment["usage"]["requested_model_id"] == "gemini-3.6-flash"
    assert assessment["usage"]["model_id"] == "gemini-3.6-flash-actual"
