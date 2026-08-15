from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run_isolated(
    script_name: str, *script_args: str
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    return subprocess.run(
        [
            sys.executable,
            "-I",
            str(PROJECT_ROOT / "scripts" / script_name),
            *script_args,
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_semantic_evaluation_script_runs_without_editable_import_path():
    result = _run_isolated("evaluate_semantic_layer.py")

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["details"]
    assert all(item["passed"] for item in report["details"])


def test_demo_rehearsal_script_runs_without_editable_import_path():
    result = _run_isolated("rehearse_demo.py")

    assert result.returncode == 0, result.stderr
    assert "rehearsal 3/3: passed" in result.stdout


def test_fhir_validation_script_runs_without_editable_import_path():
    result = _run_isolated("validate_fhir_r4.py", "--help")

    assert result.returncode == 0, result.stderr
    assert "--schema" in result.stdout
