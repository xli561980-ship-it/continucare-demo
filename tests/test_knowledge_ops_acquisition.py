from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from continucare.knowledge.ops import (
    AcquisitionEnvironment,
    AcquisitionRequest,
    AcquisitionService,
    AppendOnlyLedger,
    ChangeSet,
    GovernedSourceV2,
    GovernanceGate,
    GuardedHttpConnector,
    KnowledgeGap,
    KnowledgeOpsIntegrityError,
    KnowledgeOpsPolicyError,
    LedgerCollection,
    NetworkAccessDisabled,
    OfflineFixtureConnector,
    PromotionDecision,
    QuarantineBlobStore,
    SourceCandidate,
    SourcePromotionService,
    SourceSnapshot,
    assert_deidentified_query_terms,
    assert_no_sensitive_data,
    load_builtin_ops_bundle,
    validate_public_peer_ip,
    validate_transport_route,
    validate_url_against_policy,
)
from continucare.knowledge.ops.acquisition import AcquisitionRun
from continucare.knowledge.ops.security import NUMERIC_SENSITIVE_PATTERNS


FIXTURE_CATALOG_SHA256 = (
    "e711994018bb783236e050d890783502eaee91e7345e4d4e9808fdf542764a3f"
)
PROFILE_POLICIES = {
    "fixture-medication-followup": "nmpa-cn-regulatory-metadata",
    "fixture-chronic-cardiopulmonary": "nlm-pubmed-metadata",
    "fixture-oncology-pro": "nci-pro-ctcae-metadata",
    "fixture-acute-high-risk": "nmpa-cn-regulatory-metadata",
    "fixture-rare-disease-terminology": "hpo-official-release-metadata",
}
VERIFIED_ACQUISITION_DIGEST_PAYLOAD = b"verified-acquisition-digest-467"
VERIFIED_ACQUISITION_DIGEST_SHA256 = (
    "c892a18617c8d8e20ec15680838003dd5d50a80b269c89daccf1960b9915acd8"
)
VERIFIED_ACQUISITION_DIGEST_CN_PHONE = "15680838003"


def _fixture_root() -> Path:
    return Path(__file__).parent / "fixtures" / "knowledge_ops"


def _connector(root: Path | None = None, digest: str = FIXTURE_CATALOG_SHA256):
    return OfflineFixtureConnector(root or _fixture_root(), catalog_sha256=digest)


def _request(profile_id: str, *, request_id: str, policy_id: str | None = None):
    bundle = load_builtin_ops_bundle()
    profile = next(item for item in bundle.coverage_profiles if item.profile_id == profile_id)
    return AcquisitionRequest(
        request_id=request_id,
        validation_profile_id=profile_id,
        trigger="scheduled",
        policy_ids=(policy_id or PROFILE_POLICIES[profile_id],),
        topic_codes=(
            {
                "system": "urn:continucare:synthetic-topic",
                "version": "1",
                "code": profile_id,
                "display": "Synthetic validation topic",
            },
        ),
        query_terms=(profile_id.replace("fixture-", "").replace("-", " "),),
        scope=profile.scope,
        created_at=datetime.now(timezone.utc),
        created_by="system:test",
    )


def _service(tmp_path: Path, *, connector=None, environment="synthetic_test"):
    ledger = AppendOnlyLedger(tmp_path / "ledger")
    quarantine = QuarantineBlobStore(tmp_path / "quarantine")
    service = AcquisitionService(
        bundle=load_builtin_ops_bundle(),
        ledger=ledger,
        quarantine=quarantine,
        connector=connector or _connector(),
        environment=environment,
    )
    return service, ledger, quarantine


def _ledger_state(ledger: AppendOnlyLedger):
    return (
        ledger.verify_all(),
        tuple(
            (
                collection.value,
                tuple(entry.ref for entry in ledger.list_heads(collection)),
            )
            for collection in LedgerCollection
        ),
    )


def test_offline_pipeline_covers_all_five_validation_domains(tmp_path):
    service, ledger, quarantine = _service(tmp_path)
    results = []

    for index, profile_id in enumerate(PROFILE_POLICIES, start=1):
        results.append(
            service.run(_request(profile_id, request_id=f"five-domain-{index}"))
        )

    assert all(result.status == "completed" for result in results)
    assert all(len(result.candidate_refs) == 1 for result in results)
    assert all(len(result.snapshot_refs) == 1 for result in results)
    assert all(len(result.change_set_refs) == 1 for result in results)
    assert all(len(result.gap_refs) == 2 for result in results)
    assert ledger.verify_all() == 35
    assert len(tuple((quarantine.root / "blobs").glob("*.bin"))) == 5

    for result in results:
        candidate = SourceCandidate.model_validate(
            ledger.get(result.candidate_refs[0]).payload
        )
        snapshot = SourceSnapshot.model_validate(
            ledger.get(result.snapshot_refs[0]).payload
        )
        change = ChangeSet.model_validate(ledger.get(result.change_set_refs[0]).payload)
        gaps = [KnowledgeGap.model_validate(ledger.get(ref).payload) for ref in result.gap_refs]
        assert candidate.synthetic is True
        assert candidate.contains_patient_data is False
        assert candidate.knowledge_effect == "informational_only"
        assert candidate.runtime_authority == "none"
        assert snapshot.storage == "quarantined_synthetic_fixture"
        assert snapshot.quarantine_blob is not None
        assert quarantine.read_verified(snapshot.quarantine_blob)
        assert change.change_kind == "new_source"
        assert change.requires_review is True
        assert {gap.gap_kind for gap in gaps} == {
            "rights_review_missing",
            "source_review_missing",
        }
        assert all(gap.runtime_authority == "none" for gap in gaps)


