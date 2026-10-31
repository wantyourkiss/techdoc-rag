"""TechDoc RAG S3-A: fail-closed provenance/citation gate.

This module does NOT establish semantic entailment, correctness of a paraphrase,
completeness of an answer, or permission to access externally sourced documents.
It only verifies citations against a trusted, caller-supplied block catalog and
returns source extracts; generated claims must undergo independent semantic review.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
import re
import unicodedata
from typing import Any


def normalize(text: str) -> str:
    return ' '.join(unicodedata.normalize('NFKC', str(text)).split()).casefold()


@dataclass(frozen=True)
class Request:
    question: str
    target_snapshot: str | None
    permitted_snapshots: frozenset[str]
    required_quote_groups: tuple[tuple[str, ...], ...] = ()
    max_excerpt_chars: int = 1500
    # Trusted caller may explicitly request citations across several DIFFERENT documents.
    # Never infer this from an unscoped query or untrusted retrieval results.
    target_snapshots: frozenset[str] | None = None
    # None preserves the legacy all-selected-required contract. An explicit empty
    # set means ordinary knowledge search: any authorized source may support it.
    required_snapshots: frozenset[str] | None = None
    previous_user_question: str | None = None

    @property
    def coverage_targets(self):
        return self.target_snapshots if self.required_snapshots is None else self.required_snapshots


@dataclass(frozen=True)
class Candidate:
    evidence_id: str
    snapshot_id: str
    page: int | None
    required_quotes: tuple[str, ...]


@dataclass
class GateResult:
    status: str
    reason: str
    extracts: list[dict[str, Any]] = field(default_factory=list)
    draft: str = ''
    independently_adjudicated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class EvidenceGate:
    def __init__(self, block_catalog: list[dict[str, Any]]):
        self.blocks: dict[str, dict[str, Any]] = {}
        self.duplicate_block_ids: set[str] = set()
        for b in block_catalog:
            bid = b.get('block_id')
            if not isinstance(bid, str) or not bid:
                raise ValueError('block_id missing')
            if bid in self.blocks:
                self.duplicate_block_ids.add(bid)
            else:
                self.blocks[bid] = b
        if self.duplicate_block_ids:
            raise ValueError('duplicate block_id: ' + sorted(self.duplicate_block_ids)[0])

    def evaluate(self, request: Request, candidates: list[Candidate]) -> GateResult:
        if not request.permitted_snapshots:
            return GateResult('ABSTAIN_ACCESS', 'No authorized document scope')
        if not isinstance(request.max_excerpt_chars, int) or request.max_excerpt_chars <= 0:
            return GateResult('ABSTAIN_BAD_REQUEST', 'Invalid evidence budget')
        if not candidates:
            return GateResult('ABSTAIN_NO_EVIDENCE', 'No retrieved evidence')
        explicit = request.target_snapshots
        if request.required_snapshots is not None:
            if explicit is None or not request.required_snapshots<=explicit or len(request.required_snapshots)>4:
                return GateResult('ABSTAIN_BAD_REQUEST','Invalid knowledge coverage targets')
        if explicit is not None:
            if request.target_snapshot is not None or not explicit:
                return GateResult('ABSTAIN_BAD_REQUEST', 'Ambiguous document scope request')
            if not explicit <= request.permitted_snapshots:
                return GateResult('ABSTAIN_ACCESS', 'Requested document set is not authorized')
            # Reject, never silently discard an out-of-scope citation from a retrieval backend.
            if any(c.snapshot_id not in explicit for c in candidates):
                return GateResult('ABSTAIN_VERSION_SCOPE', 'Candidate outside explicit source scope')
        elif request.target_snapshot is None:
            versions = {c.snapshot_id for c in candidates}
            if len(versions) > 1:
                return GateResult('ABSTAIN_VERSION_AMBIGUOUS', 'Multiple document snapshots; explicit version required')
        elif request.target_snapshot not in request.permitted_snapshots:
            return GateResult('ABSTAIN_ACCESS', 'Requested version not authorized')

        accepted: list[dict[str, Any]] = []
        rejected: list[str] = []
        scoped = [c for c in candidates if (explicit is not None or request.target_snapshot is None or c.snapshot_id == request.target_snapshot)]
        if not scoped:
            return GateResult('ABSTAIN_VERSION_SCOPE', 'No evidence from requested snapshot')
        for c in scoped:
            if c.snapshot_id not in request.permitted_snapshots:
                rejected.append('ACCESS')
                continue
            b = self.blocks.get(c.evidence_id)
            if not b or c.snapshot_id != b.get('snapshot_id') or c.page != b.get('locator', {}).get('page'):
                rejected.append('ANCHOR_MISMATCH')
                continue
            if not b.get('indexable', False) or b.get('kind') in ('table_unverified', 'unparsed'):
                rejected.append('LOW_QUALITY')
                continue
            src_text = b.get('text') or ''
            if not src_text:
                rejected.append('EMPTY_SOURCE')
                continue
            if not c.required_quotes or any(not q or normalize(q) not in normalize(src_text) for q in c.required_quotes):
                rejected.append('QUOTE_NOT_IN_SOURCE')
                continue
            # Do not truncate a quote silently. If the whole source block is too long,
            # return only independently matched source excerpt strings.
            if sum(len(q) for q in c.required_quotes) > request.max_excerpt_chars:
                rejected.append('BUDGET')
                continue
            # We have validated match against the source block, but this is still
            # NOT semantic entailment. Metadata `verified` is parser-side only.
            accepted.append({
                'evidence_key': f'E{len(accepted)+1}',
                'snapshot_id': b['snapshot_id'],
                'block_id': b['block_id'],
                'page': b['locator'].get('page'),
                'locator': b['locator'],
                'section': b.get('section', ''),
                'quotes': list(c.required_quotes),
                'parser_verified': bool(b.get('verified', False)),
                'semantic_support_verified': False,
            })
        # An explicit cross-document answer must contain anchored evidence from EACH
        # requested snapshot; a hit from only one document is not sufficient.
        if request.coverage_targets is not None and not request.coverage_targets <= {e['snapshot_id'] for e in accepted}:
            return GateResult('ABSTAIN_INCOMPLETE_SCOPE', 'Evidence missing from one or more selected documents')
        if not accepted:
            priority = ['ACCESS', 'ANCHOR_MISMATCH', 'LOW_QUALITY', 'QUOTE_NOT_IN_SOURCE', 'BUDGET', 'EMPTY_SOURCE']
            p = next((p for p in priority if p in rejected), 'NO_VALID_EVIDENCE')
            return GateResult('ABSTAIN_INVALID_EVIDENCE', p)

        # Each group represents one required subclaim; each subclaim must be
        # backed by at least one quoted source fragment.
        all_quotes = [normalize(q) for e in accepted for q in e['quotes']]
        for group in request.required_quote_groups:
            if not group or not any(normalize(q) in all_quotes for q in group):
                return GateResult('ABSTAIN_INCOMPLETE', 'One or more required evidence groups missing')

        # Fail-closed on ANY rejected proposed evidence (untrusted retrieval hit).
        # Never quietly drop invalid evidence and still present a complete answer.
        if rejected:
            return GateResult('NEEDS_REVIEW', 'Some candidates failed: ' + ','.join(sorted(set(rejected))), accepted)

        extracts = []
        for e in accepted:
            extracts.append(' '.join(e['quotes']) + f" [{e['snapshot_id']}, {e['locator']}, {e['block_id']}]")
        return GateResult(
            'EVIDENCE_ONLY',
            'Source excerpts aligned; answer semantics and completeness NOT verified',
            accepted,
            '\n'.join(extracts),
            False,
        )

    def verify_draft(self, result: GateResult, claims: list[dict[str, Any]]) -> GateResult:
        """Only permits exact evidence extracts; all paraphrases withheld pending review."""
        if result.status != 'EVIDENCE_ONLY':
            return GateResult('ABSTAIN_NO_APPROVED_DRAFT', 'Evidence gate not cleared')
        by_key = {e['evidence_key']: e for e in result.extracts}
        rendered: list[str] = []
        if not claims:
            return GateResult('ABSTAIN_NO_APPROVED_DRAFT', 'No proposed claims')
        for claim in claims:
            cites = claim.get('citations', [])
            text = claim.get('text', '')
            if not text or not isinstance(cites, list) or not cites or any(c not in by_key for c in cites):
                return GateResult('NEEDS_SEMANTIC_REVIEW', 'Missing or invalid claim citations')
            quoted = [normalize(q) for c in cites for q in by_key[c]['quotes']]
            if normalize(text) not in quoted:
                return GateResult('NEEDS_SEMANTIC_REVIEW', 'Paraphrased/generated claims not semantically adjudicated')
            rendered.append(text + ' ' + ' '.join('[' + c + ']' for c in cites))
        return GateResult('EVIDENCE_ONLY', 'Exact source quotation only; not a semantic answer', result.extracts, '\n'.join(rendered), False)


def conflict_on_curated_field(facts: list[dict[str, Any]], key: str, snapshot_id: str | None) -> str:
    """Only works for independently curated structured facts, NOT raw text contradiction detection."""
    selected = [f for f in facts if f.get('fact_key') == key and (snapshot_id is None or f.get('snapshot_id') == snapshot_id)]
    if any(not f.get('curated', False) for f in selected):
        return 'NEEDS_REVIEW'
    versions = {f.get('snapshot_id') for f in selected}
    if snapshot_id is None and len(versions) > 1:
        return 'VERSION_REQUIRED'
    if len({normalize(str(f.get('value'))) for f in selected}) > 1:
        return 'CONFLICT_REQUIRES_REVIEW'
    return 'NO_CONFLICT_DETECTED' if selected else 'NO_EVIDENCE'
