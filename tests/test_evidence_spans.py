from __future__ import annotations

import base64
import hashlib

from app.evidence import anchor_exact_evidence_refs, canonicalize_evidence_text
from app.models import (
    Claim,
    ConditionEvaluation,
    ConditionKind,
    ConditionStatus,
    EvidenceItem,
    EvidenceRef,
    ReasoningProposal,
)
from app.policy import validate_evidence_refs
from app.workflow import _adf_text, _gmail_text


def evidence(text: str, app: str = "gmail") -> EvidenceItem:
    return EvidenceItem(
        evidence_id=f"ev-{app}-1",
        app=app,
        account_ref="account",
        external_id="object-1",
        source_version="v1",
        retrieved_at="2026-09-14T00:00:00+00:00",
        identity={},
        content_text=text,
        locator={},
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
    )


def proposal(ref: EvidenceRef) -> ReasoningProposal:
    return ReasoningProposal(
        condition_evaluations=[
            ConditionEvaluation(
                condition_id="cond-1",
                kind=ConditionKind.ACCEPTANCE,
                line_item_ids=["line-1"],
                requirement="Written acceptance",
                status=ConditionStatus.UNMET,
                support_refs=[ref],
            )
        ],
        claims=[Claim(text="Acceptance is missing.", evidence_refs=[ref.model_copy()])],
        action_intents=[],
        human_dependencies=[],
        missing_evidence=[],
    )


def reference(item: EvidenceItem, quote: str, start: int | None = None, end: int | None = None) -> EvidenceRef:
    actual = item.content_text.index(quote)
    return EvidenceRef(
        evidence_id=item.evidence_id,
        field_or_part="content_text",
        start=actual if start is None else start,
        end=actual + len(quote) if end is None else end,
        quote=quote,
    )


def test_exact_valid_span_is_accepted() -> None:
    item = evidence("Prefix. Customer acceptance is pending.")
    assert validate_evidence_refs(proposal(reference(item, "Customer acceptance is pending.")), [item]) == []


def test_wrong_start_offset_is_rejected() -> None:
    item = evidence("Prefix. Customer acceptance is pending.")
    ref = reference(item, "Customer acceptance is pending.", start=0)
    assert validate_evidence_refs(proposal(ref), [item]) == [
        f"quote mismatch: {item.evidence_id}",
        f"quote mismatch: {item.evidence_id}",
    ]


def test_wrong_end_offset_is_rejected() -> None:
    item = evidence("Prefix. Customer acceptance is pending.")
    start = item.content_text.index("Customer")
    ref = reference(item, "Customer acceptance is pending.", end=start + 10)
    assert validate_evidence_refs(proposal(ref), [item]) == [
        f"quote mismatch: {item.evidence_id}",
        f"quote mismatch: {item.evidence_id}",
    ]


def test_whitespace_and_newline_normalization_has_stable_offsets() -> None:
    text = canonicalize_evidence_text("  First line\r\nSecond line\rThird line  ")
    assert text == "First line\nSecond line\nThird line"
    item = evidence(text)
    assert validate_evidence_refs(proposal(reference(item, "Second line\nThird line")), [item]) == []


def test_gmail_message_body_uses_canonical_snapshot_text() -> None:
    body = "Project CLEARDUE.\r\nAcceptance failed."
    encoded = base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")
    text = _gmail_text({"mimeType": "text/plain", "body": {"data": encoded}})
    assert text == "Project CLEARDUE.\nAcceptance failed."
    item = evidence(text)
    assert validate_evidence_refs(proposal(reference(item, "Acceptance failed.")), [item]) == []


def test_jira_description_and_comment_can_be_anchored_in_canonical_text() -> None:
    description = {"content": [{"content": [{"text": "Migration deployed."}]}]}
    comment = {"content": [{"content": [{"text": "Group mapping failed acceptance."}]}]}
    text = canonicalize_evidence_text(_adf_text(description) + "\r\n" + _adf_text(comment))
    item = evidence(text, app="jira")
    assert validate_evidence_refs(proposal(reference(item, "Group mapping failed acceptance.")), [item]) == []


def test_punctuation_is_part_of_the_exact_quote() -> None:
    item = evidence("Customer said: \"Not accepted—please retry.\"")
    assert validate_evidence_refs(proposal(reference(item, "\"Not accepted—please retry.\"")), [item]) == []


def test_genuinely_nonexistent_quote_remains_rejected_after_anchoring() -> None:
    item = evidence("Customer acceptance is pending.")
    bad = EvidenceRef(
        evidence_id=item.evidence_id,
        field_or_part="text/plain",
        start=0,
        end=17,
        quote="Customer accepted",
    )
    anchored = anchor_exact_evidence_refs(proposal(bad), [item])
    assert any("invalid evidence field" in error for error in validate_evidence_refs(anchored, [item]))


def test_unicode_is_normalized_before_persistence_and_counted_as_code_points() -> None:
    text = canonicalize_evidence_text("Cafe\u0301 — ग्राहक acceptance pending")
    assert text == "Café — ग्राहक acceptance pending"
    item = evidence(text)
    ref = reference(item, "ग्राहक acceptance pending")
    assert ref.end - ref.start == len(ref.quote)
    assert validate_evidence_refs(proposal(ref), [item]) == []


def test_unique_exact_quote_repairs_model_offsets_before_strict_validation() -> None:
    item = evidence("Header\nThe milestone is not accepted.\nFooter")
    raw = EvidenceRef(
        evidence_id=item.evidence_id,
        field_or_part="text/plain",
        start=0,
        end=7,
        quote="The milestone is not accepted.",
    )
    anchored = anchor_exact_evidence_refs(proposal(raw), [item])
    expected_start = item.content_text.index(raw.quote)
    assert anchored.condition_evaluations[0].support_refs[0].start == expected_start
    assert anchored.condition_evaluations[0].support_refs[0].end == expected_start + len(raw.quote)
    assert validate_evidence_refs(anchored, [item]) == []


def test_ambiguous_quote_with_bad_offsets_is_not_reanchored() -> None:
    item = evidence("pending / pending")
    raw = EvidenceRef(
        evidence_id=item.evidence_id,
        field_or_part="content_text",
        start=2,
        end=9,
        quote="pending",
    )
    anchored = anchor_exact_evidence_refs(proposal(raw), [item])
    assert anchored.condition_evaluations[0].support_refs[0].start == 2
    assert any("quote mismatch" in error for error in validate_evidence_refs(anchored, [item]))
