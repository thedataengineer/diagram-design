#!/usr/bin/env python3
"""Increment the synchronized Claude, Codex, and Factory manifest versions."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATHS = (
    Path(".claude-plugin/plugin.json"),
    Path(".codex-plugin/plugin.json"),
    Path(".factory-plugin/plugin.json"),
)
SKILL_PATH = Path("skills/diagram-design/SKILL.md")
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


class PackageVersionError(ValueError):
    """Raised when the package versions cannot be bumped safely."""


def read_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageVersionError(f"could not read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise PackageVersionError(f"{path} must contain a JSON object")
    return payload


def parse_version(value: object, label: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or (match := SEMVER.fullmatch(value)) is None:
        raise PackageVersionError(
            f"{label} version must be strict MAJOR.MINOR.PATCH semver; got {value!r}"
        )
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def bump(root: Path, part: str = "patch") -> str:
    manifests = [(relative, read_manifest(root / relative)) for relative in MANIFEST_PATHS]
    raw_versions = [payload.get("version") for _, payload in manifests]
    if any(version != raw_versions[0] for version in raw_versions[1:]):
        rendered = ", ".join(
            f"{relative}={version!r}"
            for (relative, _), version in zip(manifests, raw_versions)
        )
        raise PackageVersionError(f"manifest versions are not synchronized: {rendered}")

    major, minor, patch = parse_version(raw_versions[0], str(MANIFEST_PATHS[0]))
    if part == "major":
        next_version = (major + 1, 0, 0)
    elif part == "minor":
        next_version = (major, minor + 1, 0)
    elif part == "patch":
        next_version = (major, minor, patch + 1)
    else:
        raise PackageVersionError(f"unsupported version part: {part}")

    version = ".".join(str(value) for value in next_version)
    rendered_manifests: list[tuple[Path, str]] = []
    for relative, payload in manifests:
        payload["version"] = version
        rendered_manifests.append(
            (root / relative, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        )
    skill_path, skill_contents = render_skill_metadata_version(root, version)
    for path, contents in rendered_manifests:
        path.write_text(contents, encoding="utf-8")
    skill_path.write_text(skill_contents, encoding="utf-8")
    return version


# The one SKILL.md `metadata.version` form every release tool accepts: a
# direct child of the top-level block mapping `metadata:`, written as a
# double-quoted MAJOR.MINOR string. verify-plugin-package.py imports
# locate_skill_metadata_version() so the PR gate rejects exactly what this
# bumper cannot rewrite. The auto-bump publish job keeps a self-contained copy
# of the same rule (it must not import repository code); test-plugin-package.py
# runs that copy against the same fixtures.
METADATA_KEY_LINE = re.compile(r"metadata:(?P<value>.*)")
METADATA_VERSION_LINE = re.compile(
    r'(?P<head>(?P<indent> +)version: +)"(?P<value>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))"'
    r"(?P<tail>(?: +#.*)?)"
)
METADATA_VERSION_FORM = 'a direct child of metadata, double-quoted MAJOR.MINOR (version: "2.6")'


def locate_skill_metadata_version(text: str) -> tuple[list[str], int, re.Match[str]]:
    """Find SKILL.md's single rewritable ``metadata.version`` line.

    Returns the file's lines (with endings), the index of the version line, and
    its METADATA_VERSION_LINE match. Raises PackageVersionError naming the
    problem for anything else: no frontmatter, no block ``metadata:`` mapping,
    zero or several direct ``version`` children, or a direct ``version`` that
    is unquoted, single-quoted, or not MAJOR.MINOR. Deeper keys such as
    ``metadata.compat.version`` are not ``metadata.version`` and are ignored.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise PackageVersionError(f"{SKILL_PATH} has no YAML frontmatter")
    closing = next(
        (index for index in range(1, len(lines)) if lines[index].strip() == "---"), None
    )
    if closing is None:
        raise PackageVersionError(f"{SKILL_PATH} frontmatter is unterminated")

    bodies = [line.rstrip("\r\n") for line in lines]
    metadata_rows = [
        index
        for index in range(1, closing)
        if METADATA_KEY_LINE.fullmatch(bodies[index]) is not None
    ]
    if len(metadata_rows) != 1:
        raise PackageVersionError(
            f"{SKILL_PATH} frontmatter must contain exactly one top-level "
            f"metadata mapping holding metadata.version; found {len(metadata_rows)}"
        )
    start = metadata_rows[0]
    value = METADATA_KEY_LINE.fullmatch(bodies[start]).group("value")  # type: ignore[union-attr]
    if value.strip() and not value.strip().startswith("#"):
        raise PackageVersionError(
            f"{SKILL_PATH} metadata must be a block mapping (flow mappings and "
            f"inline values are not supported); metadata.version must be "
            f"{METADATA_VERSION_FORM}"
        )

    child_indent: int | None = None
    candidates: list[int] = []
    for index in range(start + 1, closing):
        body = bodies[index]
        if not body.strip() or body.lstrip().startswith("#"):
            continue
        indent = len(body) - len(body.lstrip(" "))
        if indent == 0:
            break
        if child_indent is None:
            child_indent = indent
        if indent < child_indent:
            raise PackageVersionError(
                f"{SKILL_PATH} metadata mapping has inconsistent indentation "
                f"at line {index + 1}"
            )
        if indent == child_indent and re.match(r" *version *:", body):
            candidates.append(index)

    if len(candidates) != 1:
        raise PackageVersionError(
            f"{SKILL_PATH} frontmatter must contain exactly one metadata.version, "
            f"{METADATA_VERSION_FORM}; found {len(candidates)}"
        )
    index = candidates[0]
    match = METADATA_VERSION_LINE.fullmatch(bodies[index])
    if match is None or len(match.group("indent")) != child_indent:
        raise PackageVersionError(
            f"{SKILL_PATH} metadata.version must be {METADATA_VERSION_FORM}; "
            f"got {bodies[index].strip()!r}"
        )
    return lines, index, match


def render_skill_metadata_version(root: Path, version: str) -> tuple[Path, str]:
    """Keep SKILL.md's hand-maintained metadata.version on the manifest MAJOR.MINOR.

    The package verifier fails when the two drift, so the bump must update both
    sides in one step. Returns the pending write (rendered before any manifest
    is written, so a malformed SKILL.md aborts the whole bump); raises when the
    frontmatter does not carry exactly one rewritable metadata.version.
    """
    path = root / SKILL_PATH
    if not path.is_file():
        raise PackageVersionError(f"missing {SKILL_PATH}")
    lines, index, match = locate_skill_metadata_version(path.read_text(encoding="utf-8"))
    major_minor = ".".join(version.split(".")[:2])
    line = lines[index]
    ending = line[len(line.rstrip("\r\n")) :]
    lines[index] = f'{match.group("head")}"{major_minor}"{match.group("tail")}{ending}'
    return path, "".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Increment all plugin manifests (patch by default)."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--major", action="store_true", help="increment the major version")
    group.add_argument("--minor", action="store_true", help="increment the minor version")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    part = "major" if args.major else "minor" if args.minor else "patch"
    try:
        version = bump(ROOT, part)
    except PackageVersionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Updated Claude, Codex, and Factory plugin manifests to {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
