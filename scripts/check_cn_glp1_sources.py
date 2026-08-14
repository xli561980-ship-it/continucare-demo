"""Fail-closed integrity check for downloaded CN GLP-1 source artifacts."""

from __future__ import annotations

import argparse
from pathlib import Path

from continucare.knowledge import load_cn_glp1_release
from continucare.knowledge.validator import sha256_file


ROOT = Path(__file__).resolve().parents[1]


def check_sources(*, allow_missing_restricted: bool = False) -> list[str]:
    release = load_cn_glp1_release().release
    messages: list[str] = []
    errors: list[str] = []
    for source in release.sources:
        if not source.canonical_url:
            errors.append(f"{source.source_id}: missing canonical URL")
        if source.superseded_by and source.runtime_eligible:
            errors.append(f"{source.source_id}: superseded source is runtime eligible")
        if source.superseded_by:
            messages.append(f"{source.source_id}: retained background archive; superseded")
        if not source.local_path:
            messages.append(f"{source.source_id}: metadata-only source")
            continue
        path = ROOT / source.local_path
        if not path.is_file():
            if allow_missing_restricted and source.license_status == "restricted":
                messages.append(f"{source.source_id}: restricted local file intentionally absent")
                continue
            errors.append(f"{source.source_id}: missing {source.local_path}")
            continue
        actual = sha256_file(path)
        if actual != source.sha256:
            errors.append(f"{source.source_id}: SHA-256 mismatch")
            continue
        messages.append(f"{source.source_id}: {actual}")
    if errors:
        raise SystemExit("source integrity failed:\n- " + "\n- ".join(errors))
    return messages


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--allow-missing-restricted",
        action="store_true",
        help="permit MAH package inserts to be absent from a distributable checkout",
    )
    args = parser.parse_args()
    for message in check_sources(allow_missing_restricted=args.allow_missing_restricted):
        print(message)


if __name__ == "__main__":
    main()
