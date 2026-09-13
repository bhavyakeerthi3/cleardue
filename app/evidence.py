from __future__ import annotations

import unicodedata

from .models import EvidenceItem, EvidenceRef, ReasoningProposal


def canonicalize_evidence_text(value: str) -> str:
    """Create the single text representation used for persistence and offsets."""
    return unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n").strip()


def _proposal_refs(proposal: ReasoningProposal) -> list[EvidenceRef]:
    refs: list[EvidenceRef] = []
    for condition in proposal.condition_evaluations:
        refs.extend(condition.support_refs)
        refs.extend(condition.conflict_refs)
    for claim in proposal.claims:
        refs.extend(claim.evidence_refs)
    if proposal.root_cause:
        refs.extend(proposal.root_cause.refs)
    return refs


def anchor_exact_evidence_refs(
    proposal: ReasoningProposal, evidence_items: list[EvidenceItem]
) -> ReasoningProposal:
    """Anchor exact, unique model quotes to immutable evidence snapshots.

    Models are not trusted to count offsets. A reference is adjusted only when its
    quote occurs verbatim exactly once in the stored snapshot. Missing or
    ambiguous quotes remain untouched so deterministic validation rejects them.
    """
    anchored = proposal.model_copy(deep=True)
    evidence = {item.evidence_id: item for item in evidence_items if not item.agent_generated}
    for ref in _proposal_refs(anchored):
        item = evidence.get(ref.evidence_id)
        if not item:
            continue
        text = item.content_text
        if 0 <= ref.start < ref.end <= len(text) and text[ref.start:ref.end] == ref.quote:
            ref.field_or_part = "content_text"
            continue
        first = text.find(ref.quote)
        if first < 0 or text.find(ref.quote, first + 1) >= 0:
            continue
        ref.start = first
        ref.end = first + len(ref.quote)
        ref.field_or_part = "content_text"
    return anchored
