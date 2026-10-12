"""Offline only: default catalog export; plan/score require explicit local JSON inputs.

Examples:
  python scripts/autonomy_benchmark.py
  python scripts/autonomy_benchmark.py plan --environment environment.json
  python scripts/autonomy_benchmark.py template --manifest manifest.json --run-id front_block.r1
  python scripts/autonomy_benchmark.py score --manifest manifest.json --results runs.json

Outputs UTF-8 JSON to stdout. Does not write files, inspect live settings, connect
to CAD, invoke a model, or run the 30 planned sessions. Score exit 0 means all 30
real-model/real-CAD unassisted runs passed; exit 1 means incomplete/not passed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sw_assistant.validation.autonomy_benchmark import catalog, create_manifest, record_template, score


def _read(path: str, maximum: int) -> Any:
    with Path(path).open("rb") as stream:
        value = stream.read(maximum + 1)
    if len(value) > maximum:
        raise ValueError("input JSON exceeds its fixed size limit")
    return json.loads(value.decode("utf-8-sig"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("catalog", help="export ten synthetic tasks; execute nothing")
    plan = commands.add_parser("plan", help="freeze 30 planned slots for an explicit environment")
    plan.add_argument("--environment", required=True)
    template = commands.add_parser("template", help="export an unverified record with no fabricated observations")
    template.add_argument("--manifest", required=True)
    template.add_argument("--run-id", required=True)
    evaluate = commands.add_parser("score", help="audit supplied results without running anything")
    evaluate.add_argument("--manifest", required=True)
    evaluate.add_argument("--results", required=True)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "plan":
            result = create_manifest(_read(arguments.environment, 16_384))
        elif arguments.command == "score":
            result = score(_read(arguments.manifest, 65_536), _read(arguments.results, 2_000_000))
        elif arguments.command == "template":
            result = record_template(_read(arguments.manifest, 65_536), arguments.run_id)
        else:
            result = catalog()
    except (OSError, ValueError, TypeError, RecursionError):
        print(json.dumps({"error": "Invalid/unreadable local benchmark input; nothing was executed."}), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return int(arguments.command == "score" and not result["campaign_passed"])


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
