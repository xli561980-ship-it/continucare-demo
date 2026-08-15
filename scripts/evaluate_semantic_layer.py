"""Offline contract evaluation for the synthetic Layer-3 semantic baseline."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

from continucare.adapters.sqlite_store import SQLiteStore
from continucare.care_agent import CareAgentService
from continucare.care_agent.release import LAYER3_RELEASE
from continucare.care_agent.model_api import (
    SemanticModelConfig,
    UnconfiguredModelAdapter,
)
from continucare.care_engine import CareEngine
from continucare.demo_data import DEMO_PATIENT_ID


CASES = Path(__file__).parents[1] / "tests" / "fixtures" / "semantic_cases_v1.json"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the frozen offline Layer-3 synthetic contract evaluation."
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    totals = {
        "cases": len(cases),
        "status_exact": 0,
        "link_set_exact": 0,
        "clarification_count_exact": 0,
        "zero_safety_violations": 0,
    }
    details = []
    with tempfile.TemporaryDirectory(prefix="continucare-layer3-") as directory:
        for index, case in enumerate(cases):
            store = SQLiteStore(Path(directory) / f"case-{index}.db")
            engine = CareEngine(store)
            session = engine.start_or_resume(DEMO_PATIENT_ID)
            result = CareAgentService(
                store,
                care_engine=engine,
                model_adapter=UnconfiguredModelAdapter(SemanticModelConfig()),
            ).analyze(
                session.session_id,
                case["text"],
            ).result
            actual_links = sorted(item.link_id for item in result.candidates)
            checks = {
                "status_exact": result.status.value == case["expected_status"],
                "link_set_exact": actual_links == sorted(case["expected_links"]),
                "clarification_count_exact": (
                    len(result.clarifications) == case["expected_clarifications"]
                ),
                "zero_safety_violations": not result.safety_violations,
            }
            for name, passed in checks.items():
                totals[name] += int(passed)
            details.append(
                {
                    "case_id": case["case_id"],
                    "passed": all(checks.values()),
                    "checks": checks,
                }
            )
    output = {
        "release": LAYER3_RELEASE.as_dict(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "evaluation_scope": "synthetic_contract_regression_not_clinical_performance",
        "policy_version": "semantic_cases_v1",
        "totals": totals,
        "all_passed": all(item["passed"] for item in details),
        "details": details,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(output, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(output, ensure_ascii=False, indent=2))
    raise SystemExit(0 if output["all_passed"] else 1)


if __name__ == "__main__":
    main()
