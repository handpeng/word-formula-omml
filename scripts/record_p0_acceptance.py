#!/usr/bin/env python3
"""Bind controlled Microsoft Word observations and emit the representative W7 receipt."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from word_formula_omml.acceptance import AcceptanceError, complete_representative_acceptance


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
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--output-job", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    try:
        candidates = dict(args.candidate)
        if len(candidates) != len(args.candidate):
            raise AcceptanceError("duplicate candidate logical ID")
        validated, receipt = complete_representative_acceptance(
            args.job,
            candidates,
            _read_json(args.request),
            _read_json(args.observations),
        )
        _write_json(args.output_job, validated.to_dict())
        _write_json(args.receipt, receipt)
    except (AcceptanceError, OSError, json.JSONDecodeError) as error:
        print(f"P0 acceptance recording failed: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "acceptance_receipt_id": receipt["acceptance_receipt_id"],
                "job": str(args.output_job),
                "receipt": str(args.receipt),
                "state": receipt["state"],
                "delivery_status": receipt["p0_gate"]["delivery_status"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
