#!/usr/bin/env python3
"""Boot the app once per mock state and assert the square's mode.

Reads the app's own decision log (~/.local/share/agent-aware-
terminal/decisions.log): for every AAT_MOCKSTATE scenario the last
[mode] line must name the expected mode. This is the end-to-end
state matrix check; unit tests live in backend/tests.

Usage: python3 tools/check_states.py [mock ...]
"""
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "dist" / "agent-aware-terminal" / "agent-aware-terminal"
LOG = pathlib.Path.home() / ".local" / "share" / \
    "agent-aware-terminal" / "decisions.log"

EXPECTED = {
    "idle": "blinky", "working": "build", "thinking": "flash",
    "attention": "shuffle", "completed": "done", "glitch": "glitch",
    "trail": "trail", "compile": "compile", "link": "link",
    "copy": "copy", "install": "install", "delete": "delete",
    "download": "download", "test": "test", "git": "git",
    "extract": "extract", "serve": "serve", "question": "question",
    "failcompile": "fail_compile", "failtest": "fail_test",
    "fail": "fail",
}


def log_lines():
    try:
        return LOG.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []


def check(mock: str, expected: str) -> tuple[bool, str]:
    before = len(log_lines())
    env = dict(os.environ, AAT_MOCKSTATE=mock, AAT_OPEN_OVERLAY="1",
               AAT_APP_ID="dev.meta.aat.check", AAT_THEME="dark")
    proc = subprocess.Popen(
        [str(BIN)], env=env, cwd="/tmp",
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 12
        got = None
        while time.time() < deadline:
            for line in log_lines()[before:]:
                if "[mode]" in line and " -> " in line:
                    got = line.split(" -> ", 1)[1].split(" ", 1)[0]
            if got == expected:
                return True, got
            time.sleep(0.5)
        return got == expected, str(got)
    finally:
        proc.kill()
        proc.wait(timeout=5)


def main() -> int:
    mocks = sys.argv[1:] or sorted(EXPECTED)
    if not BIN.exists():
        print(f"binary missing: {BIN} (run ./build-binary.sh)")
        return 2
    bad = 0
    for mock in mocks:
        ok, got = check(mock, EXPECTED[mock])
        print(f"{'OK ' if ok else 'FAIL'} {mock:<12} -> {got}"
              f" (expect {EXPECTED[mock]})")
        bad += 0 if ok else 1
    print(f"\n{len(mocks) - bad}/{len(mocks)} states correct")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
