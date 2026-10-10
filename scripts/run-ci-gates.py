#!/usr/bin/env python3
"""Run the CI gates listed in .maintainer-policy.json outside GitHub Actions.

``gates.local_commands`` is the one list of commands CI runs;
test-maintainer-policy.py fails when it drifts from ci.yml. This runner is how
a second CI (Woodpecker, see .woodpecker/ci.yml) or a contributor runs that
same list, so no other file keeps its own copy of the gate commands.

Every gate runs even after one fails, like ci.yml's ``if: always()`` steps,
and the exit status is non-zero when any gate failed.

Commands that need more than a Python toolchain are split into groups:

* ``python``  every ``python3 scripts/...`` gate, including lint-render.py
              (needs Playwright and its Chromium) and build-readme-thumbs.py
              (needs Pillow).
* ``node``    the ``npx ... plugin validate`` gate.

One gate depends on the event, mirroring ci.yml: the manifest version gate
runs ``--require-no-bump <base>`` only for a pull request, and
``--current-only`` otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POLICY = ROOT / ".maintainer-policy.json"
VERSION_GATE = "python3 scripts/verify-plugin-package.py --require-no-bump"


def policy_commands() -> list[str]:
    payload = json.loads(POLICY.read_text(encoding="utf-8"))
    commands = payload["gates"]["local_commands"]
    if not isinstance(commands, list) or not all(isinstance(c, str) for c in commands):
        raise SystemExit("run-ci-gates: gates.local_commands must be a list of strings")
    return commands


def group_of(command: str) -> str:
    return "node" if command.startswith("npx ") else "python"


def has_ref(ref: str) -> bool:
    return (
        subprocess.run(
            ["git", "rev-parse", "-q", "--verify", ref],
            cwd=ROOT,
            capture_output=True,
        ).returncode
        == 0
    )


def plan(event: str, base: str, group: str) -> list[list[str]]:
    """Return the gates to run, each a list of shell commands run in order."""
    gates: list[list[str]] = []
    for command in policy_commands():
        if group != "all" and group_of(command) != group:
            continue
        if command.startswith(VERSION_GATE):
            if event == "pull_request":
                gates.append([f"{VERSION_GATE} {shlex.quote(base)}"])
            else:
                gates.append(["python3 scripts/verify-plugin-package.py --current-only"])
            continue
        gates.append([command])
    return gates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--event",
        default=os.environ.get("CI_PIPELINE_EVENT", "push"),
        help="push, pull_request or manual (default: $CI_PIPELINE_EVENT or push)",
    )
    parser.add_argument(
        "--base",
        default="origin/" + os.environ.get("CI_COMMIT_TARGET_BRANCH", "main"),
        help="base ref for the pull request version gate (default: origin/main)",
    )
    parser.add_argument("--group", choices=("python", "node", "all"), default="all")
    parser.add_argument("--list", action="store_true", help="print the plan and exit")
    args = parser.parse_args()

    if args.event == "pull_request" and not has_ref(args.base):
        branch = args.base.split("/", 1)[-1]
        subprocess.run(
            ["git", "fetch", "--no-tags", "origin", f"+refs/heads/{branch}:refs/remotes/{args.base}"],
            cwd=ROOT,
            check=False,
        )

    gates = plan(args.event, args.base, args.group)
    if args.list:
        for steps in gates:
            print(" && ".join(steps))
        return 0

    failed: list[str] = []
    started = time.monotonic()
    for index, steps in enumerate(gates, 1):
        label = " && ".join(steps)
        print(f"\n=== [{index}/{len(gates)}] {label}", flush=True)
        gate_started = time.monotonic()
        ok = True
        for step in steps:
            if subprocess.run(step, shell=True, cwd=ROOT).returncode != 0:
                ok = False
                break
        elapsed = time.monotonic() - gate_started
        print(f"=== {'ok' if ok else 'FAILED'} in {elapsed:.1f}s: {label}", flush=True)
        if not ok:
            failed.append(label)

    total = time.monotonic() - started
    print(f"\n{len(gates) - len(failed)}/{len(gates)} gates passed in {total:.0f}s")
    for label in failed:
        print(f"FAILED: {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
