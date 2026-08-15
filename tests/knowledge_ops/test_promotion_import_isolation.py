from __future__ import annotations

import ast
from collections import deque
from importlib.util import resolve_name
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

_WRITE_CAPABLE_KNOWLEDGE_OPS_MODULES = frozenset(
    {
        "continucare.knowledge.ops",
        "continucare.knowledge.ops.acquisition",
        "continucare.knowledge.ops.connectors",
        "continucare.knowledge.ops.evidence",
        "continucare.knowledge.ops.promotion",
        "continucare.knowledge.ops.release",
        "continucare.knowledge.ops.review",
        "continucare.knowledge.ops.source_connectors.live_validation",
        "continucare.knowledge.ops.store",
    }
)
_RUNTIME_AND_PATHWAY_EXACT_ENTRIES = frozenset(
    {
        "continucare.agents",
        "continucare.agents.runtime",
        "continucare.care_agent",
        "continucare.care_agent.service",
        "continucare.care_engine",
        "continucare.care_engine.service",
    }
)
_RUNTIME_AND_PATHWAY_NAMESPACES = (
    "continucare.layer4",
    "continucare.pathways",
    "continucare.services",
)


def _project_modules(root: Path) -> tuple[dict[str, Path], frozenset[str]]:
    candidates: list[Path] = []
    app_module = root / "app.py"
    if app_module.is_file():
        candidates.append(app_module)
    for source_root in (root / "continucare", root / "pages"):
        if source_root.is_dir():
            candidates.extend(sorted(source_root.rglob("*.py")))

    modules: dict[str, Path] = {}
    packages: set[str] = set()
    for path in candidates:
        parts = path.relative_to(root).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
            if not parts:
                continue
            packages.add(".".join(parts))
        module = ".".join(parts)
        if module in modules:
            raise AssertionError(f"duplicate project module: {module}")
        modules[module] = path
    return modules, frozenset(packages)


def _resolved_from_base(
    node: ast.ImportFrom,
    *,
    current_module: str,
    packages: frozenset[str],
) -> str:
    if node.level == 0:
        return node.module or ""
    current_package = (
        current_module
        if current_module in packages
        else current_module.rpartition(".")[0]
    )
    if not current_package:
        raise AssertionError(
            f"relative import has no package context: {current_module}"
        )
    relative_name = "." * node.level + (node.module or "")
    try:
        return resolve_name(relative_name, current_package)
    except ImportError as exc:
        raise AssertionError(
            f"invalid relative import {relative_name!r} in {current_module}"
        ) from exc


def _module_imports(
    module: str,
    path: Path,
    *,
    modules: dict[str, Path],
    packages: frozenset[str],
) -> frozenset[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    targets: set[str] = set()
    for node in ast.walk(tree):
        candidates: list[str] = []
        if isinstance(node, ast.Import):
            candidates.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _resolved_from_base(
                node,
                current_module=module,
                packages=packages,
            )
            if base:
                candidates.append(base)
                candidates.extend(
                    f"{base}.{alias.name}"
                    for alias in node.names
                    if alias.name != "*"
                )
        targets.update(candidate for candidate in candidates if candidate in modules)
    return frozenset(targets)


def _import_graph(root: Path) -> tuple[dict[str, frozenset[str]], dict[str, Path]]:
    modules, packages = _project_modules(root)
    graph = {
        module: _module_imports(
            module,
            path,
            modules=modules,
            packages=packages,
        )
        for module, path in modules.items()
    }
    return graph, modules


def _shortest_path(
    graph: dict[str, frozenset[str]],
    roots: set[str],
    target: str,
) -> tuple[str, ...] | None:
    queue = deque((root, (root,)) for root in sorted(roots) if root in graph)
    visited = {root for root in roots if root in graph}
    while queue:
        module, path = queue.popleft()
        if module == target:
            return path
        for imported in sorted(graph[module]):
            if imported not in visited:
                visited.add(imported)
                queue.append((imported, (*path, imported)))
    return None


def _entry_roots(modules: dict[str, Path]) -> set[str]:
    roots = {
        "app",
        "continucare.knowledge",
        "continucare.knowledge.__main__",
        "continucare.knowledge.models",
        "continucare.knowledge.registry",
        "continucare.knowledge.render",
        "continucare.knowledge.resolvers",
    } | set(_RUNTIME_AND_PATHWAY_EXACT_ENTRIES)
    roots.update(module for module in modules if module.startswith("pages."))
    roots.update(
        module
        for module in modules
        if any(
            module == namespace or module.startswith(f"{namespace}.")
            for namespace in _RUNTIME_AND_PATHWAY_NAMESPACES
        )
    )
    missing = sorted(root for root in roots if root not in modules)
    assert not missing, f"declared isolation entry modules are missing: {missing}"
    return roots


def _definition_modules(
    modules: dict[str, Path], class_name: str
) -> frozenset[str]:
    definitions: set[str] = set()
    for module, path in modules.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.ClassDef) and node.name == class_name
            for node in tree.body
        ):
            definitions.add(module)
    return frozenset(definitions)


