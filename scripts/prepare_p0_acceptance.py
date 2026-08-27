#!/usr/bin/env python3
"""Freeze a candidate-bound request for representative Microsoft Word P0 acceptance."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from word_formula_omml.acceptance import (
    AcceptanceError,
    acceptance_observation_template,
    prepare_representative_acceptance,
)


def _candidate(value: str) -> tuple[str, Path]:
    logical_id, separator, raw_path = value.partition("=")
    if not separator or not logical_id or not raw_path:
        raise argparse.ArgumentTypeError("candidate must be LOGICAL_ID=PATH")
    return logical_id, Path(raw_path)


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--candidate", action="append", type=_candidate, required=True)
    parser.add_argument("--coverage", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="acceptance request JSON")
    parser.add_argument("--observations-output", type=Path, required=True, help="manual Word observation template")
    args = parser.parse_args()
    try:
        candidates = dict(args.candidate)
        if len(candidates) != len(args.candidate):
            raise AcceptanceError("duplicate candidate logical ID")
        request = prepare_representative_acceptance(args.job, candidates, _read_json(args.coverage))
        observations = acceptance_observation_template(request)
        _write_json(args.output, request)
        _write_json(args.observations_output, observations)
    except (AcceptanceError, OSError, json.JSONDecodeError) as error:
        print(f"P0 acceptance preparation failed: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "acceptance_request_id": request["acceptance_request_id"],
                "request": str(args.output),
                "observations": str(args.observations_output),
                "candidate_hashes": {
                    item["logical_id"]: item["candidate_sha256"] for item in request["artifacts"]
                },
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
