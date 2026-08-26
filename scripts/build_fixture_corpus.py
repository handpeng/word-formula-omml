#!/usr/bin/env python3
"""Build the synthetic DOCX corpus and a source-bound manifest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.fixtures.docx_fixture import load_expectations, write_fixture
from word_formula_omml.contract import dump_manifest, load_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="Directory for generated corpus files")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    docx_path = args.output / "adversarial-v1.docx"
    source_sha256 = write_fixture(docx_path)
    data = load_expectations()
    data["source_sha256"] = source_sha256
    manifest = load_manifest(data)
    manifest_path = args.output / "adversarial-v1.manifest.json"
    dump_manifest(manifest, manifest_path)
    print(json.dumps({"docx": str(docx_path), "manifest": str(manifest_path), "source_sha256": source_sha256}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