def test_repeated_identical_fixture_creates_append_only_unchanged_change_set(tmp_path):
    service, ledger, _ = _service(tmp_path)
    first = service.run(
        _request("fixture-medication-followup", request_id="repeat-1")
    )
    second = service.run(
        _request("fixture-medication-followup", request_id="repeat-2")
    )

    assert first.status == second.status == "completed"
    assert second.candidate_refs[0].record_version == 2
    assert second.snapshot_refs[0].record_version == 2
    assert second.change_set_refs[0].record_version == 2
    change = ChangeSet.model_validate(ledger.get(second.change_set_refs[0]).payload)
    assert change.change_kind == "unchanged"
    assert change.changed_fields == ()
    assert change.requires_review is False
    assert len(second.gap_refs) == 2


def test_fixture_content_update_creates_content_change_not_silent_overwrite(tmp_path):
    fixture_root = tmp_path / "fixtures"
    shutil.copytree(_fixture_root(), fixture_root)
    first_connector = _connector(fixture_root)
    service, ledger, _ = _service(tmp_path / "state", connector=first_connector)
    first = service.run(
        _request("fixture-medication-followup", request_id="content-change-1")
    )

    content_path = fixture_root / "medication_followup.txt"
    content_path.write_text(
        content_path.read_text(encoding="utf-8") + "Synthetic change marker: 2\n",
        encoding="utf-8",
    )
    catalog_path = fixture_root / "catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    resource = next(
        item for item in catalog["resources"] if item["stable_id"] == "medication-followup-v1"
    )
    resource["content_sha256"] = hashlib.sha256(content_path.read_bytes()).hexdigest()
    catalog_path.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    second_connector = _connector(
        fixture_root, hashlib.sha256(catalog_path.read_bytes()).hexdigest()
    )
    second_service = AcquisitionService(
        bundle=load_builtin_ops_bundle(),
        ledger=ledger,
        quarantine=QuarantineBlobStore(tmp_path / "state" / "quarantine"),
        connector=second_connector,
    )
    second = second_service.run(
        _request("fixture-medication-followup", request_id="content-change-2")
    )

    old_snapshot = SourceSnapshot.model_validate(ledger.get(first.snapshot_refs[0]).payload)
    new_snapshot = SourceSnapshot.model_validate(ledger.get(second.snapshot_refs[0]).payload)
    change = ChangeSet.model_validate(ledger.get(second.change_set_refs[0]).payload)
    assert old_snapshot.content_sha256 != new_snapshot.content_sha256
    assert change.change_kind == "content_changed"
    assert change.changed_fields == ("content",)
    assert len(second.gap_refs) == 3
    assert any(
        KnowledgeGap.model_validate(ledger.get(ref).payload).gap_kind
        == "content_change_review_missing"
        for ref in second.gap_refs
    )
    assert ledger.history(LedgerCollection.SNAPSHOT, second.snapshot_refs[0].record_id)[0].ref == first.snapshot_refs[0]


def test_offline_connector_rejects_catalog_hash_mismatch():
    with pytest.raises(KnowledgeOpsIntegrityError, match="catalog SHA-256"):
        _connector(digest="0" * 64)


def test_offline_connector_rejects_content_tampering(tmp_path):
    root = tmp_path / "fixtures"
    shutil.copytree(_fixture_root(), root)
    (root / "oncology_pro.txt").write_text("tampered", encoding="utf-8")

    with pytest.raises(KnowledgeOpsIntegrityError, match="content SHA-256"):
        _connector(root)


def test_offline_connector_rejects_symlinked_fixture(tmp_path):
    root = tmp_path / "fixtures"
    shutil.copytree(_fixture_root(), root)
    target = root / "rare_terminology.txt"
    real = root / "real.txt"
    target.rename(real)
    target.symlink_to(real)

    with pytest.raises(KnowledgeOpsIntegrityError, match="symlink"):
        _connector(root)


