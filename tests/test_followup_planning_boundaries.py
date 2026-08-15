from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

from continucare.followup_planning import SQLiteFollowupPlanningRepository


PACKAGE = Path(__file__).parents[1] / "continucare" / "followup_planning"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_followup_planning_has_no_direct_forbidden_module_imports():
    forbidden = (
        "continucare.knowledge",
        "continucare.pathways",
        "continucare.case_intake",
        "continucare.db",
        "continucare.pages",
    )
    imports = {
        module
        for path in PACKAGE.glob("*.py")
        for module in _imports(path)
    }

    assert not {
        module
        for module in imports
        if any(module == prefix or module.startswith(f"{prefix}.") for prefix in forbidden)
    }


def test_repository_schema_contains_no_clinical_or_delivery_resource_tables():
    connection = sqlite3.connect(":memory:")
    SQLiteFollowupPlanningRepository(connection).initialize()
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }

    assert tables
    assert all(name.startswith("followup_planning_") for name in tables)
    assert tables.isdisjoint(
        {
            "QuestionnaireResponse",
            "Observation",
            "Task",
            "Alert",
            "Communication",
            "ServiceRequest",
        }
    )
