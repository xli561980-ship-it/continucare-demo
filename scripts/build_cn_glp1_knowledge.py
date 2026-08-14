"""Build deterministic runtime artifacts from the CN GLP-1 L1 release."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

from continucare.knowledge import (
    compile_knowledge_release,
    compile_observation_mappings,
    compile_plan_definition,
    compile_questionnaire,
)


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "continucare/knowledge/data/cn_glp1/v1"
DATA_HASH_FIELDS = {
    "source_registry_sha256": "source_registry.json",
    "product_registry_sha256": "product_registry.json",
    "evidence_claims_sha256": "evidence_claims.json",
    "metric_definitions_sha256": "metric_definitions.json",
    "terminology_manifest_sha256": "terminology_manifest.json",
    "patient_content_sha256": "patient_content.zh-CN.json",
    "data_quality_rules_sha256": "data_quality_rules.json",
    "clinical_rules_sha256": "clinical_rules.json",
    "coverage_report_sha256": "coverage_report.json",
}
ARTIFACT_HASH_FIELDS = {
    "glp1_14d_questionnaire.json": "questionnaire_sha256",
    "glp1_14d_plan_definition.json": "plan_definition_sha256",
    "glp1_14d_observation_mapping.json": "observation_mapping_sha256",
}


def encoded(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def artifacts() -> dict[str, object]:
    release = compile_knowledge_release()
    return {
        "release.json": release.model_dump(mode="json"),
        "glp1_14d_questionnaire.json": compile_questionnaire(),
        "glp1_14d_plan_definition.json": compile_plan_definition(),
        "glp1_14d_observation_mapping.json": compile_observation_mappings(),
    }


def prepare_manifest(
    *, created_at: str, output: Path, candidate_release_id: str
) -> None:
    """Refresh deterministic digests for a not-yet-published release candidate."""

    manifest_path = DATA_DIR / "release_manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    coverage_path = DATA_DIR / "coverage_report.json"
    coverage_bytes = coverage_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    datetime.fromisoformat(created_at)
    current_release_id = manifest["release_id"]
    if manifest.get("status") != "draft_candidate":
        raise SystemExit(
            "refusing to prepare the tracked engineering_validated release in place; "
            "first bump all release references and mark the new manifest draft_candidate"
        )
    if candidate_release_id != current_release_id:
        raise SystemExit(
            "--candidate-release-id must match the tracked draft_candidate release_id"
        )
    if (output / "release.json").is_file():
        raise SystemExit("refusing to prepare an already-published output directory")
    expected_output_name = candidate_release_id
    if output.name != expected_output_name:
        raise SystemExit(
            "candidate output directory must end with the candidate release_id"
        )
    manifest["created_at"] = created_at
    manifest["status"] = "engineering_validated"
    coverage = json.loads(coverage_bytes)
    coverage["release_id"] = candidate_release_id
    coverage["report_id"] = f"{candidate_release_id}-coverage"
    coverage["generated_at"] = created_at
    coverage_path.write_bytes(encoded(coverage))
    for field, name in DATA_HASH_FIELDS.items():
        manifest[field] = hashlib.sha256((DATA_DIR / name).read_bytes()).hexdigest()

    # Artifact compilers only need the registries; temporarily write the data
    # digests so the strict loader can construct the release candidate.
    manifest_path.write_bytes(encoded(manifest))
    try:
        compiled = {
            "questionnaire_sha256": compile_questionnaire(),
            "plan_definition_sha256": compile_plan_definition(),
            "observation_mapping_sha256": compile_observation_mappings(),
        }
    except Exception:
        manifest_path.write_bytes(manifest_bytes)
        coverage_path.write_bytes(coverage_bytes)
        raise
    for field, value in compiled.items():
        manifest[field] = hashlib.sha256(encoded(value)).hexdigest()
    manifest_path.write_bytes(encoded(manifest))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--prepare-release",
        metavar="ISO_TIMESTAMP",
        help="prepare an explicitly named, never-published release candidate",
    )
    parser.add_argument(
        "--candidate-release-id",
        help="new release id required with --prepare-release",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.prepare_release:
        current_release_id = json.loads(
            (DATA_DIR / "release_manifest.json").read_text("utf-8")
        )["release_id"]
        if not args.candidate_release_id:
            raise SystemExit(
                "--prepare-release requires a new --candidate-release-id; "
                "the tracked release is immutable"
            )
        manifest_status = json.loads(
            (DATA_DIR / "release_manifest.json").read_text("utf-8")
        )["status"]
        if manifest_status != "draft_candidate":
            raise SystemExit(
                "refusing to prepare the tracked engineering_validated release in "
                "place; create a new draft_candidate release first"
            )
        if args.candidate_release_id != current_release_id:
            raise SystemExit(
                "--candidate-release-id must match the tracked draft_candidate "
                "release_id"
            )
        intended_output = (
            args.output or ROOT / "output" / args.candidate_release_id
        )
        prepare_manifest(
            created_at=args.prepare_release,
            output=intended_output,
            candidate_release_id=args.candidate_release_id,
        )
        print(f"prepared release manifest at {args.prepare_release}")
    built = artifacts()
    if args.output is None:
        args.output = ROOT / "output" / built["release.json"]["manifest"]["release_id"]
    manifest = built["release.json"]["manifest"]
    errors: list[str] = []
    for name, field in ARTIFACT_HASH_FIELDS.items():
        actual = hashlib.sha256(encoded(built[name])).hexdigest()
        if manifest[field] != actual:
            errors.append(f"manifest {field} does not match compiled {name}")
    if args.check:
        for name, value in built.items():
            path = args.output / name
            if not path.is_file():
                errors.append(f"published artifact missing: {path}")
            elif path.read_bytes() != encoded(value):
                errors.append(f"published artifact drift: {path}")
        if errors:
            raise SystemExit("build check failed:\n- " + "\n- ".join(errors))
        print(f"{len(built)} artifacts match manifest and published output")
        return
    if errors:
        raise SystemExit("build failed:\n- " + "\n- ".join(errors))
    if args.output.exists():
        for name, value in built.items():
            path = args.output / name
            if path.is_file() and path.read_bytes() != encoded(value):
                raise SystemExit(
                    "refusing to overwrite an existing release with changed content; "
                    "bump release_id and output directory"
                )
    args.output.mkdir(parents=True, exist_ok=True)
    for name, value in built.items():
        (args.output / name).write_bytes(encoded(value))
        print(args.output / name)


if __name__ == "__main__":
    main()