@pytest.mark.parametrize(
    "url",
    [
        "http://www.nmpa.gov.cn/source",
        "https://user:secret@www.nmpa.gov.cn/source",
        "https://127.0.0.1/source",
        "https://localhost/source",
        "https://evil.example/source",
        "https://www.nmpa.gov.cn:8443/source",
        "https://www.nmpa.gov.cn/a/../source",
        "https://www.nmpa.gov.cn/source#fragment",
        "https://www.nmpa.gov.cn/source?token=secret",
        "https://www.nmpa.gov.cn/patient/user@example.org",
        "https://www.nmpa.gov.cn/a/%252e%252e/source",
        "https://www.nmpa.gov.cn/patient/user%2540example.org",
        "https://www.nmpa.gov.cn/source%2500",
        "https://www.nmpa.gov.cn/source%ZZ",
    ],
)
def test_url_guard_rejects_ssrf_and_credential_shapes(url):
    policy = load_builtin_ops_bundle().source_policy("nmpa-cn-regulatory-metadata")

    with pytest.raises(KnowledgeOpsPolicyError):
        validate_url_against_policy(url, policy)


def test_url_guard_accepts_only_allowlisted_pubmed_query_parameters():
    policy = load_builtin_ops_bundle().source_policy("nlm-pubmed-metadata")

    canonical = validate_url_against_policy(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term=heart%20failure",
        policy,
    )
    assert canonical.startswith("https://eutils.ncbi.nlm.nih.gov/")
    with pytest.raises(KnowledgeOpsPolicyError, match="not allowlisted"):
        validate_url_against_policy(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?callback=x",
            policy,
        )
    with pytest.raises(KnowledgeOpsPolicyError, match="personal data"):
        validate_url_against_policy(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?term=user@example.org",
            policy,
        )


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1"],
)
def test_transport_peer_guard_rejects_non_public_addresses(address):
    with pytest.raises(KnowledgeOpsPolicyError, match="not public"):
        validate_public_peer_ip(address)


def test_transport_route_revalidates_every_redirect_and_peer():
    policy = load_builtin_ops_bundle().source_policy("nmpa-cn-regulatory-metadata")
    requested = "https://www.nmpa.gov.cn/start"

    assert validate_transport_route(
        requested_url=requested,
        redirect_urls=("https://www.nmpa.gov.cn/final",),
        peer_ips=("8.8.8.8", "1.1.1.1"),
        policy=policy,
    ) == (requested, "https://www.nmpa.gov.cn/final")
    with pytest.raises(KnowledgeOpsPolicyError):
        validate_transport_route(
            requested_url=requested,
            redirect_urls=("https://evil.example/final",),
            peer_ips=("8.8.8.8", "1.1.1.1"),
            policy=policy,
        )
    with pytest.raises(KnowledgeOpsPolicyError, match="peer IP"):
        validate_transport_route(
            requested_url=requested,
            redirect_urls=("https://www.nmpa.gov.cn/final",),
            peer_ips=("8.8.8.8",),
            policy=policy,
        )


@pytest.mark.parametrize(
    "term",
    [
        "user@example.org",
        "13800138000",
        "+49 151 23456789",
        "11010519491231002X",
        "patient_id: abc-123",
        "https://example.org/search",
    ],
)
def test_acquisition_query_terms_reject_direct_identifiers_and_urls(term):
    with pytest.raises(KnowledgeOpsPolicyError):
        assert_deidentified_query_terms((term,))


def test_structured_privacy_guard_rejects_nested_patient_fields():
    with pytest.raises(KnowledgeOpsPolicyError, match="patient/personal data key"):
        assert_no_sensitive_data({"source": {"patient_id": "synthetic-1"}})


def test_structured_privacy_guard_only_exempts_well_formed_technical_hashes():
    assert_no_sensitive_data({"entry_sha256": "1" * 64})

    with pytest.raises(KnowledgeOpsPolicyError, match="personal data"):
        assert_no_sensitive_data({"external_id": "13800138000"})
    with pytest.raises(KnowledgeOpsPolicyError, match="personal data"):
        assert_no_sensitive_data({"entry_sha256": "user@example.org"})


def test_acquisition_request_extra_patient_field_fails_schema():
    payload = _request(
        "fixture-medication-followup", request_id="patient-field"
    ).model_dump(mode="json")
    payload["patient_id"] = "synthetic-1"

    with pytest.raises(ValidationError, match="Extra inputs"):
        AcquisitionRequest.model_validate(payload)


def test_guarded_http_connector_is_inert_and_never_calls_transport():
    class ExplodingTransport:
        called = False

        def get(self, url, *, maximum_bytes):
            self.called = True
            raise AssertionError("transport must not be called")

    transport = ExplodingTransport()
    connector = GuardedHttpConnector(network_enabled=True, transport=transport)
    policy = load_builtin_ops_bundle().source_policy("nmpa-cn-regulatory-metadata")
    offline = _connector()
    request = _request("fixture-medication-followup", request_id="live-disabled")
    resource = offline.discover(request, policy)[0]

    with pytest.raises(NetworkAccessDisabled, match="disabled"):
        connector.fetch(resource, policy)
    assert transport.called is False


