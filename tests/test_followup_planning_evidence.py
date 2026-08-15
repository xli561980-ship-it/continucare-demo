from __future__ import annotations

import json
from pathlib import Path

import pytest

from continucare.followup_planning import (
    EvidenceSnapshotFormatError,
    EvidenceSnapshotIntegrityError,
    JsonEvidenceSnapshotReader,
)


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "followup_planning"
    / "evidence_snapshots.json"
)
EXPECTED_PAYLOAD_SHA256 = (
    "5092e39c3a8fb8a18357c53c138974dc4d781ee680a081c765bab91b1b2fdcd2"
)


def test_external_digest_authenticates_payload_and_snapshot_hash_is_derived():
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert "sha256" not in raw
    assert "digest" not in raw

    reader = JsonEvidenceSnapshotReader(
        FIXTURE, expected_payload_sha256=EXPECTED_PAYLOAD_SHA256
    )
    snapshot = reader.lookup_exact("synthetic-topic-nausea")

    assert reader.payload_sha256 == EXPECTED_PAYLOAD_SHA256
    assert snapshot is not None
    assert len(snapshot.snapshot_hash) == 64
    assert snapshot.snapshot_hash != EXPECTED_PAYLOAD_SHA256
    assert snapshot.knowledge_effect == "informational_only"
    assert snapshot.runtime_authority == "none"


def test_tampered_payload_fails_external_digest_check(tmp_path):
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["payload"]["snapshots"][0]["statement_snapshot"] += " 篡改。"
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(EvidenceSnapshotIntegrityError, match="external expected"):
        JsonEvidenceSnapshotReader(
            tampered, expected_payload_sha256=EXPECTED_PAYLOAD_SHA256
        )


def test_wrong_external_digest_is_rejected_for_unchanged_payload():
    wrong_digest = "0" * 64
    with pytest.raises(EvidenceSnapshotIntegrityError, match="digest"):
        JsonEvidenceSnapshotReader(FIXTURE, expected_payload_sha256=wrong_digest)


@pytest.mark.parametrize(
    "content",
    (
        "{not-json",
        json.dumps({"schema_version": "1.0", "payload": {"snapshots": []}}),
        json.dumps(
            {
                "schema_version": "1.0",
                "payload": {"snapshots": [{"unexpected": "shape"}]},
            }
        ),
    ),
)
def test_invalid_json_or_schema_has_format_error_not_integrity_error(
    tmp_path, content
):
    invalid = tmp_path / "invalid.json"
    invalid.write_text(content, encoding="utf-8")

    with pytest.raises(EvidenceSnapshotFormatError):
        JsonEvidenceSnapshotReader(
            invalid, expected_payload_sha256=EXPECTED_PAYLOAD_SHA256
        )


def test_lookup_is_exact_and_preserves_not_assessed():
    reader = JsonEvidenceSnapshotReader(
        FIXTURE, expected_payload_sha256=EXPECTED_PAYLOAD_SHA256
    )

    assert reader.lookup_exact("synthetic-topic-nausea").review_status == "not_assessed"
    assert reader.lookup_exact("Synthetic-topic-nausea") is None
    assert reader.lookup_exact("synthetic-topic-nausea ") is None
    assert reader.lookup_exact("nausea") is None
