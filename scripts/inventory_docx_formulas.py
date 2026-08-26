#!/usr/bin/env python3
"""Create a read-only, story-aware formula candidate inventory for a DOCX."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from word_formula_omml.inventory import InventoryError, write_inventory


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Source DOCX; it is never modified")
    parser.add_argument("output", type=Path, help="Manifest output path")
    args = parser.parse_args()
    try:
        manifest = write_inventory(args.source, args.output)
    except InventoryError as error:
        print(f"inventory failed closed: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "manifest": str(args.output),
                "manifest_id": manifest.manifest_id,
                "source_sha256": manifest.source_sha256,
                "occurrences": len(manifest.formulas),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
