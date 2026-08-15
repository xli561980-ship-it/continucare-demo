from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def _file_evidence(path: Path) -> tuple[str, int, int] | None:
    if not path.exists():
        return None
    payload = path.read_bytes()
    stat = path.stat()
    return hashlib.sha256(payload).hexdigest(), stat.st_size, stat.st_mtime_ns


def test_default_unset_subprocess_imports_and_factories_use_zero_network_or_workspace_db(
    tmp_path: Path,
) -> None:
    root = Path(__file__).parents[2]
    workspace_db = root / "data" / "continucare.db"
    before_db = _file_evidence(workspace_db)
    before_status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    child_temp = tmp_path / "child"
    child_temp.mkdir()
    script = r'''
import json
import os
import socket
import sqlite3
import ssl
from pathlib import Path

# The existing v1 Knowledge import chain loads urllib3, whose module import
# performs one local IPv6-capability socket construction.  Preload that
# third-party module before installing the P1 guard so this subprocess proves
# the incremental Knowledge Ops imports/factories themselves are zero-socket.
import urllib3.util.connection

class ZeroNetworkViolation(RuntimeError):
    pass

class ZeroWorkspaceDatabaseViolation(RuntimeError):
    pass

calls = {"socket": 0, "sqlite": 0}
allowed_root = Path(os.environ["P1_CHILD_TEMP_ROOT"]).resolve(strict=True)
real_sqlite_connect = sqlite3.connect

def reject_network(*args, **kwargs):
    calls["socket"] += 1
    raise ZeroNetworkViolation("unexpected socket call")

def guarded_sqlite(database, *args, **kwargs):
    target = Path(database).resolve()
    if target != allowed_root and allowed_root not in target.parents:
        raise ZeroWorkspaceDatabaseViolation(str(target))
    calls["sqlite"] += 1
    return real_sqlite_connect(database, *args, **kwargs)

socket.socket = reject_network
socket.create_connection = reject_network
ssl.SSLContext.wrap_socket = reject_network
sqlite3.connect = guarded_sqlite

import continucare
import continucare.knowledge.ops
from continucare.knowledge.ops import GuardedHttpConnector, load_builtin_ops_bundle
from continucare.knowledge.ops.read_model import load_builtin_ops_read_model
from continucare.knowledge.ops.source_connectors import DailyMedConnector
from continucare.knowledge.ops.source_connectors.live_validation import run_live_validation

bundle = load_builtin_ops_bundle()
read_model = load_builtin_ops_read_model()
GuardedHttpConnector()
DailyMedConnector()
report = run_live_validation(external_egress_enabled=False, environ={})
assert bundle.index.bundle_version == 4
assert read_model.boundary.runtime_authority == "none"
assert report.request_count == 0
assert {item.status for item in report.records} == {"not_attempted"}
assert calls == {"socket": 0, "sqlite": 0}
print(json.dumps(calls, sort_keys=True))
'''
    env = os.environ.copy()
    env.pop("CONTINUCARE_KNOWLEDGE_LIVE_VALIDATION", None)
    env.update(
        {
            "CONTINUCARE_EXTERNAL_EGRESS_ENABLED": "false",
            "CONTINUCARE_DB_PATH": str(child_temp / "continucare.db"),
            "P1_CHILD_TEMP_ROOT": str(child_temp),
            "PYTHONPYCACHEPREFIX": str(child_temp / "pycache"),
        }
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == {"socket": 0, "sqlite": 0}
    assert _file_evidence(workspace_db) == before_db
    after_status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert after_status == before_status


def test_live_report_schema_never_contains_response_body_or_write_authority() -> None:
    from continucare.knowledge.ops.source_connectors.live_validation import (
        run_live_validation,
    )

    report = run_live_validation(
        external_egress_enabled=True,
        environ={"CONTINUCARE_KNOWLEDGE_LIVE_VALIDATION": "false"},
    )
    dumped = report.model_dump(mode="json")
    assert report.request_count == 0
    assert report.contains_response_body is False
    assert report.wrote_knowledge_state is False
    assert report.release_ready is False
    assert report.runtime_authority == "none"
    assert all(item.response_body_recorded is False for item in report.records)
    assert all(item.ledger_write_performed is False for item in report.records)
    serialized = json.dumps(dumped, ensure_ascii=False)
    assert '"body"' not in serialized


def test_explicit_live_validator_uses_fixed_targets_and_body_free_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from continucare.knowledge.ops.source_connectors.contracts import MetadataResponse
    from continucare.knowledge.ops.source_connectors import live_validation as live

    bodies = {
        "dailymed-spl-history-json": json.dumps(
            {
                "data": {
                    "spl": {
                        "setid": "ee06186f-2aa3-4990-a760-757579d8f77b",
                        "title": "Synthetic metadata",
                    },
                    "history": [
                        {"spl_version": "1", "published_date": "2026-08-14"}
                    ],
                }
            }
        ).encode(),
        "ema-website-medicines-json": json.dumps(
            [
                {
                    "ema_product_number": "EMEA-H-C-000001",
                    "name_of_medicine": "Synthetic",
                    "medicine_url": "https://www.ema.europa.eu/en/synthetic",
                }
            ]
        ).encode(),
        "medlineplus-health-topics-xml": (
            b'<health-topics dategenerated="2026-08-14">'
            b'<health-topic id="1" title="Synthetic" language="English" '
            b'date-created="2026-08-14" url="https://medlineplus.gov/synthetic.html"/>'
            b"</health-topics>"
        ),
        "pubmed-esummary-json": json.dumps(
            {
                "result": {
                    "uids": ["31452104"],
                    "31452104": {"title": "Synthetic bibliographic metadata"},
                }
            }
        ).encode(),
        "pmc-open-access-locator-xml": (
            b'<OA><records><record id="PMC13901" license="synthetic" retracted="no">'
            b'<link href="https://ftp.ncbi.nlm.nih.gov/synthetic"/>'
            b"</record></records></OA>"
        ),
    }
    media = {
        "dailymed-spl-history-json": "application/json",
        "ema-website-medicines-json": "application/json",
        "medlineplus-health-topics-xml": "application/xml",
        "pubmed-esummary-json": "application/json",
        "pmc-open-access-locator-xml": "application/xml",
    }

    class FakeLiveTransport:
        identity_binding_proven = True

        def __init__(self, *, permit, maximum_retries: int) -> None:
            assert permit is not None
            assert maximum_retries == 0

        def execute(self, request, endpoint):
            assert request.endpoint_id == endpoint.endpoint_id
            return MetadataResponse(
                endpoint_id=endpoint.endpoint_id,
                status=200,
                media_type=media[endpoint.endpoint_id],
                charset="utf-8",
                headers={},
                body=bodies[endpoint.endpoint_id],
                peer_ip="93.184.216.34",
            )

    monkeypatch.setattr(live, "SecureMetadataTransport", FakeLiveTransport)
    report = live.run_live_validation(
        external_egress_enabled=True,
        environ={"CONTINUCARE_KNOWLEDGE_LIVE_VALIDATION": "true"},
    )
    assert report.request_count == 5
    assert {item.status for item in report.records} == {"validated"}
    assert all(item.parsed_metadata_record_count == 1 for item in report.records)
    assert all(item.whole_response_sha256 for item in report.records)
    assert report.contains_response_body is False
    assert report.wrote_knowledge_state is False