def _format_violations(violations: dict[str, tuple[str, ...]]) -> str:
    return "\n".join(
        f"{target}: {' -> '.join(path)}"
        for target, path in sorted(violations.items())
    )


def test_runtime_and_v1_read_entries_cannot_reach_knowledge_ops_writes() -> None:
    graph, modules = _import_graph(REPOSITORY_ROOT)
    definition_modules = _definition_modules(
        modules, "EvidenceCandidatePromotionService"
    )
    assert definition_modules == frozenset(
        {"continucare.knowledge.ops.evidence"}
    ), "the promotion service definition must remain uniquely identifiable"
    forbidden = _WRITE_CAPABLE_KNOWLEDGE_OPS_MODULES | definition_modules
    roots = _entry_roots(modules)
    violations = {
        target: path
        for target in forbidden
        if (path := _shortest_path(graph, roots, target)) is not None
    }
    assert not violations, _format_violations(violations)


def test_promotion_modules_cannot_reach_runtime_or_v1_read_entries() -> None:
    graph, modules = _import_graph(REPOSITORY_ROOT)
    promotion_roots = {
        "continucare.knowledge.ops.evidence",
        "continucare.knowledge.ops.promotion",
    }
    forbidden = {
        "app",
        "continucare.knowledge",
        "continucare.knowledge.registry",
        "continucare.knowledge.render",
    } | set(_RUNTIME_AND_PATHWAY_EXACT_ENTRIES)
    forbidden.update(
        module
        for module in modules
        if module.startswith("pages.")
        or any(
            module == namespace or module.startswith(f"{namespace}.")
            for namespace in _RUNTIME_AND_PATHWAY_NAMESPACES
        )
    )
    violations = {
        target: path
        for target in forbidden
        if (path := _shortest_path(graph, promotion_roots, target)) is not None
    }
    assert not violations, _format_violations(violations)


@pytest.mark.parametrize("indirect", [False, True], ids=["direct", "indirect"])
def test_import_graph_detects_forbidden_import_mutations(
    tmp_path: Path, indirect: bool
) -> None:
    files = {
        "continucare/__init__.py": "",
        "continucare/knowledge/__init__.py": "",
        "continucare/knowledge/ops/__init__.py": "",
        "continucare/knowledge/ops/promotion.py": "",
    }
    if indirect:
        files.update(
            {
                "app.py": "from continucare import gateway\n",
                "continucare/gateway/__init__.py": "from . import bridge\n",
                "continucare/gateway/bridge.py": "import continucare.layer\n",
                "continucare/layer.py": (
                    "from continucare.knowledge.ops import promotion\n"
                ),
            }
        )
    else:
        files["app.py"] = "import continucare.knowledge.ops.promotion\n"
    for relative_path, content in files.items():
        target = tmp_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    graph, _modules = _import_graph(tmp_path)
    path = _shortest_path(
        graph,
        {"app"},
        "continucare.knowledge.ops.promotion",
    )

    assert path is not None
    if indirect:
        assert path == (
            "app",
            "continucare.gateway",
            "continucare.gateway.bridge",
            "continucare.layer",
            "continucare.knowledge.ops.promotion",
        )
    else:
        assert path == ("app", "continucare.knowledge.ops.promotion")
