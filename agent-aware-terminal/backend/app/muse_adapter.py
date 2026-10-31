"""Phase 3: AgentAdapter for Muse Code.

Muse writes a replayable JSONL event log per session under
~/.local/share/muse/sessions/YYYY/MM/DD/<session-id>/session.jsonl
(subagents get their own session.jsonl in subagent/<id>/).

This adapter tails the log of the session whose workspace_root matches
the terminal's current directory and normalises records into the
Agent-Aware Terminal internal event contract (TZ section 6.2):

    {"agent": "muse", "session_id": ..., "state": "tool_running",
     "activity": "...", "tool": "file_edit", "files": [...],
     "source": "agent_event", "timestamp": "..."}

No terminal screen-scraping is involved; states labelled
source="agent_event" are observed facts, unlike observer heuristics.
"""
from __future__ import annotations

import glob
import json
import os
import re
import threading
import time

FILE_RE = re.compile(
    r"[\w./\-]+\.(?:py|ts|tsx|js|jsx|rs|go|dart|java|kt|md|json|toml|yaml|yml|sh|css|html)\b"
)
TOOL_STATE = {
    "bash": "tool_running", "bash_input": "tool_running",
    "edit": "tool_running", "file_edit": "tool_running",
    "edit_file": "tool_running", "write": "tool_running",
    "file_write": "tool_running", "write_file": "tool_running",
    "read": "tool_running", "file_read": "tool_running",
    "read_file": "tool_running", "read_skill": "tool_running",
    "grep": "tool_running", "glob": "tool_running", "search": "tool_running",
    "web_fetch": "tool_running", "web_search": "tool_running",
}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _extract_files(text: str, limit: int = 6) -> list[str]:
    out: list[str] = []
    for m in FILE_RE.findall(text):
        pass
    for m in FILE_RE.finditer(text):
        p = m.group(0)
        if p not in out and not p.startswith("http"):
            out.append(p)
        if len(out) >= limit:
            break
    return out


def _short(text: str, n: int = 160) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


