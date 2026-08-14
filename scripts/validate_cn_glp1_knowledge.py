"""Validate the CN GLP-1 L1 release and its downloaded source files."""

from __future__ import annotations

import argparse
import csv
import io
import zipfile
from pathlib import Path

from continucare.knowledge import (
    load_cn_glp1_release,
    validate_release,
    validate_runtime_artifacts,
)


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "continucare/knowledge/data/cn_glp1/v1"
LOINC_ARCHIVE = ROOT / "output/clinical-source-pack-2026-08-13/data/Loinc_2.82.zip"


def validate_loinc_archive(path: Path) -> list[str]:
    expected = {
        "81660-3": "Nausea [Presence]",
        "94070-0": "Emesis count 24 hour",
        "75301-2": "Fluid intake 24 hour Estimated",
    }
    found: dict[str, str] = {}
    with zipfile.ZipFile(path) as archive:
        with archive.open("LoincTable/Loinc.csv") as raw:
            reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))
            for row in reader:
                code = row["LOINC_NUM"]
                if code in expected:
                    found[code] = row["LONG_COMMON_NAME"]
                    if len(found) == len(expected):
                        break
    missing = set(expected) - set(found)
    mismatched = {
        code: (expected[code], found.get(code))
        for code in expected
        if code in found and found[code] != expected[code]
    }
    if missing or mismatched:
        raise ValueError(f"LOINC validation failed: missing={missing}, mismatched={mismatched}")
    return [f"LOINC 2.82 verified: {code} {display}" for code, display in found.items()]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--skip-source-files",
        action="store_true",
        help="validate the packaged release without the external download pack",
    )
    args = parser.parse_args()
    registry = load_cn_glp1_release()
    messages = validate_release(
        registry.release,
        data_dir=DATA_DIR,
        repository_root=None if args.skip_source_files else ROOT,
    )
    messages.extend(validate_runtime_artifacts(registry.release))
    if not args.skip_source_files:
        messages.extend(validate_loinc_archive(LOINC_ARCHIVE))
    for message in messages:
        print(message)


if __name__ == "__main__":
    main()
