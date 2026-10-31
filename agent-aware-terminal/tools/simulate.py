#!/usr/bin/env python3
"""Check a simulation script against the square's mode classifier.

Usage: python3 tools/simulate.py sims/demo_run.jsonl [more.jsonl ...]

Prints the expected timeline (state -> square mode) and exits
non-zero if any line's "expect" does not match. No GUI, no agent:
this is the automated way to regression-test the visualization.
"""
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app.simulator import evaluate  # noqa: E402


def _mode_fn():
    spec = importlib.util.spec_from_file_location(
        "native_terminal", ROOT / "desktop" / "native_terminal.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.mode_for_agent


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    mode_fn = _mode_fn()
    failed = 0
    for path in argv:
        print(f"== {path}")
        for row in evaluate(path, mode_fn):
            what = row["command"] or row["tool"] or row["state"]
            mark = ""
            if row["expect"] is not None:
                mark = ("OK  " if row["ok"] else "FAIL") + \
                    f" (expect {row['expect']})"
                failed += 0 if row["ok"] else 1
            print(f"  +{row['at']:5.1f}s  {row['state']:<13} "
                  f"{str(what)[:44]:<44} -> {row['mode']:<12} {mark}")
    print("FAILED" if failed else "ALL OK")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
