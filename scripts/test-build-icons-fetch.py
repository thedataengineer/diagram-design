#!/usr/bin/env python3
"""Regression tests for build-icons.py network failure handling.

A stalled read raises TimeoutError (socket.timeout on 3.9), which is not a
URLError; fetch() must treat it like any other network failure and fall back
instead of crashing the whole build.
"""

from __future__ import annotations

import importlib.util
import io
import socket
import sys
import urllib.error
from contextlib import redirect_stderr
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "scripts" / "build-icons.py"


def load_build_module():
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("diagram_design_build_icons", BUILD)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load build-icons.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StalledResponse:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def __enter__(self) -> "StalledResponse":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def read(self) -> bytes:
        raise self.exc


def main() -> int:
    build_icons = load_build_module()
    failures = {
        "read timeout": TimeoutError("The read operation timed out"),
        "socket timeout": socket.timeout("timed out"),
        "connection reset": ConnectionResetError(54, "Connection reset by peer"),
        "url error": urllib.error.URLError("unreachable"),
    }
    original = build_icons.urllib.request.urlopen
    try:
        for label, exc in failures.items():
            build_icons.urllib.request.urlopen = (
                lambda *args, _exc=exc, **kwargs: StalledResponse(_exc)
            )
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                result = build_icons.fetch("https://example.invalid/icon.svg")
            if result is not None:
                raise AssertionError(f"{label}: expected None, got {result!r}")
            if "network error" not in stderr.getvalue():
                raise AssertionError(f"{label}: failure was not reported on stderr")
            print(f"PASS: fetch() reports and survives a {label}")
    finally:
        build_icons.urllib.request.urlopen = original
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