def test_production_acquisition_rejects_offline_fixture_without_writing(tmp_path):
    service, ledger, _ = _service(tmp_path, environment=AcquisitionEnvironment.PRODUCTION)

    with pytest.raises(KnowledgeOpsPolicyError, match="cannot run in production"):
        service.run(_request("fixture-medication-followup", request_id="production"))
    assert ledger.verify_all() == 0


@pytest.mark.parametrize(
    "updates",
    [
        {"validation_profile_id": "fixture-oncology-pro"},
        {"policy_id": "hpo-official-release-metadata"},
        {"synthetic": False},
    ],
)
def test_acquisition_revalidates_connector_resources_before_staging(
    tmp_path, updates
):
    bundle = load_builtin_ops_bundle()
    request = _request("fixture-medication-followup", request_id="connector-boundary")
    policy = bundle.source_policy("nmpa-cn-regulatory-metadata")
    discovered = _connector().discover(request, policy)[0].model_copy(
        update={"connector_id": "boundary-test", **updates}
    )

    class BoundaryConnector:
        connector_id = "boundary-test"

        def discover(self, request, policy):
            return (discovered,)

        def fetch(self, resource, policy):
            raise AssertionError("invalid resource must fail before fetch")

    service, ledger, _ = _service(tmp_path, connector=BoundaryConnector())
    result = service.run(request)

    assert result.status == "failed"
    assert result.candidate_refs == ()
    run = AcquisitionRun.model_validate(ledger.get(result.run_ref).payload)
    assert run.failure_code == "policy_error"


@pytest.mark.parametrize(
    "updates",
    [
        {"connector_id": "different-connector"},
        {"stable_id": "different-resource"},
        {
            "canonical_url": (
                "https://www.nmpa.gov.cn/synthetic-offline-fixture/different"
            )
        },
        {"content_type": "application/xml"},
        {"content_sha256": "0" * 64},
        {
            "body": b"Contact user@example.org",
            "content_sha256": hashlib.sha256(
                b"Contact user@example.org"
            ).hexdigest(),
        },
        {
            "body": b"\xff\xfe",
            "content_sha256": hashlib.sha256(b"\xff\xfe").hexdigest(),
        },
        {"synthetic": False},
        {"redirect_urls": ("https://www.nmpa.gov.cn/redirect",)},
    ],
)
def test_acquisition_revalidates_fetched_bytes_before_quarantine(tmp_path, updates):
    bundle = load_builtin_ops_bundle()
    request = _request("fixture-medication-followup", request_id="fetch-boundary")
    policy = bundle.source_policy("nmpa-cn-regulatory-metadata")
    fixture_connector = _connector()
    fixture_resource = fixture_connector.discover(request, policy)[0]
    discovered = fixture_resource.model_copy(
        update={"connector_id": "fetch-boundary-test"}
    )
    fetched = replace(
        fixture_connector.fetch(fixture_resource, policy),
        **{"connector_id": "fetch-boundary-test", **updates},
    )

    class BoundaryConnector:
        connector_id = "fetch-boundary-test"

        def discover(self, request, policy):
            return (discovered,)

        def fetch(self, resource, policy):
            return fetched

    service, ledger, quarantine = _service(
        tmp_path, connector=BoundaryConnector()
    )
    result = service.run(request)

    assert result.status == "failed"
    assert result.snapshot_refs == ()
    assert list((quarantine.root / "blobs").iterdir()) == []
    run = AcquisitionRun.model_validate(ledger.get(result.run_ref).payload)
    assert run.failure_code in {"integrity_error", "policy_error"}


