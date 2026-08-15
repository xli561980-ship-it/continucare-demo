"""Developer-only read-only Knowledge Evidence inspection CLI."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from continucare.knowledge.models import KnowledgeBundleError
from continucare.knowledge.registry import LoadMode, load_builtin_bundle
from continucare.knowledge.render import (
    render_pathway_knowledge,
    render_symptom_knowledge,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect registered Knowledge Evidence")
    parser.add_argument("code", nargs="?")
    parser.add_argument("--version")
    parser.add_argument("--symptom-index-id")
    parser.add_argument("--record-version", type=int)
    parser.add_argument("--historical", action="store_true")
    args = parser.parse_args(argv)
    symptom_lookup = args.symptom_index_id is not None
    if symptom_lookup and (args.code or args.version or args.record_version is None):
        print(
            "error: symptom lookup requires --symptom-index-id and --record-version only",
            file=sys.stderr,
        )
        return 2
    if not symptom_lookup and (not args.code or not args.version):
        print("error: --version is required for an exact Pathway lookup", file=sys.stderr)
        return 2
    mode = LoadMode.HISTORICAL if args.historical else LoadMode.CURRENT
    try:
        registry = load_builtin_bundle(mode=mode)
        if symptom_lookup:
            view = registry.for_symptom(args.symptom_index_id, args.record_version)
        else:
            view = registry.for_pathway(args.code, args.version)
    except KnowledgeBundleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    rendered = (
        render_symptom_knowledge(view)
        if symptom_lookup
        else render_pathway_knowledge(view)
    )
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