class MuseAdapter(threading.Thread):
    def __init__(self, on_event, get_cwd, sessions_root: str | None = None):
        super().__init__(daemon=True, name="muse-adapter")
        self.on_event = on_event
        self.get_cwd = get_cwd
        self.sessions_root = sessions_root or os.path.expanduser(
            "~/.local/share/muse/sessions")
        self.current_file: str | None = None
        self.offset = 0
        self.state: dict = {
            "agent": "muse", "session_id": None, "state": "unknown",
            "activity": "", "tool": None, "files": [], "plan": [],
            "source": "agent_event", "timestamp": _now(),
            "active_subagents": 0, "model": None, "task": "",
        }
        self._stop = threading.Event()
        self._message_pending = False
        self._last_message = ""
        self._last_record_ts = 0.0

    # -- session discovery -------------------------------------------------
    def _workspace_of(self, path: str) -> str | None:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh):
                    if i > 400:
                        break
                    if '"workspace_root"' in line:
                        m = re.search(r'"workspace_root"\s*:\s*"([^"]+)"', line)
                        if m:
                            return m.group(1)
        except Exception:
            return None
        return None

    def _find_session(self) -> str | None:
        cwd = self.get_cwd() or ""
        pattern = os.path.join(self.sessions_root, "*", "*", "*", "*", "session.jsonl")
        files = [f for f in glob.glob(pattern) if "/subagent/" not in f]
        files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
        for f in files[:25]:
            ws = self._workspace_of(f)
            if ws and cwd and (cwd == ws or cwd.startswith(ws + os.sep)
                               or ws.startswith(cwd + os.sep)):
                return f
        # Fallback: a log touched in the last 90 s is probably live.
        if files and time.time() - os.path.getmtime(files[0]) < 90:
            return files[0]
        return None

    def _count_subagents(self, session_file: str) -> int:
        base = os.path.join(os.path.dirname(session_file), "subagent")
        if not os.path.isdir(base):
            return 0
        now = time.time()
        n = 0
        for f in glob.glob(os.path.join(base, "*", "session.jsonl")):
            if now - os.path.getmtime(f) < 120:
                n += 1
        return n

    # -- record normalisation ------------------------------------------------
    def _emit(self, **kw):
        ev = {
            "agent": "muse", "session_id": self.state.get("session_id"),
            "state": "working", "activity": "",
            "source": "agent_event", "timestamp": _now(),
        }
        ev.update(kw)
        # tool/files are sticky: an event that does not mention them must
        # not wipe the tool that is currently running (status and
        # diagnostic records otherwise reset the UI to generic work).
        if "tool" not in ev:
            ev["tool"] = self.state.get("tool")
        if "files" not in ev:
            ev["files"] = self.state.get("files") or []
        self.state.update({k: ev[k] for k in
                           ("state", "activity", "tool", "files")
                           if k in ev})
        if "plan" in ev:
            self.state["plan"] = ev["plan"]
        self.on_event(ev)

    def _handle_record(self, rec: dict):
        pt = rec.get("payload_type", "")
        payload = rec.get("payload") or {}
        sid = (rec.get("stream") or {}).get("id")
        if sid and not self.state.get("session_id"):
            self.state["session_id"] = sid

        if pt == "runtime.session.metadata":
            record = payload.get("record") or {}
            self.state["model"] = record.get("model_id")
            self._emit(state="starting",
                       activity=f"Muse session started ({record.get('model_id', '')})")
            return

        if pt == "runtime.user_intent.accepted":
            self._message_pending = False
            text = ""
            for msg in payload.get("model_messages") or []:
                for part in msg.get("content") or []:
                    if part.get("kind") == "text":
                        text = part.get("text", "")
            if text:
                self.state["task"] = _short(text, 300)
                self._emit(state="planning", activity=f"Task: {_short(text, 120)}")
            return

        if pt == "tool_batch.effect.started":
            self._message_pending = False
            record = payload.get("record") or {}
            tool = record.get("tool_name") or "tool"
            self._emit(state="tool_running", tool=tool,
                       activity=f"Running {tool}")
            return

        if pt == "tool_batch.effect.terminal":
            self._emit(state="working", tool=None, activity="Tool finished")
            return

        if pt == "approval_wait.effect.started":
            record = payload.get("record") or {}
            tool = record.get("tool_name") or "an action"
            self._emit(state="awaiting_user", tool=tool,
                       activity=f"The agent is waiting for approval: {tool}. "
                                "Switch back to the terminal.")
            return

        if pt == "approval_wait.effect.terminal":
            self._emit(state="working", activity="Approval received")
            return

        if pt == "session.end":
            self._emit(state="completed", activity="Session ended")
            return

        if pt != "runtime.session":
            return

        ev = payload.get("event") or {}
        kind = ev.get("kind", "")

        if kind == "todo_snapshot_updated":
            plan = [{"status": i.get("status"), "text": i.get("text")}
                    for i in ev.get("items") or []]
            current = next((i["text"] for i in plan
                            if i["status"] == "in_progress"), "")
            self._emit(state="planning", plan=plan,
                       activity=f"Plan: {_short(current, 120)}" if current else "Plan updated")
        elif kind in ("reasoning_summary_delta", "reasoning_summary_committed"):
            # The model is thinking: that is the planning state, not
            # generic work (the square shows COLOR FLASH for it).
            text = ev.get("text", "")
            now = time.time()
            if kind == "reasoning_summary_delta" and \
                    now - getattr(self, "_last_reasoning", 0) < 1.5:
                return
            self._last_reasoning = now
            if text:
                self._emit(state="planning", activity=_short(text, 150))
        elif kind == "assistant_tool_calls_committed":
            for call in ev.get("tool_calls") or []:
                name = call.get("name") or "tool"
                args = call.get("args") or ""
                files = _extract_files(args)
                if name == "write_todos":
                    continue
                self._emit(state=TOOL_STATE.get(name, "tool_running"),
                           tool=name, files=files,
                           activity=f"{name}: {_short(files[0] if files else args, 110)}")
        elif kind == "assistant_message_committed":
            # An assistant message is not by itself a request for the
            # user: keep the current state, only refresh the activity.
            # (Approval waits arrive as approval_wait events.)
            text = ev.get("text", "")
            if text:
                self._message_pending = True
                self._last_message = _short(text, 150)
                cur = self.state.get("state")
                keep = cur if cur in ("starting", "planning", "working",
                                      "tool_running", "awaiting_user") \
                    else "working"
                self._emit(state=keep, activity=_short(text, 150))
        elif kind == "model_failure_terminal_intent":
            self._emit(state="failed",
                       activity=_short(ev.get("reason") or "Agent failed", 150))
        elif kind == "failed":
            # Per-task failure (e.g. one bash tool exited non-zero):
            # noteworthy, but the agent itself keeps working.
            self._emit(state="working",
                       activity=f"Tool failed: "
                                f"{_short(ev.get('reason') or '', 100)}")
        elif kind == "status":
            msg = ev.get("message", "")
            noisy = ("attempt", "serialized", "eligibility", "diagnostic")
            if msg and not any(n in msg for n in noisy):
                self._emit(state="working", activity=_short(msg, 120))

    def _read_new(self):
        try:
            size = os.path.getsize(self.current_file)
        except Exception:
            self.current_file = None
            return
        if size < self.offset:  # rotated / replaced
            self.offset = 0
        if size == self.offset:
            return
        with open(self.current_file, encoding="utf-8", errors="replace") as fh:
            fh.seek(self.offset)
            chunk = fh.read()
            self.offset = fh.tell()
        for line in chunk.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            records = []
            if "children" in obj:
                for child in obj["children"]:
                    try:
                        records.append(json.loads(child["record_json"]))
                    except Exception:
                        pass
            elif obj.get("record_type") == "event":
                records = [obj]
            for rec in records:
                self._last_record_ts = time.time()
                try:
                    self._handle_record(rec)
                except Exception:
                    pass

    def _check_quiet(self):
        """Interactive sessions emit no session.end at the end of a
        turn. If the agent produced its reply message and the log has
        been quiet for a few seconds, the turn is over."""
        busy = ("starting", "planning", "working", "tool_running")
        if self._message_pending and self.state.get("state") in busy \
                and self._last_record_ts \
                and time.time() - self._last_record_ts > 4:
            self._message_pending = False
            self._emit(state="completed", tool=None,
                       activity=self._last_message or "Turn finished")

    # -- main loop -------------------------------------------------------------
    def run(self):
        while not self._stop.is_set():
            try:
                found = self._find_session()
                if found and found != self.current_file:
                    self.current_file = found
                    # Fresh log: replay from start; old log: only new events.
                    fresh = time.time() - os.path.getmtime(found) < 180
                    self.offset = 0 if fresh else os.path.getsize(found)
                    self.state["session_id"] = None
                if self.current_file:
                    self._read_new()
                    self._check_quiet()
                    self.state["active_subagents"] = \
                        self._count_subagents(self.current_file)
            except Exception:
                pass
            self._stop.wait(0.6)

    def stop(self):
        self._stop.set()

    # -- offline replay (tests / demo fallback) --------------------------------
    def replay_file(self, path: str, max_events: int = 400):
        """Parse an existing session.jsonl and return normalised events."""
        events = []
        saved = self.on_event
        self.on_event = lambda ev: events.append(ev)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if len(events) >= max_events:
                        break
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    records = []
                    if "children" in obj:
                        for child in obj["children"]:
                            try:
                                records.append(json.loads(child["record_json"]))
                            except Exception:
                                pass
                    elif obj.get("record_type") == "event":
                        records = [obj]
                    for rec in records:
                        self._handle_record(rec)
        finally:
            self.on_event = saved
        return events