def test_recomputed_phone_shaped_content_digest_uses_exact_snapshot_trust_path(
    tmp_path,
):
    bundle = load_builtin_ops_bundle()
    request = _request(
        "fixture-medication-followup", request_id="verified-digest-path"
    )
    policy = bundle.source_policy("nmpa-cn-regulatory-metadata")
    fixture_connector = _connector()
    fixture_resource = fixture_connector.discover(request, policy)[0]
    discovered = fixture_resource.model_copy(
        update={"connector_id": "verified-digest-path-test"}
    )
    body = VERIFIED_ACQUISITION_DIGEST_PAYLOAD
    digest = hashlib.sha256(body).hexdigest()
    assert digest == VERIFIED_ACQUISITION_DIGEST_SHA256
    phone_pattern = next(
        item.expression for item in NUMERIC_SENSITIVE_PATTERNS if item.name == "cn_phone"
    )
    phone_match = phone_pattern.search(digest)
    assert phone_match is not None
    assert phone_match.group() == VERIFIED_ACQUISITION_DIGEST_CN_PHONE
    fetched = replace(
        fixture_connector.fetch(fixture_resource, policy),
        connector_id="verified-digest-path-test",
        body=body,
        content_sha256=digest,
    )

    class VerifiedDigestConnector:
        connector_id = "verified-digest-path-test"

        def discover(self, request, policy):
            return (discovered,)

        def fetch(self, resource, policy):
            return fetched

    service, ledger, quarantine = _service(
        tmp_path,
        connector=VerifiedDigestConnector(),
    )
    result = service.run(request)
    snapshot = SourceSnapshot.model_validate(
        ledger.get(result.snapshot_refs[0]).payload
    )

    assert result.status == "completed"
    assert hashlib.sha256(body).hexdigest() == digest == snapshot.content_sha256
    phone_match = phone_pattern.search(digest)
    assert phone_match is not None
    assert phone_match.group() == VERIFIED_ACQUISITION_DIGEST_CN_PHONE
    assert snapshot.quarantine_blob is not None
    assert quarantine.read_verified(snapshot.quarantine_blob) == body


