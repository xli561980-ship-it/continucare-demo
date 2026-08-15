"""Append-only, hash-chained filesystem ledger for staged knowledge objects."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator

from continucare.knowledge.ops.models import (
    KnowledgeOpsIntegrityError,
    KnowledgeOpsPolicyError,
    NonBlank,
    SafeId,
    Sha256,
    StrictModel,
)


class LedgerCollection(StrEnum):
    ACQUISITION_RUN = "acquisition_run"
    SOURCE = "source"
    SNAPSHOT = "snapshot"
    CHANGE_SET = "change_set"
    CANDIDATE = "candidate"
    EVIDENCE_CANDIDATE = "evidence_candidate"
    GAP = "gap"
    REVIEW_PACKET = "review_packet"
    REVIEW_EVENT = "review_event"
    CLAIM = "claim"
    BINDING = "binding"
    PATIENT_CONTENT = "patient_content"
    TRANSLATION = "translation"
    TERMINOLOGY_MAPPING = "terminology_mapping"
    RELEASE_CANDIDATE = "release_candidate"
    READINESS_REPORT = "readiness_report"
    RELEASE = "release"


class LedgerRef(StrictModel):
    collection: LedgerCollection
    record_id: SafeId
    record_version: int = Field(ge=1)
    entry_sha256: Sha256


class LedgerEntry(StrictModel):
    collection: LedgerCollection
    record_id: SafeId
    record_version: int = Field(ge=1)
    payload_type: SafeId
    payload: dict[str, object]
    recorded_at: datetime
    recorded_by: NonBlank
    synthetic: bool = False
    supersedes_entry_sha256: Sha256 | None = None
    entry_sha256: Sha256

    @field_validator("recorded_at")
    @classmethod
    def recorded_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("recorded_at must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_chain_edge(self) -> "LedgerEntry":
        if self.record_version == 1 and self.supersedes_entry_sha256 is not None:
            raise ValueError("ledger version 1 cannot supersede an entry")
        if self.record_version > 1 and self.supersedes_entry_sha256 is None:
            raise ValueError("ledger successor requires predecessor SHA-256")
        return self

    @property
    def ref(self) -> LedgerRef:
        return LedgerRef(
            collection=self.collection,
            record_id=self.record_id,
            record_version=self.record_version,
            entry_sha256=self.entry_sha256,
        )


class AppendOnlyLedger:
    """Versioned object ledger with no mutation or deletion API.

    Each logical object is an immutable sequence of canonical JSON files.  A
    successor embeds the digest of its immediate predecessor.  Writes use an
    exclusive file lock and an atomic hard-link, so an existing version can
    never be replaced.
    """

    def __init__(self, root: Path) -> None:
        requested = Path(root)
        requested.mkdir(parents=True, exist_ok=True)
        if requested.is_symlink():
            raise KnowledgeOpsIntegrityError("ledger root cannot be a symlink")
        self._root = requested.resolve(strict=True)
        self._records_root = self._root / "records"
        self._locks_root = self._root / ".locks"
        self._records_root.mkdir(mode=0o700, exist_ok=True)
        self._locks_root.mkdir(mode=0o700, exist_ok=True)
        if self._records_root.is_symlink() or self._locks_root.is_symlink():
            raise KnowledgeOpsIntegrityError("ledger internal directories cannot be symlinks")

    @property
    def root(self) -> Path:
        return self._root

    def append(
        self,
        collection: LedgerCollection | str,
        record_id: str,
        *,
        payload_type: str,
        payload: BaseModel | Mapping[str, object],
        recorded_by: str,
        recorded_at: datetime | None = None,
        synthetic: bool = False,
        expected_record_version: int | None = None,
    ) -> LedgerEntry:
        collection_value = LedgerCollection(collection)
        if not _safe_id(record_id):
            raise ValueError("record_id is not a safe ledger identifier")
        if not _safe_id(payload_type):
            raise ValueError("payload_type is not a safe ledger identifier")
        canonical_payload = _json_payload(payload)
        _validate_typed_collection_boundary(
            collection=collection_value,
            record_id=record_id,
            payload_type=payload_type,
            payload=canonical_payload,
            synthetic=synthetic,
        )
        timestamp = recorded_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            raise ValueError("recorded_at must include a timezone")

        lock_path = self._locks_root / f"{collection_value.value}--{record_id}.lock"
        try:
            lock_fd = os.open(
                lock_path,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except OSError as exc:
            raise KnowledgeOpsIntegrityError("ledger lock file is unsafe") from exc
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            history = self._history_unlocked(collection_value, record_id)
            predecessor = history[-1] if history else None
            if predecessor is not None and predecessor.synthetic and not synthetic:
                raise KnowledgeOpsPolicyError(
                    "synthetic ledger lineage cannot be changed to non-synthetic"
                )
            version = 1 if predecessor is None else predecessor.record_version + 1
            if expected_record_version is not None and version != expected_record_version:
                raise KnowledgeOpsIntegrityError(
                    "ledger record version changed before append"
                )
            _validate_typed_record_version(payload_type, canonical_payload, version)
            base = {
                "collection": collection_value.value,
                "record_id": record_id,
                "record_version": version,
                "payload_type": payload_type,
                "payload": canonical_payload,
                "recorded_at": timestamp.isoformat(),
                "recorded_by": recorded_by,
                "synthetic": synthetic,
                "supersedes_entry_sha256": (
                    None if predecessor is None else predecessor.entry_sha256
                ),
            }
            draft = LedgerEntry.model_validate(
                {**base, "entry_sha256": "0" * 64}
            )
            normalized_base = draft.model_dump(
                mode="json", exclude={"entry_sha256"}
            )
            digest = hashlib.sha256(_canonical_bytes(normalized_base)).hexdigest()
            entry = LedgerEntry.model_validate({**normalized_base, "entry_sha256": digest})
            target_dir = self._record_dir(collection_value, record_id, create=True)
            target = target_dir / f"{version:08d}.json"
            _atomic_create(target, _canonical_bytes(entry.model_dump(mode="json")))
            return entry
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)

    def history(
        self, collection: LedgerCollection | str, record_id: str
    ) -> tuple[LedgerEntry, ...]:
        collection_value = LedgerCollection(collection)
        if not _safe_id(record_id):
            raise ValueError("record_id is not a safe ledger identifier")
        return self._history_unlocked(collection_value, record_id)

    def head(
        self, collection: LedgerCollection | str, record_id: str
    ) -> LedgerEntry | None:
        history = self.history(collection, record_id)
        return history[-1] if history else None

    def get(self, reference: LedgerRef) -> LedgerEntry:
        history = self.history(reference.collection, reference.record_id)
        if reference.record_version > len(history):
            raise KnowledgeOpsIntegrityError("ledger reference version is unknown")
        entry = history[reference.record_version - 1]
        if entry.entry_sha256 != reference.entry_sha256:
            raise KnowledgeOpsIntegrityError("ledger reference SHA-256 mismatch")
        return entry

    def list_heads(
        self, collection: LedgerCollection | str
    ) -> tuple[LedgerEntry, ...]:
        collection_value = LedgerCollection(collection)
        collection_dir = self._records_root / collection_value.value
        if not collection_dir.exists():
            return ()
        if collection_dir.is_symlink() or not collection_dir.is_dir():
            raise KnowledgeOpsIntegrityError("invalid ledger collection directory")
        heads: list[LedgerEntry] = []
        for record_dir in sorted(collection_dir.iterdir()):
            if (
                record_dir.is_symlink()
                or not record_dir.is_dir()
                or not _safe_id(record_dir.name)
            ):
                raise KnowledgeOpsIntegrityError("invalid ledger record directory")
            history = self.history(collection_value, record_dir.name)
            if history:
                heads.append(history[-1])
        return tuple(heads)

    def verify_all(self) -> int:
        verified = 0
        for collection in LedgerCollection:
            collection_dir = self._records_root / collection.value
            if not collection_dir.exists():
                continue
            if collection_dir.is_symlink():
                raise KnowledgeOpsIntegrityError("ledger collection cannot be a symlink")
            for record_dir in sorted(collection_dir.iterdir()):
                if record_dir.is_symlink() or not record_dir.is_dir():
                    raise KnowledgeOpsIntegrityError("invalid ledger record directory")
                history = self.history(collection, record_dir.name)
                verified += len(history)
        return verified

    def _record_dir(
        self,
        collection: LedgerCollection,
        record_id: str,
        *,
        create: bool,
    ) -> Path:
        collection_dir = self._records_root / collection.value
        record_dir = collection_dir / record_id
        if create:
            for path in (collection_dir, record_dir):
                if path.is_symlink():
                    raise KnowledgeOpsIntegrityError(
                        "ledger path cannot be a symlink"
                    )
                try:
                    path.mkdir(mode=0o700)
                except FileExistsError:
                    pass
                if path.is_symlink() or not path.is_dir():
                    raise KnowledgeOpsIntegrityError(
                        "ledger path must be a regular directory"
                    )
        else:
            for path in (collection_dir, record_dir):
                if path.exists() and path.is_symlink():
                    raise KnowledgeOpsIntegrityError(
                        "ledger path cannot be a symlink"
                    )
        return record_dir

    def _history_unlocked(
        self, collection: LedgerCollection, record_id: str
    ) -> tuple[LedgerEntry, ...]:
        record_dir = self._record_dir(collection, record_id, create=False)
        if not record_dir.exists():
            return ()
        if record_dir.is_symlink() or not record_dir.is_dir():
            raise KnowledgeOpsIntegrityError("invalid ledger record path")
        paths: list[Path] = []
        for child in sorted(record_dir.iterdir()):
            if (
                not child.is_symlink()
                and child.is_file()
                and re.fullmatch(r"\.\d{8}\.json\.[0-9a-f]{24}\.tmp", child.name)
            ):
                continue
            if child.is_symlink() or not child.is_file() or not child.name.endswith(".json"):
                raise KnowledgeOpsIntegrityError(
                    "ledger record directory contains an unexpected entry"
                )
            paths.append(child)
        expected_names = [f"{index:08d}.json" for index in range(1, len(paths) + 1)]
        if [path.name for path in paths] != expected_names:
            raise KnowledgeOpsIntegrityError("ledger versions must be contiguous")
        entries: list[LedgerEntry] = []
        for expected_version, path in enumerate(paths, start=1):
            if path.is_symlink() or not path.is_file():
                raise KnowledgeOpsIntegrityError("ledger entry must be a regular file")
            try:
                entry = LedgerEntry.model_validate_json(path.read_bytes())
            except Exception as exc:
                raise KnowledgeOpsIntegrityError(
                    f"invalid ledger entry {path.name}: {exc}"
                ) from exc
            if (
                entry.collection != collection.value
                or entry.record_id != record_id
                or entry.record_version != expected_version
            ):
                raise KnowledgeOpsIntegrityError("ledger entry identity mismatch")
            digest = hashlib.sha256(
                _canonical_bytes(
                    entry.model_dump(mode="json", exclude={"entry_sha256"})
                )
            ).hexdigest()
            if digest != entry.entry_sha256:
                raise KnowledgeOpsIntegrityError("ledger entry SHA-256 mismatch")
            predecessor = entries[-1] if entries else None
            expected_predecessor = (
                None if predecessor is None else predecessor.entry_sha256
            )
            if entry.supersedes_entry_sha256 != expected_predecessor:
                raise KnowledgeOpsIntegrityError("ledger predecessor chain mismatch")
            entries.append(entry)
        return tuple(entries)


def _safe_id(value: str) -> bool:
    if not value or len(value) > 128 or not value[0].isalnum():
        return False
    return value.isascii() and all(character.isalnum() or character in "._-" for character in value)


def _json_payload(payload: BaseModel | Mapping[str, object]) -> dict[str, object]:
    raw = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else dict(payload)
    try:
        serialized = json.dumps(
            raw,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        normalized = json.loads(serialized)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"ledger payload must be canonical JSON: {exc}") from exc
    if not isinstance(normalized, dict):
        raise ValueError("ledger payload must be a JSON object")
    return normalized


def _validate_typed_collection_boundary(
    *,
    collection: LedgerCollection,
    record_id: str,
    payload_type: str,
    payload: dict[str, object],
    synthetic: bool,
) -> None:
    evidence_type = "evidence_candidate_v2"
    draft_type = "machine_draft_claim_v2"
    record_type = payload.get("record_type")
    if record_type == "evidence_candidate" and payload_type != evidence_type:
        raise KnowledgeOpsPolicyError(
            "EvidenceCandidate record_type requires evidence_candidate_v2"
        )
    if (
        record_type == "evidence_candidate"
        and collection != LedgerCollection.EVIDENCE_CANDIDATE
    ):
        raise KnowledgeOpsPolicyError(
            "EvidenceCandidate record may only be stored in EVIDENCE_CANDIDATE"
        )
    if record_type == "machine_draft_claim":
        if payload_type != draft_type:
            raise KnowledgeOpsPolicyError(
                "MachineDraftClaim record_type requires machine_draft_claim_v2"
            )
        if collection != LedgerCollection.CLAIM:
            raise KnowledgeOpsPolicyError(
                "MachineDraftClaim record may only be stored in CLAIM"
            )
    if collection == LedgerCollection.EVIDENCE_CANDIDATE:
        if payload_type != evidence_type:
            raise KnowledgeOpsPolicyError(
                "EVIDENCE_CANDIDATE collection requires evidence_candidate_v2"
            )
        if payload.get("record_type") != "evidence_candidate":
            raise KnowledgeOpsPolicyError(
                "EvidenceCandidate record_type must be evidence_candidate"
            )
        if not record_id.startswith("evc-") or payload.get("candidate_id") != record_id:
            raise KnowledgeOpsPolicyError(
                "EvidenceCandidate ledger identity requires the evc- namespace"
            )
    elif payload_type == evidence_type:
        raise KnowledgeOpsPolicyError(
            "evidence_candidate_v2 may only be stored in EVIDENCE_CANDIDATE"
        )

    if payload_type == draft_type:
        if collection != LedgerCollection.CLAIM:
            raise KnowledgeOpsPolicyError(
                "machine_draft_claim_v2 may only be stored in CLAIM"
            )
        if payload.get("record_type") != "machine_draft_claim":
            raise KnowledgeOpsPolicyError(
                "MachineDraftClaim record_type must be machine_draft_claim"
            )
        if not record_id.startswith("dcl-") or payload.get("claim_id") != record_id:
            raise KnowledgeOpsPolicyError(
                "MachineDraftClaim ledger identity requires the dcl- namespace"
            )

    if payload_type in {evidence_type, draft_type}:
        if payload.get("synthetic") is not synthetic:
            raise KnowledgeOpsPolicyError(
                "typed payload synthetic status must equal its ledger lineage"
            )
        author = payload.get("author_provenance")
        if not isinstance(author, dict) or author.get("synthetic") is not synthetic:
            raise KnowledgeOpsPolicyError(
                "typed payload author provenance must preserve synthetic lineage"
            )


def _validate_typed_record_version(
    payload_type: str,
    payload: dict[str, object],
    ledger_version: int,
) -> None:
    field = {
        "evidence_candidate_v2": "candidate_version",
        "machine_draft_claim_v2": "claim_version",
    }.get(payload_type)
    if field is not None and payload.get(field) != ledger_version:
        raise KnowledgeOpsPolicyError(
            f"{payload_type} {field} must equal its ledger record version"
        )


def _canonical_bytes(value: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _atomic_create(target: Path, payload: bytes) -> None:
    if target.exists():
        raise FileExistsError(f"append-only ledger target already exists: {target.name}")
    temporary = target.parent / f".{target.name}.{secrets.token_hex(12)}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
