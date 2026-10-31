"""Phase 2: SessionObserver.

Reads observable facts about the terminal session from /proc: the
foreground process, its command line, its working directory and the
process tree under the shell. It separates observations from
inferences (TZ 2.2): process/cwd are source="observed", while
"an agent is probably working" is source="heuristic" with a
confidence the UI can display.
"""
from __future__ import annotations

import os
import threading
import time

AGENT_BINARIES = ("muse", "codex", "claude")


def _proc_info(pid: int) -> dict | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            stat = fh.read().decode(errors="replace")
        comm = stat[stat.index("(") + 1: stat.rindex(")")]
        fields = stat[stat.rindex(")") + 2:].split()
        ppid = int(fields[1])
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            cmdline = fh.read().decode(errors="replace").replace("\0", " ").strip()
        cwd = os.readlink(f"/proc/{pid}/cwd")
        return {"pid": pid, "comm": comm, "ppid": ppid,
                "cmdline": cmdline, "cwd": cwd}
    except Exception:
        return None


def _process_tree(root_pid: int) -> list[dict]:
    procs: dict[int, dict] = {}
    children: dict[int, list[int]] = {}
    try:
        pids = [int(p) for p in os.listdir("/proc") if p.isdigit()]
    except Exception:
        return []
    for pid in pids:
        info = _proc_info(pid)
        if info:
            procs[pid] = info
            children.setdefault(info["ppid"], []).append(pid)
    out, stack = [], [root_pid]
    seen = set()
    while stack:
        pid = stack.pop()
        if pid in seen or pid not in procs:
            continue
        seen.add(pid)
        out.append(procs[pid])
        stack.extend(children.get(pid, []))
    return out


class SessionObserver(threading.Thread):
    def __init__(self, pty, on_snapshot, interval: float = 0.8):
        super().__init__(daemon=True, name="session-observer")
        self.pty = pty
        self.on_snapshot = on_snapshot
        self.interval = interval
        self.snapshot: dict = {}
        self._prev_fg: int | None = None
        self._command_started: float | None = None
        self._stop = threading.Event()

    def _detect_agent(self, tree: list[dict]) -> str | None:
        for proc in tree:
            cmd = (proc["cmdline"] or proc["comm"] or "").lower()
            for name in AGENT_BINARIES:
                if f"/{name}" in cmd or cmd.startswith(name + " ") \
                        or proc["comm"] == name:
                    return name
        return None

    def run(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                pass
            self._stop.wait(self.interval)

    def _tick(self):
        shell_pid = self.pty.pid
        if not shell_pid:
            return
        fg_pid = self.pty.foreground_pid() or shell_pid
        fg = _proc_info(fg_pid) or _proc_info(shell_pid) or {}
        shell = _proc_info(shell_pid) or {}
        tree = _process_tree(shell_pid)
        agent_cli = self._detect_agent(tree)

        command_running = fg_pid != shell_pid
        event = None
        now = time.time()
        if command_running and self._prev_fg == shell_pid:
            self._command_started = now
            event = {"kind": "command_started",
                     "process": fg.get("comm"), "source": "observed"}
        elif not command_running and self._prev_fg not in (None, shell_pid):
            dur = round(now - (self._command_started or now), 1)
            event = {"kind": "command_finished", "duration_s": dur,
                     "source": "observed"}
            self._command_started = None
        self._prev_fg = fg_pid

        # Inference layer (clearly separated from observation, TZ 2.2).
        if agent_cli and command_running:
            inferred_state, confidence = "working", 0.55
        elif agent_cli:
            inferred_state, confidence = "awaiting_user", 0.5
        elif command_running:
            inferred_state, confidence = "tool_running", 0.4
        else:
            inferred_state, confidence = "idle", 0.9

        self.snapshot = {
            "shell_pid": shell_pid,
            "foreground": {"pid": fg.get("pid"), "process": fg.get("comm"),
                           "cmdline": (fg.get("cmdline") or "")[:200]},
            "cwd": fg.get("cwd") or shell.get("cwd") or "",
            "command_running": command_running,
            "command_duration_s": round(now - self._command_started, 1)
                if self._command_started and command_running else None,
            "agent_cli_detected": agent_cli,
            "process_tree": [{"pid": p["pid"], "process": p["comm"],
                              "cmdline": (p["cmdline"] or "")[:120]}
                             for p in tree[:25]],
            "inferred": {"state": inferred_state, "confidence": confidence,
                         "source": "heuristic"},
            "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": "observed",
        }
        if event:
            event["timestamp"] = self.snapshot["observed_at"]
            self.snapshot["event"] = event
        self.on_snapshot(self.snapshot)

    def stop(self):
        self._stop.set()
