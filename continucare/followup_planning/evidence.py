"""Offline, exact-match evidence snapshot reader with external integrity input."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import ValidationError

from continucare.followup_planning.models import (
    EvidenceFixtureEnvelope,
    EvidenceSnapshot,
    EvidenceSnapshotPayload,
)


class EvidenceSnapshotError(RuntimeError):
    pass


class EvidenceSnapshotFormatError(EvidenceSnapshotError):
    pass


class EvidenceSnapshotIntegrityError(EvidenceSnapshotError):
    pass


def canonical_payload_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def evidence_payload_sha256(payload: EvidenceSnapshotPayload | object) -> str:
    value = (
        payload.model_dump(mode="json")
        if isinstance(payload, EvidenceSnapshotPayload)
        else payload
    )
    return hashlib.sha256(canonical_payload_bytes(value)).hexdigest()


class JsonEvidenceSnapshotReader:
    """Load a fixture whose payload digest is supplied by a trusted caller.

    No digest is read from the envelope itself. The external expected digest
    authenticates the canonical ``payload`` object; each runtime snapshot hash
    is then derived from that snapshot's canonical payload.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        expected_payload_sha256: str,
    ):
        if len(expected_payload_sha256) != 64 or any(
            character not in "0123456789abcdef"
            for character in expected_payload_sha256
        ):
            raise ValueError("expected_payload_sha256 must be a lowercase SHA-256")
        self.path = Path(path)
        self.expected_payload_sha256 = expected_payload_sha256
        self.payload_sha256: str
        self._snapshots = self._load()

    def _load(self) -> dict[str, EvidenceSnapshot]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvidenceSnapshotFormatError(
                "evidence snapshot envelope is not valid UTF-8 JSON"
            ) from exc
        try:
            envelope = EvidenceFixtureEnvelope.model_validate(raw)
        except ValidationError as exc:
            raise EvidenceSnapshotFormatError(
                "evidence snapshot envelope violates the versioned schema"
            ) from exc

        payload_value = envelope.payload.model_dump(mode="json")
        actual_digest = hashlib.sha256(
            canonical_payload_bytes(payload_value)
        ).hexdigest()
        self.payload_sha256 = actual_digest
        if actual_digest != self.expected_payload_sha256:
            raise EvidenceSnapshotIntegrityError(
                "evidence payload digest does not match the external expected digest"
            )

        snapshots: dict[str, EvidenceSnapshot] = {}
        for payload in envelope.payload.snapshots:
            snapshot = EvidenceSnapshot(
                **payload.model_dump(),
                snapshot_hash=evidence_payload_sha256(payload),
            )
            snapshots[snapshot.topic_ref.reference_id] = snapshot
        return snapshots

    def lookup_exact(self, topic_id: str) -> EvidenceSnapshot | None:
        """Return only an exact topic ID match; never normalize or approximate."""

        return self._snapshots.get(topic_id)
