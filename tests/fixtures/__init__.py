"""Deterministic synthetic DOCX fixtures used by the regression suite."""

from .docx_fixture import (
    EXPECTATIONS_PATH,
    build_fixture_package,
    load_expectations,
    write_fixture,
)

__all__ = [
    "EXPECTATIONS_PATH",
    "build_fixture_package",
    "load_expectations",
    "write_fixture",
]
