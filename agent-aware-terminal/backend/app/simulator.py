"""Simulated agent-event streams for testing the visualization.

A simulation is a JSONL file: one JSON object per line describing the
agent state to show. Lines are streamed with timing, so the overlay
behaves exactly as if a real agent (or the terminal observer) produced
those states. Optional fields:

  "sleep":  seconds to wait before applying this line (default 1)
  "expect": the square mode this state should produce — checked by
            tools/simulate.py and the unit tests

Every other field is copied verbatim into the agent-state dict
("state", "tool", "command", "activity", "needs", "last_failure", ...).
This lets a text file drive the whole visualization without launching
an agent or typing in a terminal.
"""

import json
import threading
import time

META_KEYS = ("sleep", "expect")


def load_script(path):
    lines = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            lines.append(json.loads(raw))
    return lines


def state_for_line(line):
    state = {k: v for k, v in line.items() if k not in META_KEYS}
    state.setdefault("state", "idle")
    state.setdefault("agent", "simulator")
    state.setdefault("source", "simulated")
    return state


def evaluate(path, mode_fn):
    """Run a script through a mode classifier without any timing.

    Returns rows: dict(n, delay, state, mode, expect, ok)."""
    rows = []
    at = 0.0
    for i, line in enumerate(load_script(path), 1):
        at += float(line.get("sleep", 1))
        state = state_for_line(line)
        mode = mode_fn(state)
        expect = line.get("expect")
        rows.append({"n": i, "at": at, "state": state.get("state"),
                     "tool": state.get("tool"),
                     "command": state.get("command"),
                     "mode": mode, "expect": expect,
                     "ok": expect is None or expect == mode})
    return rows


class SimulationFeed:
    """Streams a script into .state, line by line, on a thread."""

    def __init__(self, path, loop=False):
        self.path = path
        self.loop = loop
        self.state = None
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop.set()

    def _run(self):
        try:
            lines = load_script(self.path)
        except Exception:
            return
        while not self._stop.is_set():
            for line in lines:
                if self._stop.wait(float(line.get("sleep", 1))):
                    return
                self.state = state_for_line(line)
            if not self.loop:
                return
