"""Deterministic capability checks used before formula processing."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


PREFLIGHT_SCHEMA_VERSION = 1
PYTHON_MIN = (3, 10)
PYTHON_MAX_EXCLUSIVE = (3, 13)
PANDOC_MIN = (3, 0, 0)
PANDOC_MAX_EXCLUSIVE = (4, 0, 0)
PANDOC_API_MIN = (1, 22)
PANDOC_API_MAX_EXCLUSIVE = (2, 0)
COMPANION_FILES = ("SKILL.md", "ooxml.md")


class PreflightError(RuntimeError):
    """Raised for malformed preflight inputs rather than an unavailable tool."""


def _version_tuple(value: Sequence[int], width: int) -> tuple[int, ...]:
    if len(value) < 1 or any(isinstance(item, bool) or not isinstance(item, int) for item in value):
        raise PreflightError(f"invalid version tuple: {value!r}")
    return tuple(value[:width]) + (0,) * max(0, width - len(value))


def _in_range(value: tuple[int, ...], minimum: tuple[int, ...], maximum: tuple[int, ...]) -> bool:
    return value >= minimum and value < maximum


def _check_python(version: Sequence[int] | None = None) -> dict[str, Any]:
    raw = version or tuple(sys.version_info[:2])
    current = _version_tuple(raw, 2)
    state = "PASS" if _in_range(current, PYTHON_MIN, PYTHON_MAX_EXCLUSIVE) else "FAIL"
    result: dict[str, Any] = {
        "name": "python",
        "state": state,
        "version": ".".join(str(item) for item in current),
        "required": (
            f">={PYTHON_MIN[0]}.{PYTHON_MIN[1]},"
            f"<{PYTHON_MAX_EXCLUSIVE[0]}.{PYTHON_MAX_EXCLUSIVE[1]}"
        ),
    }
    if state == "FAIL":
        result["reason"] = "Python version is outside the supported matrix"
    return result


def _decode(data: bytes | str | None) -> str:
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data or ""


def _parse_pandoc_version(output: str) -> tuple[int, ...] | None:
    match = re.search(r"\bpandoc\s+([0-9]+(?:\.[0-9]+){1,3})\b", output, re.IGNORECASE)
    if match is None:
        return None
    return tuple(int(item) for item in match.group(1).split("."))


def _parse_api_version(output: bytes) -> tuple[int, ...] | None:
    try:
        value = json.loads(output)
    except (TypeError, json.JSONDecodeError):
        return None
    version = value.get("pandoc-api-version") if isinstance(value, Mapping) else None
    if not isinstance(version, list) or any(isinstance(item, bool) or not isinstance(item, int) for item in version):
        return None
    return tuple(version)


def _check_pandoc(executable: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": "pandoc",
        "state": "FAIL",
        "executable": executable,
        "required": f">={'.'.join(map(str, PANDOC_MIN))},<{PANDOC_MAX_EXCLUSIVE[0]}",
        "api_required": f">={'.'.join(map(str, PANDOC_API_MIN))},<{PANDOC_API_MAX_EXCLUSIVE[0]}",
    }
    if not executable:
        result["reason"] = "Pandoc executable name is empty"
        return result
    resolved = shutil.which(executable)
    if resolved is None:
        result["reason"] = f"Pandoc executable {executable!r} was not found on PATH"
        return result
    result["resolved"] = resolved
    try:
        version_run = subprocess.run(
            [executable, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
        api_run = subprocess.run(
            [executable, "--from=markdown", "--to=json"],
            input=b"",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
    except subprocess.TimeoutExpired as error:
        result["reason"] = f"Pandoc preflight timed out: {error}"
        return result
    except OSError as error:
        result["reason"] = f"Pandoc preflight could not execute {executable!r}: {error}"
        return result

    version = _parse_pandoc_version(_decode(version_run.stdout) + "\n" + _decode(version_run.stderr))
    api_version = _parse_api_version(api_run.stdout)
    if version is None:
        result["reason"] = "Pandoc --version output did not contain a recognizable version"
        return result
    normalized_version = _version_tuple(version, 3)
    result["version"] = ".".join(str(item) for item in normalized_version)
    if version_run.returncode != 0:
        result["reason"] = f"Pandoc --version exited with status {version_run.returncode}"
        return result
    if not _in_range(normalized_version, PANDOC_MIN, PANDOC_MAX_EXCLUSIVE):
        result["reason"] = "Pandoc version is outside the supported range"
        return result
    if api_run.returncode != 0 or api_run.stderr:
        diagnostics = _decode(api_run.stderr).strip() or f"exit status {api_run.returncode}"
        result["reason"] = f"Pandoc JSON capability probe failed: {diagnostics}"
        return result
    if api_version is None:
        result["reason"] = "Pandoc capability probe returned no valid pandoc-api-version"
        return result
    normalized_api = _version_tuple(api_version, 2)
    result["api_version"] = list(api_version)
    if not _in_range(normalized_api, PANDOC_API_MIN, PANDOC_API_MAX_EXCLUSIVE):
        result["reason"] = "Pandoc API version is outside the supported range"
        return result
    result["state"] = "PASS"
    return result


def _check_companion(root: str | Path | None, *, required: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": "companion_docx_skill",
        "required": required,
        "state": "FAIL" if required else "DEFERRED",
    }
    if root is None:
        result["reason"] = "set --companion-root to the reviewed companion docx skill directory"
        return result
    path = Path(root)
    result["root"] = str(path)
    if not path.is_dir():
        result["reason"] = f"companion docx skill directory does not exist: {path}"
        return result
    missing = [name for name in COMPANION_FILES if not (path / name).is_file()]
    if missing:
        result["reason"] = f"companion docx skill is missing required files: {missing}"
        return result
    result["state"] = "PASS"
    return result


def _check_native_word(command: str | None, *, required: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": "microsoft_word",
        "required": required,
        "state": "FAIL" if required else "UNAVAILABLE",
    }
    candidates = [command] if command else ["WINWORD.EXE", "winword"]
    resolved = next((shutil.which(item) for item in candidates if item and shutil.which(item)), None)
    if resolved is None:
        result["reason"] = "Microsoft Word is not available; native validation remains a manual/controlled gate"
        return result
    result["executable"] = resolved
    result["state"] = "PASS"
    return result


@dataclass(frozen=True)
class PreflightReport:
    """Machine-readable preflight result with separate portable and delivery flags."""

    checks: tuple[dict[str, Any], ...]
    require_companion: bool
    require_native_word: bool

    @property
    def status(self) -> str:
        return "FAIL" if any(check["state"] == "FAIL" for check in self.checks) else "PASS"

    @property
    def portable_ready(self) -> bool:
        required = {"python", "pandoc"}
        return all(check["state"] == "PASS" for check in self.checks if check["name"] in required)

    @property
    def mutation_ready(self) -> bool:
        companion = next(check for check in self.checks if check["name"] == "companion_docx_skill")
        return self.portable_ready and companion["state"] == "PASS"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PREFLIGHT_SCHEMA_VERSION,
            "status": self.status,
            "portable_ready": self.portable_ready,
            "mutation_ready": self.mutation_ready,
            "require_companion": self.require_companion,
            "require_native_word": self.require_native_word,
            "checks": [dict(check) for check in self.checks],
        }


def run_preflight(
    *,
    pandoc: str = "pandoc",
    companion_root: str | Path | None = None,
    require_companion: bool = False,
    native_word_command: str | None = None,
    require_native_word: bool = False,
    python_version: Sequence[int] | None = None,
) -> PreflightReport:
    """Run all checks without touching a DOCX or creating a staging artifact."""

    checks = (
        _check_python(python_version),
        _check_pandoc(pandoc),
        _check_companion(companion_root, required=require_companion),
        _check_native_word(native_word_command, required=require_native_word),
    )
    return PreflightReport(
        checks=checks,
        require_companion=require_companion,
        require_native_word=require_native_word,
    )


__all__ = [
    "COMPANION_FILES",
    "PANDOC_API_MAX_EXCLUSIVE",
    "PANDOC_API_MIN",
    "PANDOC_MAX_EXCLUSIVE",
    "PANDOC_MIN",
    "PREFLIGHT_SCHEMA_VERSION",
    "PYTHON_MAX_EXCLUSIVE",
    "PYTHON_MIN",
    "PreflightError",
    "PreflightReport",
    "run_preflight",
]
