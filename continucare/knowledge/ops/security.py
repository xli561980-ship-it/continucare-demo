"""Privacy and SSRF guards for knowledge acquisition.

No function in this module resolves DNS or opens a socket.  It validates
de-identified acquisition inputs and the evidence a future transport must
return after every redirect.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

from continucare.knowledge.ops.models import KnowledgeOpsPolicyError, SourcePolicy


_FORBIDDEN_DATA_KEYS = frozenset(
    {
        "patient_id",
        "patient_identifier",
        "patient_name",
        "subject_id",
        "medical_record_number",
        "mrn",
        "encounter_id",
        "date_of_birth",
        "dob",
        "phone",
        "phone_number",
        "email",
        "email_address",
        "street_address",
        "home_address",
        "national_id",
        "id_card_number",
    }
)
_SENSITIVE_QUERY_KEY_PARTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "authorization",
    "signature",
    "credential",
    "session",
    "cookie",
)
_TECHNICAL_VALUE_KEYS = frozenset(
    {
        "code",
        "version",
        "document_version",
        "canonical_url",
        "relative_path",
        "content_type",
        "collection",
        "policy_id",
        "profile_id",
        "validation_profile_id",
    }
)
AUDITED_SHA256_FIELD_EVIDENCE = MappingProxyType(
    {
        # AppendOnlyLedger creates and replays these over canonical entry bytes.
        "entry_sha256": "AppendOnlyLedger.append/_history_unlocked canonical entry digest",
        "supersedes_entry_sha256": "AppendOnlyLedger predecessor-chain replay",
        # Acquisition/connectors create these from exact bytes or canonical metadata.
        "catalog_sha256": "OfflineFixtureConnector recomputes fixture catalog bytes",
        "content_sha256": "connector/quarantine recompute exact content bytes",
        "metadata_sha256": "AcquisitionService canonical SourceCandidate metadata digest",
        "whole_record_sha256": "EvidenceCandidate binds verified SourceSnapshot content",
        "whole_response_sha256": "source connector response_digest hashes exact response bytes",
        # Hash-pinned governance loading and its derived read/review pins.
        "source_catalog_sha256": (
            "alias audit loader recomputes exact v1 terminology catalog bytes"
        ),
        "document_sha256": "SourceRightsEvidence official-document capture digest",
        "manifest_sha256": "load_ops_bundle recomputes every pinned manifest",
        "bundle_index_sha256": "KnowledgeOpsBundle.index_sha256 canonical index digest",
        "governance_index_sha256": "KnowledgeOpsBundle.index_sha256 canonical index digest",
        "safety_boundary_sha256": "ReviewPacketBuilder canonical SafetyBoundary digest",
        # Exact ledger/ref bindings copied into immutable review/release objects.
        "subject_entry_sha256": "ReviewPacket exact LedgerRef binding",
        "expected_predecessor_sha256": "ReviewEvent append-only predecessor check",
        # Reviewer/author evidence and attestations have explicit producer/verifier contracts.
        "provenance_evidence_sha256": (
            "typed declaration only; untrusted until future evidence binding"
        ),
        "verification_evidence_sha256": "ReviewerVerifier identity-evidence contract",
        "reviewer_verification_evidence_sha256": "ReviewEvent reviewer snapshot binding",
        "reviewer_identity_assertion_sha256": "canonical reviewer identity assertion digest",
        "event_claim_sha256": "canonical ReviewEvent claim digest",
        "attestation_sha256": "ReviewerVerifier attestation producer/verifier contract",
    }
)
# Audit inventory only. Membership never grants recursive scanner trust.
AUDITED_SHA256_FIELDS = frozenset(AUDITED_SHA256_FIELD_EVIDENCE)
_LOWER_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_LABELED_IDENTIFIER = re.compile(
    r"(?i)(?:patient|patient[ _-]?id|mrn|medical[ _-]?record|身份证|病历号|患者)\s*[:=]"
)
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_DIGEST_ID_GROUP_SIZE = 8


@dataclass(frozen=True, slots=True)
class NumericSensitivePattern:
    name: str
    expression: re.Pattern[str]
    minimum_unseparated_digit_run: int | None
    requires_leading_plus: bool = False


class DigestTrustProfile(StrEnum):
    """Code-selected profiles for digests verified before a specific scan."""

    OFFLINE_FIXTURE_RESOURCE = "offline_fixture_resource"
    ACQUISITION_SOURCE_SNAPSHOT = "acquisition_source_snapshot"
    ACQUISITION_CHANGE_SET = "acquisition_change_set"
    ACQUISITION_KNOWLEDGE_GAP = "acquisition_knowledge_gap"
    ACQUISITION_RUN = "acquisition_run"
    EVIDENCE_CANDIDATE = "evidence_candidate"
    MACHINE_DRAFT_CLAIM = "machine_draft_claim"
    PROMOTION_DECISION = "promotion_decision"
    GOVERNED_SOURCE = "governed_source"
    REVIEW_PACKET = "review_packet"
    REVIEW_EVENT = "review_event"
    KNOWLEDGE_RELEASE_CANDIDATE = "knowledge_release_candidate"
    RELEASE_READINESS_REPORT_BASE = "release_readiness_report_base"
    RELEASE_READINESS_REPORT = "release_readiness_report"
    KNOWLEDGE_RELEASE = "knowledge_release"


@dataclass(frozen=True, slots=True)
class _SequenceIndex:
    pass


_ANY_SEQUENCE_INDEX = _SequenceIndex()
_DigestPathSegment = str | int
_DigestPathPatternSegment = str | _SequenceIndex


@dataclass(frozen=True, slots=True)
class TrustedDigestPath:
    segments: tuple[_DigestPathPatternSegment, ...]
    evidence: str


DIGEST_TRUST_PROFILE_PATHS = MappingProxyType(
    {
        DigestTrustProfile.OFFLINE_FIXTURE_RESOURCE: (
            TrustedDigestPath(
                ("content_sha256",),
                "OfflineFixtureConnector rehashed the exact fixture bytes",
            ),
        ),
        DigestTrustProfile.ACQUISITION_SOURCE_SNAPSHOT: (
            TrustedDigestPath(
                ("candidate_ref", "entry_sha256"),
                "AppendOnlyLedger returned the exact SourceCandidate reference",
            ),
            TrustedDigestPath(
                ("content_sha256",),
                "AcquisitionService rehashed the fetched bytes",
            ),
            TrustedDigestPath(
                ("metadata_sha256",),
                "AcquisitionService hashed canonical SourceCandidate metadata",
            ),
            TrustedDigestPath(
                ("quarantine_blob", "content_sha256"),
                "QuarantineBlobStore rehashed the bytes before returning the blob ref",
            ),
        ),
        DigestTrustProfile.ACQUISITION_CHANGE_SET: (
            TrustedDigestPath(
                ("candidate_ref", "entry_sha256"),
                "ChangeSet construction received the exact SourceCandidate LedgerRef",
            ),
            TrustedDigestPath(
                ("previous_snapshot_ref", "entry_sha256"),
                "ChangeSet construction received the replayed predecessor LedgerRef",
            ),
            TrustedDigestPath(
                ("current_snapshot_ref", "entry_sha256"),
                "ChangeSet construction received the exact current SourceSnapshot LedgerRef",
            ),
        ),
        DigestTrustProfile.ACQUISITION_KNOWLEDGE_GAP: (
            TrustedDigestPath(
                ("subject_ref", "entry_sha256"),
                "KnowledgeGap construction received an exact replayed subject LedgerRef",
            ),
        ),
        DigestTrustProfile.ACQUISITION_RUN: (
            TrustedDigestPath(
                ("candidate_refs", _ANY_SEQUENCE_INDEX, "entry_sha256"),
                "AcquisitionRun records SourceCandidate refs returned by the ledger",
            ),
            TrustedDigestPath(
                ("snapshot_refs", _ANY_SEQUENCE_INDEX, "entry_sha256"),
                "AcquisitionRun records SourceSnapshot refs returned by the ledger",
            ),
            TrustedDigestPath(
                ("change_set_refs", _ANY_SEQUENCE_INDEX, "entry_sha256"),
                "AcquisitionRun records ChangeSet refs returned by the ledger",
            ),
            TrustedDigestPath(
                ("gap_refs", _ANY_SEQUENCE_INDEX, "entry_sha256"),
                "AcquisitionRun records KnowledgeGap refs returned by the ledger",
            ),
        ),
        DigestTrustProfile.EVIDENCE_CANDIDATE: (
            TrustedDigestPath(
                ("source_candidate_ref", "entry_sha256"),
                "EvidenceCandidate staging replayed the exact SourceCandidate",
            ),
            TrustedDigestPath(
                ("source_snapshot_ref", "entry_sha256"),
                "EvidenceCandidate staging replayed the exact SourceSnapshot",
            ),
            TrustedDigestPath(
                ("whole_record_sha256",),
                "EvidenceCandidate binds the validated SourceSnapshot content digest",
            ),
        ),
        DigestTrustProfile.MACHINE_DRAFT_CLAIM: (
            TrustedDigestPath(
                ("evidence_candidate_ref", "entry_sha256"),
                "draft Claim promotion replayed the exact EvidenceCandidate",
            ),
            TrustedDigestPath(
                ("source_candidate_ref", "entry_sha256"),
                "draft Claim promotion replayed the exact SourceCandidate lineage",
            ),
            TrustedDigestPath(
                ("source_snapshot_ref", "entry_sha256"),
                "draft Claim promotion replayed the exact SourceSnapshot lineage",
            ),
        ),
        DigestTrustProfile.PROMOTION_DECISION: (
            TrustedDigestPath(
                ("subject_ref", "entry_sha256"),
                "Source promotion matched the decision to the exact candidate",
            ),
            TrustedDigestPath(
                ("evidence_refs", _ANY_SEQUENCE_INDEX, "entry_sha256"),
                "Source promotion replayed every current review-event reference",
            ),
            TrustedDigestPath(
                ("blocking_gap_refs", _ANY_SEQUENCE_INDEX, "entry_sha256"),
                "Source promotion replayed every current open KnowledgeGap reference",
            ),
        ),
        DigestTrustProfile.GOVERNED_SOURCE: (
            TrustedDigestPath(
                ("candidate_ref", "entry_sha256"),
                "governed Source construction replayed the exact SourceCandidate",
            ),
            TrustedDigestPath(
                ("snapshot_ref", "entry_sha256"),
                "governed Source construction replayed the exact SourceSnapshot",
            ),
            TrustedDigestPath(
                ("content_sha256",),
                "governed Source copies the validated SourceSnapshot content digest",
            ),
            TrustedDigestPath(
                ("promotion_evidence_refs", _ANY_SEQUENCE_INDEX, "entry_sha256"),
                "governed Source construction replayed promotion review evidence",
            ),
            TrustedDigestPath(
                ("unresolved_gap_refs", _ANY_SEQUENCE_INDEX, "entry_sha256"),
                "governed Source construction replayed promotion KnowledgeGaps",
            ),
        ),
        DigestTrustProfile.REVIEW_PACKET: (
            TrustedDigestPath(
                ("subject", "object_ref", "entry_sha256"),
                "ReviewPacketBuilder resolved the exact current ledger subject",
            ),
            TrustedDigestPath(
                ("subject_entry_sha256",),
                "ReviewPacket model binds this value to the exact subject LedgerRef",
            ),
            TrustedDigestPath(
                ("governance_index_sha256",),
                "packet material verification recomputed the loaded bundle index digest",
            ),
            TrustedDigestPath(
                ("governance_manifests", _ANY_SEQUENCE_INDEX, "manifest_sha256"),
                "packet material verification matched the loader-verified manifest evidence",
            ),
            TrustedDigestPath(
                ("evidence_refs", _ANY_SEQUENCE_INDEX, "entry_sha256"),
                "packet material verification replayed every evidence LedgerRef",
            ),
            TrustedDigestPath(
                ("open_gap_refs", _ANY_SEQUENCE_INDEX, "entry_sha256"),
                "packet material verification replayed every current open Gap LedgerRef",
            ),
            TrustedDigestPath(
                ("safety_boundary_sha256",),
                "packet material verification recomputed the SafetyBoundary digest",
            ),
        ),
        DigestTrustProfile.REVIEW_EVENT: (
            TrustedDigestPath(
                ("subject", "object_ref", "entry_sha256"),
                "ReviewEvent resolution matched the packet's replayed subject",
            ),
            TrustedDigestPath(
                ("packet_ref", "entry_sha256"),
                "ReviewEvent resolution replayed the exact current ReviewPacket",
            ),
            TrustedDigestPath(
                ("reviewer_verification_evidence_sha256",),
                "ReviewerVerifier authorization and identity snapshot checks succeeded",
            ),
            TrustedDigestPath(
                ("reviewer_identity_assertion_sha256",),
                "reviewer identity snapshot verification recomputed the assertion",
            ),
            TrustedDigestPath(
                ("expected_predecessor_sha256",),
                "ReviewEvent resolution matched the append-only predecessor",
            ),
            TrustedDigestPath(
                (
                    "decision_payload",
                    "checklist",
                    _ANY_SEQUENCE_INDEX,
                    "evidence_refs",
                    _ANY_SEQUENCE_INDEX,
                    "entry_sha256",
                ),
                "ReviewEvent resolution replayed each checklist evidence LedgerRef",
            ),
            TrustedDigestPath(
                ("review_attestation", "event_claim_sha256"),
                "ReviewerVerifier verified the recomputed canonical ReviewEvent claim",
            ),
            TrustedDigestPath(
                ("review_attestation", "attestation_sha256"),
                "ReviewerVerifier verified the ReviewEvent attestation",
            ),
        ),
        DigestTrustProfile.KNOWLEDGE_RELEASE_CANDIDATE: (
            TrustedDigestPath(
                ("governance_index_sha256",),
                "release staging matched the currently loaded governance index",
            ),
            TrustedDigestPath(
                ("governance_manifests", _ANY_SEQUENCE_INDEX, "manifest_sha256"),
                "release staging matched loader-verified governance manifests",
            ),
            TrustedDigestPath(
                (
                    "artifacts",
                    _ANY_SEQUENCE_INDEX,
                    "object_ref",
                    "entry_sha256",
                ),
                "release staging replayed every exact artifact LedgerRef",
            ),
            TrustedDigestPath(
                ("blocking_gap_refs", _ANY_SEQUENCE_INDEX, "entry_sha256"),
                "release staging replayed every blocking KnowledgeGap LedgerRef",
            ),
        ),
        DigestTrustProfile.RELEASE_READINESS_REPORT_BASE: (
            TrustedDigestPath(
                ("release_candidate_ref", "entry_sha256"),
                "readiness assessment replayed the exact release candidate",
            ),
        ),
        DigestTrustProfile.RELEASE_READINESS_REPORT: (
            TrustedDigestPath(
                ("release_candidate_ref", "entry_sha256"),
                "readiness assessment replayed the exact release candidate",
            ),
            TrustedDigestPath(
                (
                    "blockers",
                    _ANY_SEQUENCE_INDEX,
                    "subject_ref",
                    "entry_sha256",
                ),
                "readiness assessment produced blockers from replayed ledger refs",
            ),
        ),
        DigestTrustProfile.KNOWLEDGE_RELEASE: (
            TrustedDigestPath(
                ("release_candidate_ref", "entry_sha256"),
                "release finalization replayed the exact release candidate",
            ),
            TrustedDigestPath(
                ("readiness_report_ref", "entry_sha256"),
                "release finalization replayed the exact ready report",
            ),
            TrustedDigestPath(
                ("governance_index_sha256",),
                "release finalization preserves the verified governance index",
            ),
            TrustedDigestPath(
                ("governance_manifests", _ANY_SEQUENCE_INDEX, "manifest_sha256"),
                "release finalization preserves verified governance manifests",
            ),
            TrustedDigestPath(
                (
                    "artifacts",
                    _ANY_SEQUENCE_INDEX,
                    "object_ref",
                    "entry_sha256",
                ),
                "release finalization preserves replayed artifact refs",
            ),
        ),
    }
)


NUMERIC_SENSITIVE_PATTERNS = (
    NumericSensitivePattern(
        name="cn_phone",
        expression=re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
        minimum_unseparated_digit_run=11,
    ),
    NumericSensitivePattern(
        name="cn_national_id",
        expression=re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\w)"),
        minimum_unseparated_digit_run=17,
    ),
    NumericSensitivePattern(
        name="international_phone",
        expression=re.compile(r"(?<!\w)\+\d[\d ()-]{7,}\d(?!\w)"),
        minimum_unseparated_digit_run=None,
        requires_leading_plus=True,
    ),
)
min_sensitive_unseparated_digit_run = min(
    item.minimum_unseparated_digit_run
    for item in NUMERIC_SENSITIVE_PATTERNS
    if item.minimum_unseparated_digit_run is not None
    and not item.requires_leading_plus
)
if _DIGEST_ID_GROUP_SIZE >= min_sensitive_unseparated_digit_run:
    raise RuntimeError("digest ID grouping must stay below every unseparated numeric PII run")


def assert_deidentified_query_terms(terms: Sequence[str]) -> None:
    if not terms:
        raise KnowledgeOpsPolicyError("acquisition request requires controlled query terms")
    if len(terms) > 20:
        raise KnowledgeOpsPolicyError("acquisition request has too many query terms")
    for term in terms:
        normalized = " ".join(term.split())
        if not normalized or len(normalized) > 128:
            raise KnowledgeOpsPolicyError("query term must contain 1-128 characters")
        if normalized != term.strip():
            raise KnowledgeOpsPolicyError("query terms must be normalized before acquisition")
        if "http://" in normalized.lower() or "https://" in normalized.lower():
            raise KnowledgeOpsPolicyError("query terms cannot contain URLs")
        if _contains_sensitive_text(normalized):
            raise KnowledgeOpsPolicyError("query terms appear to contain personal data")


def assert_no_sensitive_data(
    value: object,
    *,
    path: str = "payload",
    digest_trust_profile: DigestTrustProfile | None = None,
) -> None:
    """Reject common direct identifiers in structured staging payloads."""

    if digest_trust_profile is not None and not isinstance(
        digest_trust_profile, DigestTrustProfile
    ):
        raise TypeError("digest trust profile must be a code-defined constant")
    _assert_no_sensitive_data(
        value,
        display_path=path,
        structured_path=(),
        digest_trust_profile=digest_trust_profile,
    )


def _assert_no_sensitive_data(
    value: object,
    *,
    display_path: str,
    structured_path: tuple[_DigestPathSegment, ...],
    digest_trust_profile: DigestTrustProfile | None,
) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized_key = str(key).strip().lower().replace("-", "_")
            item_structured_path = (*structured_path, str(key))
            if normalized_key in _FORBIDDEN_DATA_KEYS:
                raise KnowledgeOpsPolicyError(
                    f"patient/personal data key is prohibited at {display_path}.{key}"
                )
            if isinstance(item, str) and (
                normalized_key in _TECHNICAL_VALUE_KEYS
                or (
                    _digest_path_is_trusted(
                        digest_trust_profile,
                        item_structured_path,
                    )
                    and _LOWER_SHA256.fullmatch(item) is not None
                )
            ):
                continue
            _assert_no_sensitive_data(
                item,
                display_path=f"{display_path}.{key}",
                structured_path=item_structured_path,
                digest_trust_profile=digest_trust_profile,
            )
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            _assert_no_sensitive_data(
                item,
                display_path=f"{display_path}[{index}]",
                structured_path=(*structured_path, index),
                digest_trust_profile=digest_trust_profile,
            )
        return
    if isinstance(value, str) and _contains_sensitive_text(value):
        raise KnowledgeOpsPolicyError(
            f"payload text appears to contain personal data at {display_path}"
        )


def _digest_path_is_trusted(
    profile: DigestTrustProfile | None,
    structured_path: tuple[_DigestPathSegment, ...],
) -> bool:
    if profile is None:
        return False
    return any(
        len(item.segments) == len(structured_path)
        and all(
            (
                isinstance(expected, _SequenceIndex)
                and isinstance(actual, int)
            )
            or expected == actual
            for expected, actual in zip(item.segments, structured_path)
        )
        for item in DIGEST_TRUST_PROFILE_PATHS[profile]
    )


def validate_url_against_policy(url: str, policy: SourcePolicy) -> str:
    """Return a canonical permitted URL or fail before connector access."""

    if any(ord(character) < 32 for character in url):
        raise KnowledgeOpsPolicyError("source URL contains control characters")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise KnowledgeOpsPolicyError(f"source URL is malformed: {exc}") from exc
    if parsed.scheme != "https":
        raise KnowledgeOpsPolicyError("source URL must use https")
    if parsed.username or parsed.password:
        raise KnowledgeOpsPolicyError("source URL cannot contain credentials")
    if parsed.fragment:
        raise KnowledgeOpsPolicyError("acquisition URL cannot contain a fragment")
    if port not in {None, 443}:
        raise KnowledgeOpsPolicyError("source URL may only use port 443")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise KnowledgeOpsPolicyError("source URL requires a host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise KnowledgeOpsPolicyError("source URL cannot use an IP literal")
    if host == "localhost" or host.endswith((".local", ".internal")):
        raise KnowledgeOpsPolicyError("source URL host is local or internal")

    allowed_hosts = {
        (urlsplit(str(origin)).hostname or "").lower().rstrip(".")
        for origin in policy.allowed_origins
    }
    permitted = host in allowed_hosts or (
        policy.allow_subdomains
        and any(host.endswith(f".{allowed}") for allowed in allowed_hosts)
    )
    if not permitted:
        raise KnowledgeOpsPolicyError(
            f"source host {host!r} is outside SourcePolicy {policy.policy_id}"
        )

    if _INVALID_PERCENT_ESCAPE.search(parsed.path) or _INVALID_PERCENT_ESCAPE.search(
        parsed.query
    ):
        raise KnowledgeOpsPolicyError("source URL contains an invalid percent escape")
    decoded_path = _fully_unquote(parsed.path, component="path")
    if any(ord(character) < 32 for character in decoded_path):
        raise KnowledgeOpsPolicyError("source URL path contains encoded control characters")
    if "\\" in parsed.path or "\\" in decoded_path:
        raise KnowledgeOpsPolicyError("source URL path cannot contain backslashes")
    if any(part in {".", ".."} for part in decoded_path.split("/")):
        raise KnowledgeOpsPolicyError("source URL path cannot contain traversal segments")
    if _contains_sensitive_text(decoded_path):
        raise KnowledgeOpsPolicyError("source URL path appears to contain personal data")

    try:
        query_pairs = parse_qsl(
            parsed.query, keep_blank_values=True, strict_parsing=True
        )
    except ValueError as exc:
        raise KnowledgeOpsPolicyError("source URL query is malformed") from exc
    query_keys = [key for key, _ in query_pairs]
    if len(query_keys) != len(set(query_keys)):
        raise KnowledgeOpsPolicyError("source URL query parameters must be unique")
    allowed_query = set(policy.allowed_query_parameters)
    for key, value in query_pairs:
        lowered = key.lower()
        if key not in allowed_query:
            raise KnowledgeOpsPolicyError(
                f"source URL query parameter {key!r} is not allowlisted"
            )
        if any(part in lowered for part in _SENSITIVE_QUERY_KEY_PARTS):
            raise KnowledgeOpsPolicyError("source URL query cannot contain credentials")
        decoded_value = _fully_unquote(value, component="query value")
        if (
            len(decoded_value) > 256
            or any(ord(character) < 32 for character in decoded_value)
            or _contains_sensitive_text(decoded_value)
        ):
            raise KnowledgeOpsPolicyError(
                f"source URL query value for {key!r} is unsafe or contains personal data"
            )

    canonical_netloc = host if port is None else f"{host}:{port}"
    return urlunsplit(("https", canonical_netloc, parsed.path or "/", parsed.query, ""))


def validate_public_peer_ip(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise KnowledgeOpsPolicyError("transport peer address is not an IP") from exc
    if not address.is_global or any(
        (
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
        )
    ):
        raise KnowledgeOpsPolicyError("transport peer address is not public")
    return address.compressed


def validate_transport_route(
    *,
    requested_url: str,
    redirect_urls: Sequence[str],
    peer_ips: Sequence[str],
    policy: SourcePolicy,
) -> tuple[str, ...]:
    if len(redirect_urls) > 3:
        raise KnowledgeOpsPolicyError("connector redirect limit exceeded")
    route = (requested_url, *redirect_urls)
    if len(peer_ips) != len(route):
        raise KnowledgeOpsPolicyError("transport must attest a peer IP for every hop")
    canonical = tuple(validate_url_against_policy(item, policy) for item in route)
    for peer_ip in peer_ips:
        validate_public_peer_ip(peer_ip)
    return canonical


def _contains_sensitive_text(value: str) -> bool:
    return (
        _EMAIL.search(value) is not None
        or _LABELED_IDENTIFIER.search(value) is not None
        or any(item.expression.search(value) for item in NUMERIC_SENSITIVE_PATTERNS)
    )


def digest_derived_internal_id(
    prefix: str,
    digest_hex: str,
    *,
    digest_characters: int,
) -> str:
    """Format real digest material as a SafeId without long numeric runs."""

    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{0,63}", prefix):
        raise ValueError("digest-derived ID prefix is not safe")
    if (
        isinstance(digest_characters, bool)
        or not isinstance(digest_characters, int)
        or digest_characters < 1
        or digest_characters > 64
        or len(digest_hex) != 64
        or _LOWER_SHA256.fullmatch(digest_hex) is None
    ):
        raise ValueError("digest-derived ID requires lower-case hexadecimal digest material")
    selected = digest_hex[:digest_characters]
    grouped = "-".join(
        selected[index : index + _DIGEST_ID_GROUP_SIZE]
        for index in range(0, len(selected), _DIGEST_ID_GROUP_SIZE)
    )
    candidate = f"{prefix}-{grouped}"
    if maximum_unseparated_digit_run(candidate) >= min_sensitive_unseparated_digit_run:
        raise ValueError("digest-derived ID violates the numeric PII separation threshold")
    return candidate


def maximum_unseparated_digit_run(value: str) -> int:
    return max((len(item) for item in re.findall(r"\d+", value)), default=0)


def _fully_unquote(value: str, *, component: str) -> str:
    current = value
    for _ in range(4):
        decoded = unquote(current)
        if decoded == current:
            return decoded
        current = decoded
    raise KnowledgeOpsPolicyError(
        f"source URL {component} uses excessive nested percent encoding"
    )
