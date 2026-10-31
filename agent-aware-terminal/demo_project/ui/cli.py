"""Pulseboard terminal UI: prints the dashboard to the console."""
from __future__ import annotations

import argparse

from api.routes import Routes
from core.engine import Engine
from storage.db import Storage


def render(summary: dict) -> str:
    return (f"metrics: {summary['count']}  "
            f"avg: {summary['avg']:.1f}  max: {summary['max']:.1f}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="pulseboard")
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    routes = Routes(Engine(Storage()))
    result = routes.post_metrics(
        "demo", {"builds": 12, "tests": 98, "uptime": 99.5})
    print("Pulseboard demo dashboard")
    print(render(result["summary"]))


if __name__ == "__main__":
    main()
