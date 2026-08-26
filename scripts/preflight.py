#!/usr/bin/env python3
"""Check portable and optional Word Formula OMML capabilities before mutation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from word_formula_omml.preflight import PreflightError, run_preflight


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pandoc", default="pandoc")
    parser.add_argument("--companion-root", type=Path)
    parser.add_argument(
        "--companion-fingerprint",
        help="reviewed SHA-256 fingerprint reported for SKILL.md + ooxml.md",
    )
    parser.add_argument("--require-companion", action="store_true")
    parser.add_argument("--native-word", dest="native_word_command")
    parser.add_argument("--require-native-word", action="store_true")
    parser.add_argument("--json", type=Path, help="also write the report to this path")
    args = parser.parse_args()
    try:
        report = run_preflight(
            pandoc=args.pandoc,
            companion_root=args.companion_root,
            require_companion=args.require_companion,
            companion_fingerprint=args.companion_fingerprint,
            native_word_command=args.native_word_command,
            require_native_word=args.require_native_word,
        )
    except PreflightError as error:
        print(f"preflight failed: {error}", file=sys.stderr)
        return 2
    rendered = json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report.status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