def test_offline_fixture_connector_accepts_rehashed_phone_bearing_content_digest(
    tmp_path,
):
    fixture_root = tmp_path / "phone-bearing-fixture"
    fixture_root.mkdir()
    body = VERIFIED_ACQUISITION_DIGEST_PAYLOAD
    digest = hashlib.sha256(body).hexdigest()
    assert digest == VERIFIED_ACQUISITION_DIGEST_SHA256
    catalog = json.loads((_fixture_root() / "catalog.json").read_text())
    resource = next(
        item
        for item in catalog["resources"]
        if item["validation_profile_id"] == "fixture-medication-followup"
    )
    resource = {
        **resource,
        "content_path": "verified-phone-bearing-digest.bin",
        "content_sha256": digest,
    }
    catalog["resources"] = [resource]
    catalog_bytes = json.dumps(
        catalog,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    (fixture_root / resource["content_path"]).write_bytes(body)
    (fixture_root / "catalog.json").write_bytes(catalog_bytes)

    connector = OfflineFixtureConnector(
        fixture_root,
        catalog_sha256=hashlib.sha256(catalog_bytes).hexdigest(),
    )
    request = _request(
        "fixture-medication-followup", request_id="verified-fixture-digest"
    )
    policy = load_builtin_ops_bundle().source_policy(
        "nmpa-cn-regulatory-metadata"
    )
    discovered = connector.discover(request, policy)
    assert len(discovered) == 1
    fetched = connector.fetch(discovered[0], policy)
    assert fetched.body == body
    assert fetched.content_sha256 == digest


def test_acquisition_rejects_retired_source_policy_before_writing(tmp_path):
    bundle = load_builtin_ops_bundle()
    policies = tuple(
        (
            policy.model_copy(update={"status": "retired"})
            if policy.policy_id == "nmpa-cn-regulatory-metadata"
            else policy
        )
        for policy in bundle.source_policies
    )
    retired_bundle = replace(bundle, source_policies=policies)
    ledger = AppendOnlyLedger(tmp_path / "ledger")
    service = AcquisitionService(
        bundle=retired_bundle,
        ledger=ledger,
        quarantine=QuarantineBlobStore(tmp_path / "quarantine"),
        connector=_connector(),
    )

    with pytest.raises(KnowledgeOpsPolicyError, match="retired SourcePolicy"):
        service.run(_request("fixture-medication-followup", request_id="retired"))
    assert ledger.verify_all() == 0


def test_acquisition_rejects_scope_drift_before_writing(tmp_path):
    request = _request("fixture-medication-followup", request_id="scope-drift")
    other = _request("fixture-oncology-pro", request_id="other-scope")
    payload = request.model_dump(mode="json")
    payload["scope"] = other.scope.model_dump(mode="json")
    drifted = AcquisitionRequest.model_validate(payload)
    service, ledger, _ = _service(tmp_path)

    with pytest.raises(KnowledgeOpsPolicyError, match="exact frozen"):
        service.run(drifted)
    assert ledger.verify_all() == 0


def test_connector_failure_is_audited_without_raw_exception_text(tmp_path):
    class FailingConnector(OfflineFixtureConnector):
        def fetch(self, resource, policy):
            raise RuntimeError("secret-token-should-not-be-persisted")

    connector = FailingConnector(
        _fixture_root(), catalog_sha256=FIXTURE_CATALOG_SHA256
    )
    service, ledger, _ = _service(tmp_path, connector=connector)
    result = service.run(
        _request("fixture-medication-followup", request_id="connector-failure")
    )

    assert result.status == "failed"
    run = AcquisitionRun.model_validate(ledger.get(result.run_ref).payload)
    assert run.failure_code == "connector_error"
    serialized = json.dumps(run.model_dump(mode="json"), ensure_ascii=False)
    assert "secret-token" not in serialized
    gap = KnowledgeGap.model_validate(ledger.get(result.gap_refs[-1]).payload)
    assert gap.gap_kind == "connector_failure"


def test_missing_fixture_is_a_gap_not_an_invented_candidate(tmp_path):
    service, ledger, _ = _service(tmp_path)
    result = service.run(
        _request(
            "fixture-medication-followup",
            request_id="missing-source",
            policy_id="hpo-official-release-metadata",
        )
    )

    assert result.status == "completed"
    assert result.candidate_refs == ()
    assert result.snapshot_refs == ()
    assert len(result.gap_refs) == 1
    gap = KnowledgeGap.model_validate(ledger.get(result.gap_refs[0]).payload)
    assert gap.gap_kind == "source_missing"


def test_quarantine_detects_blob_tampering(tmp_path):
    service, ledger, quarantine = _service(tmp_path)
    result = service.run(
        _request("fixture-medication-followup", request_id="blob-tamper")
    )
    snapshot = SourceSnapshot.model_validate(ledger.get(result.snapshot_refs[0]).payload)
    assert snapshot.quarantine_blob is not None
    target = quarantine.root / snapshot.quarantine_blob.relative_path
    target.write_bytes(b"tampered")

    with pytest.raises(KnowledgeOpsIntegrityError, match="integrity mismatch"):
        quarantine.read_verified(snapshot.quarantine_blob)


def _promotion_context(tmp_path):
    service, ledger, _ = _service(tmp_path)
    result = service.run(
        _request("fixture-medication-followup", request_id="promotion")
    )
    curator = ledger.append(
        LedgerCollection.REVIEW_EVENT,
        "synthetic-curator-review",
        payload_type="review_event",
        payload={"role": "knowledge_curator", "synthetic": True},
        recorded_by="system:test",
        synthetic=True,
    ).ref
    rights = ledger.append(
        LedgerCollection.REVIEW_EVENT,
        "synthetic-rights-review",
        payload_type="review_event",
        payload={"role": "rights_officer", "synthetic": True},
        recorded_by="system:test",
        synthetic=True,
    ).ref
    return ledger, result, (curator, rights)


class _DecisionProvider:
    def __init__(self, decision):
        self.decision = decision

    def decision_for(self, subject_ref, gate):
        assert gate == GovernanceGate.SOURCE_PROMOTION
        return self.decision


def test_synthetic_candidate_to_source_promotion_remains_nonproduction(tmp_path):
    ledger, result, evidence = _promotion_context(tmp_path)
    decision = PromotionDecision(
        subject_ref=result.candidate_refs[0],
        approved_roles=("knowledge_curator", "rights_officer"),
        evidence_refs=evidence,
        blocking_gap_refs=result.gap_refs,
        synthetic=True,
        production_eligible=False,
    )
    service = SourcePromotionService(
        bundle=load_builtin_ops_bundle(),
        ledger=ledger,
        decisions=_DecisionProvider(decision),
    )
    source_ref = service.promote(
        source_id="synthetic-medication-source",
        candidate_ref=result.candidate_refs[0],
        snapshot_ref=result.snapshot_refs[0],
        promoted_by="system:synthetic-test",
    )

    source = GovernedSourceV2.model_validate(ledger.get(source_ref).payload)
    assert source.registry_status == "synthetic_fixture"
    assert source.synthetic is True
    assert source.production_eligible is False
    assert source.access_mode == "quarantined_synthetic_fixture"
    assert source.knowledge_effect == "informational_only"
    assert source.runtime_authority == "none"
    assert source.unresolved_gap_refs == result.gap_refs
    assert len(result.gap_refs) == 2


def test_verified_phone_bearing_snapshot_digest_survives_source_promotion(tmp_path):
    ledger, result, evidence = _promotion_context(tmp_path)
    digest = hashlib.sha256(VERIFIED_ACQUISITION_DIGEST_PAYLOAD).hexdigest()
    assert digest == VERIFIED_ACQUISITION_DIGEST_SHA256
    snapshot = SourceSnapshot.model_validate(
        ledger.get(result.snapshot_refs[0]).payload
    )
    assert snapshot.quarantine_blob is not None
    verified_snapshot = snapshot.model_copy(
        update={
            "content_sha256": digest,
            "quarantine_blob": snapshot.quarantine_blob.model_copy(
                update={
                    "content_sha256": digest,
                    "relative_path": f"blobs/{digest}.bin",
                }
            ),
        }
    )
    snapshot_ref = ledger.append(
        LedgerCollection.SNAPSHOT,
        snapshot.snapshot_id,
        payload_type="source_snapshot",
        payload=verified_snapshot,
        recorded_by="system:verified-digest-fixture",
        synthetic=True,
        expected_record_version=2,
    ).ref
    decision = PromotionDecision(
        subject_ref=result.candidate_refs[0],
        approved_roles=("knowledge_curator", "rights_officer"),
        evidence_refs=evidence,
        blocking_gap_refs=result.gap_refs,
        synthetic=True,
        production_eligible=False,
    )
    source_ref = SourcePromotionService(
        bundle=load_builtin_ops_bundle(),
        ledger=ledger,
        decisions=_DecisionProvider(decision),
    ).promote(
        source_id="synthetic-phone-bearing-digest-source",
        candidate_ref=result.candidate_refs[0],
        snapshot_ref=snapshot_ref,
        promoted_by="system:synthetic-test",
    )

    source = GovernedSourceV2.model_validate(ledger.get(source_ref).payload)
    assert source.content_sha256 == digest


def test_source_promotion_fails_without_all_required_roles(tmp_path):
    ledger, result, evidence = _promotion_context(tmp_path)
    decision = PromotionDecision(
        subject_ref=result.candidate_refs[0],
        approved_roles=("knowledge_curator",),
        evidence_refs=evidence,
        synthetic=True,
        production_eligible=False,
    )
    service = SourcePromotionService(
        bundle=load_builtin_ops_bundle(),
        ledger=ledger,
        decisions=_DecisionProvider(decision),
    )

    with pytest.raises(KnowledgeOpsPolicyError, match="required roles"):
        service.promote(
            source_id="blocked-source",
            candidate_ref=result.candidate_refs[0],
            snapshot_ref=result.snapshot_refs[0],
            promoted_by="system:test",
        )


def test_source_promotion_rejects_non_review_event_evidence(tmp_path):
    ledger, result, _ = _promotion_context(tmp_path)
    decision = PromotionDecision(
        subject_ref=result.candidate_refs[0],
        approved_roles=("knowledge_curator", "rights_officer"),
        evidence_refs=(result.snapshot_refs[0],),
        synthetic=True,
        production_eligible=False,
    )
    service = SourcePromotionService(
        bundle=load_builtin_ops_bundle(),
        ledger=ledger,
        decisions=_DecisionProvider(decision),
    )

    with pytest.raises(KnowledgeOpsPolicyError, match="review events"):
        service.promote(
            source_id="blocked-source",
            candidate_ref=result.candidate_refs[0],
            snapshot_ref=result.snapshot_refs[0],
            promoted_by="system:test",
        )


def test_source_promotion_revalidates_snapshot_url_identity(tmp_path):
    ledger, result, evidence = _promotion_context(tmp_path)
    original = SourceSnapshot.model_validate(
        ledger.get(result.snapshot_refs[0]).payload
    )
    changed_payload = original.model_dump(mode="json")
    changed_payload["canonical_url"] = (
        "https://www.nmpa.gov.cn/synthetic-offline-fixture/other"
    )
    changed = SourceSnapshot.model_validate(changed_payload)
    changed_ref = ledger.append(
        LedgerCollection.SNAPSHOT,
        original.snapshot_id,
        payload_type="source_snapshot",
        payload=changed,
        recorded_by="system:test",
        synthetic=True,
    ).ref
    decision = PromotionDecision(
        subject_ref=result.candidate_refs[0],
        approved_roles=("knowledge_curator", "rights_officer"),
        evidence_refs=evidence,
        blocking_gap_refs=result.gap_refs,
        synthetic=True,
        production_eligible=False,
    )
    service = SourcePromotionService(
        bundle=load_builtin_ops_bundle(),
        ledger=ledger,
        decisions=_DecisionProvider(decision),
    )

    with pytest.raises(KnowledgeOpsPolicyError, match="differs"):
        service.promote(
            source_id="blocked-url-source",
            candidate_ref=result.candidate_refs[0],
            snapshot_ref=changed_ref,
            promoted_by="system:test",
        )


def test_source_promotion_fails_when_decision_provider_has_no_decision(tmp_path):
    ledger, result, _ = _promotion_context(tmp_path)
    service = SourcePromotionService(
        bundle=load_builtin_ops_bundle(),
        ledger=ledger,
        decisions=_DecisionProvider(None),
    )

    with pytest.raises(KnowledgeOpsPolicyError, match="lacks a review decision"):
        service.promote(
            source_id="blocked-source",
            candidate_ref=result.candidate_refs[0],
            snapshot_ref=result.snapshot_refs[0],
            promoted_by="system:test",
        )


def test_production_source_promotion_rejects_synthetic_evidence(tmp_path):
    ledger, result, evidence = _promotion_context(tmp_path)
    decision = PromotionDecision(
        subject_ref=result.candidate_refs[0],
        approved_roles=("knowledge_curator", "rights_officer"),
        evidence_refs=evidence,
        synthetic=True,
        production_eligible=False,
    )
    service = SourcePromotionService(
        bundle=load_builtin_ops_bundle(),
        ledger=ledger,
        decisions=_DecisionProvider(decision),
        environment=AcquisitionEnvironment.PRODUCTION,
    )
    before = _ledger_state(ledger)

    with pytest.raises(KnowledgeOpsPolicyError, match="rejects synthetic evidence"):
        service.promote(
            source_id="blocked-production-source",
            candidate_ref=result.candidate_refs[0],
            snapshot_ref=result.snapshot_refs[0],
            promoted_by="system:test",
        )
    assert _ledger_state(ledger) == before


def test_non_synthetic_production_promotion_still_reports_persistent_readiness_gap(
    tmp_path,
):
    ledger, result, _synthetic_evidence = _promotion_context(tmp_path)
    original_candidate = SourceCandidate.model_validate(
        ledger.get(result.candidate_refs[0]).payload
    )
    candidate = SourceCandidate.model_validate(
        {
            **original_candidate.model_dump(mode="json"),
            "candidate_id": "production-readiness-candidate",
            "request_id": "production-readiness-probe",
            "synthetic": False,
        }
    )
    candidate_ref = ledger.append(
        LedgerCollection.CANDIDATE,
        candidate.candidate_id,
        payload_type="source_candidate",
        payload=candidate,
        recorded_by="system:nonproduction-readiness-fixture",
        synthetic=False,
    ).ref
    original_snapshot = SourceSnapshot.model_validate(
        ledger.get(result.snapshot_refs[0]).payload
    )
    snapshot = SourceSnapshot.model_validate(
        {
            **original_snapshot.model_dump(mode="json"),
            "snapshot_id": "production-readiness-snapshot",
            "candidate_ref": candidate_ref.model_dump(mode="json"),
            "storage": "metadata_only",
            "quarantine_blob": None,
            "synthetic": False,
        }
    )
    snapshot_ref = ledger.append(
        LedgerCollection.SNAPSHOT,
        snapshot.snapshot_id,
        payload_type="source_snapshot",
        payload=snapshot,
        recorded_by="system:nonproduction-readiness-fixture",
        synthetic=False,
    ).ref
    evidence = tuple(
        ledger.append(
            LedgerCollection.REVIEW_EVENT,
            f"nonproduction-{role}-readiness-fixture",
            payload_type="review_event",
            payload={"role": role, "synthetic": False},
            recorded_by="system:nonproduction-readiness-fixture",
            synthetic=False,
        ).ref
        for role in ("knowledge-curator", "rights-officer")
    )
    decision = PromotionDecision(
        subject_ref=candidate_ref,
        approved_roles=("knowledge_curator", "rights_officer"),
        evidence_refs=evidence,
        synthetic=False,
        production_eligible=False,
    )
    service = SourcePromotionService(
        bundle=load_builtin_ops_bundle(),
        ledger=ledger,
        decisions=_DecisionProvider(decision),
        environment=AcquisitionEnvironment.PRODUCTION,
    )
    before = _ledger_state(ledger)

    with pytest.raises(
        KnowledgeOpsPolicyError,
        match="production Source promotion is blocked by persistent readiness Gaps",
    ):
        service.promote(
            source_id="blocked-nonsynthetic-production-source",
            candidate_ref=candidate_ref,
            snapshot_ref=snapshot_ref,
            promoted_by="system:test",
        )

    assert _ledger_state(ledger) == before


def test_source_promotion_rejects_stale_candidate_version(tmp_path):
    service, ledger, _ = _service(tmp_path)
    first = service.run(
        _request("fixture-medication-followup", request_id="stale-1")
    )
    service.run(_request("fixture-medication-followup", request_id="stale-2"))
    event = ledger.append(
        LedgerCollection.REVIEW_EVENT,
        "stale-review",
        payload_type="review_event",
        payload={"synthetic": True},
        recorded_by="system:test",
        synthetic=True,
    ).ref
    decision = PromotionDecision(
        subject_ref=first.candidate_refs[0],
        approved_roles=("knowledge_curator", "rights_officer"),
        evidence_refs=(event,),
        synthetic=True,
        production_eligible=False,
    )
    promotion = SourcePromotionService(
        bundle=load_builtin_ops_bundle(),
        ledger=ledger,
        decisions=_DecisionProvider(decision),
    )

    with pytest.raises(KnowledgeOpsPolicyError, match="stale SourceCandidate"):
        promotion.promote(
            source_id="stale-source",
            candidate_ref=first.candidate_refs[0],
            snapshot_ref=first.snapshot_refs[0],
            promoted_by="system:test",
        )


def test_source_promotion_has_no_clinical_claim_or_binding_automation():
    public_methods = {name for name in dir(SourcePromotionService) if not name.startswith("_")}

    assert public_methods == {"promote"}
