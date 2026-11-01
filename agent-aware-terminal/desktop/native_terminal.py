#!/usr/bin/env python3
"""Agent-Aware Terminal — fully native Linux application (GTK 3 + VTE).

No web pages, no WebKit, no localhost server. The terminal is a real
Vte.Terminal widget (the GTK/VTE host named in the TZ); the overlay is
native GTK widgets layered over it with GtkOverlay. Agent awareness,
project indexing, Muse Spark cards and Muse Image generation reuse the
same backend modules in-process:

    backend.app.muse_adapter      Muse session log -> AgentEvent
    backend.app.session_observer  /proc observations vs heuristics
    backend.app.project_indexer   ArchitectureModel
    backend.app.content           deterministic + Spark cards
    backend.app.meta_client       Muse Spark / Muse Image

Modes:
  agent-aware-terminal               this GUI (default)
  agent-aware-terminal --project P   open project P first
Environment: MODEL_API_KEY (Meta), AAT_OPEN_OVERLAY=1 (demo/screenshots),
AAT_MOCKSTATE=idle|working|thinking|attention|completed (display-only).
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
if FROZEN:
    RES_ROOT = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
else:
    RES_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RES_ROOT))

DATA_DIR = Path(os.environ.get(
    "AAT_DATA_DIR",
    Path.home() / ".local" / "share" / "agent-aware-terminal"))
CACHE_DIR = DATA_DIR / "cache"
BUSY_STATES = BUSY = ("starting", "planning", "working", "tool_running")


def ensure_demo_project() -> Path:
    dst = DATA_DIR / "demo_project"
    if not dst.is_dir():
        src = RES_ROOT / "demo_project"
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns(
                ".git", "__pycache__", ".venv", ".pytest_cache",
                "pulseboard-data.json"))
    return dst


def git_changes(root: str):
    """Real per-file changes from git: [{file, add, del}] or None."""
    import subprocess
    try:
        out = subprocess.run(["git", "-C", root, "diff", "--numstat", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            return None
        files = []
        for line in out.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                add = 0 if parts[0] == "-" else int(parts[0])
                dele = 0 if parts[1] == "-" else int(parts[1])
                files.append({"file": parts[2], "add": add, "del": dele})
        tracked = {f["file"] for f in files}
        status = subprocess.run(["git", "-C", root, "status", "--porcelain"],
                                capture_output=True, text=True, timeout=10)
        for line in status.stdout.splitlines():
            code, name = line[:2], line[3:].strip()
            if code == "??" and name and not name.endswith("/") \
                    and name not in tracked:
                files.append({"file": name, "add": 1, "del": 0,
                              "untracked": True})
        return files
    except Exception:
        return None


_SKIP_DIRS = {".git", "__pycache__", ".venv", ".venv-build",
              "node_modules", ".pytest_cache", "dist", "build"}


def snapshot_files(root: str) -> dict:
    """{relative path: (size, mtime)} for everything under root."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            path = os.path.join(dirpath, fn)
            try:
                st = os.stat(path)
                out[os.path.relpath(path, root)] = (st.st_size,
                                                    int(st.st_mtime))
            except OSError:
                pass
    return out


def _line_count(path: str) -> int:
    try:
        with open(path, "rb") as fh:
            return fh.read().count(b"\n")
    except Exception:
        return 0


def git_changes_scoped(root: str):
    """git_changes(), but only files inside root (repo may be a parent)."""
    import subprocess
    files = git_changes(root)
    if files is None:
        return None
    try:
        top = subprocess.run(["git", "-C", root, "rev-parse",
                              "--show-toplevel"], capture_output=True,
                             text=True, timeout=10).stdout.strip()
        prefix = os.path.relpath(root, top)
    except Exception:
        return files
    if prefix in ("", "."):
        return files
    out = []
    for f in files:
        if f["file"] == prefix or f["file"].startswith(prefix + "/"):
            g = dict(f)
            g["file"] = f["file"][len(prefix) + 1:] \
                if f["file"] != prefix else f["file"]
            out.append(g)
    return out


def session_diff(root: str, baseline: dict) -> list:
    """What actually changed in root since the baseline snapshot.

    This is the truthful per-session file list: unlike a raw git diff
    it ignores pre-existing dirty files and parent-repo noise, and it
    sees files the agent created, edited or deleted during the task.
    Line counts come from git for tracked files.
    """
    now = snapshot_files(root)
    numstat = {f["file"]: f for f in (git_changes_scoped(root) or [])}
    out = []
    for rel, sig in now.items():
        if rel not in baseline:
            f = numstat.get(rel)
            out.append({"file": rel,
                        "add": f["add"] if f else _line_count(
                            os.path.join(root, rel)),
                        "del": f["del"] if f else 0})
        elif baseline[rel] != sig:
            f = numstat.get(rel)
            out.append({"file": rel,
                        "add": f["add"] if f else 1,
                        "del": f["del"] if f else 0})
    for rel in baseline:
        if rel not in now:
            f = numstat.get(rel)
            out.append({"file": rel, "add": 0,
                        "del": f["del"] if f else 1})
    out.sort(key=lambda g: g["file"])
    return out


# ---------------------------------------------------------------------------
# Core: backend logic without any HTTP layer
# ---------------------------------------------------------------------------

class Core:
    def __init__(self, project_path: str):
        from backend.app.content import CardStore, deterministic_cards
        from backend.app.meta_client import MetaClient
        from backend.app.muse_adapter import MuseAdapter
        from backend.app.project_indexer import index_project

        self._index_project = index_project
        self._deterministic_cards = deterministic_cards
        self.meta = MetaClient()
        from backend.app.liquid_client import LiquidClient
        from backend.app.simulator import SimulationFeed
        self.liquid = LiquidClient()
        from collections import deque
        self.decisions = deque(maxlen=60)
        self.liquid_stats = {"screen_calls": 0, "screen_ok": 0,
                             "screen_err": 0, "last_choice": "",
                             "last_conf": 0.0}
        self._prev_sig = None
        self._last_outcome = None  # {"key": ..., "mode": done|fail}
        self._busy_started_at = None
        self.liquid_check = {"status": "pending", "detail": ""}
        self.dlog("boot", f"Liquid System-1: "
                    f"{'configured (' + self.liquid.model + ')' if self.liquid.configured else 'NOT CONFIGURED (no LIQUID_API_KEY)'}")
        if not self.liquid.configured:
            self.liquid_check = {"status": "no-key",
                                 "detail": "no LIQUID_API_KEY"}
        else:
            def _probe():
                ok, detail = self.liquid.health_check()
                self.liquid_check = {"status": "ok" if ok else "failed",
                                     "detail": detail}
                self.dlog("system1", f"health check: "
                                     f"{'OK' if ok else 'FAILED'} ({detail})")
            threading.Thread(target=_probe, daemon=True).start()
        self.sim_feed = None
        sim_path = os.environ.get("AAT_SIM_FILE")
        if sim_path and os.path.exists(sim_path):
            self.sim_feed = SimulationFeed(
                sim_path, loop=os.environ.get("AAT_SIM_LOOP") == "1")
            self.sim_feed.start()
        self.store = CardStore(str(DATA_DIR / "saved_cards.json"))
        self.project: dict | None = None
        self.project_path = project_path
        self.agent_state: dict = {"state": "idle", "source": "heuristic"}
        self.adapter_state: dict | None = None
        self.snapshot: dict = {}
        self.timeline: deque = deque(maxlen=60)
        self.enriched: list = []
        self.enrich_status = "idle"
        self.stats = {"files_touched": [], "events": 0, "started_at": None}
        self.working_since: float | None = None
        self.world_path: Path | None = None
        self.poster_path: Path | None = None
        self.world_status = "idle"
        self.poster_status = "idle"
        # System-1 overlay gate + end-of-session results
        self.gate = {"task_key": None, "decision": None, "p": None,
                     "source": None, "asks": 0, "asking": False,
                     "last_ask": 0.0}
        self.results: dict | None = None
        self._baseline: dict | None = None
        self.step_count = 0
        self.mode_override: dict | None = None
        self._classify_at = 0.0
        self._classifying = False
        self._session_key = None
        self._last_state = "idle"
        self._busy_since: float | None = None
        self._lock = threading.Lock()

        self.adapter = MuseAdapter(self._on_agent_event, self._get_cwd)
        self.adapter.start()
        self.reindex(project_path)
        self._load_enriched_cache()

    # -- project ------------------------------------------------------
    def reindex(self, path: str):
        try:
            self.project = self._index_project(path)
            self.project_path = self.project["root"]
        except Exception:
            self.project = None

    def _load_enriched_cache(self):
        import json
        if not self.project:
            return
        f = CACHE_DIR / f"cards-{self.project['id']}.json"
        if f.is_file():
            try:
                self.enriched = json.loads(f.read_text())
                self.enrich_status = "ready"
            except Exception:
                pass

    def cards(self) -> list:
        if not self.project:
            return []
        activity = ""
        st = self.agent_state.get("state")
        if st in BUSY + ("completed", "failed"):
            activity = str(self.agent_state.get("activity") or "")
        return self._deterministic_cards(
            self.project, self.touched_component(), activity) + self.enriched

    def touched_component(self) -> dict | None:
        from backend.app.project_indexer import component_for_file
        files = self.agent_state.get("files") or []
        if files and self.project:
            return component_for_file(self.project, files[0])
        return None

    # -- agent events ---------------------------------------------------
    def _get_cwd(self) -> str:
        return self.snapshot.get("cwd") or self.project_path

    def _on_agent_event(self, ev: dict):
        skey = ev.get("session_id") or ""
        if skey and skey != self._session_key:
            self._session_key = skey
            self.step_count = 0
            self.results = None
            self.gate = {"task_key": None, "decision": None, "p": None,
                         "source": None, "asks": 0, "asking": False,
                         "last_ask": 0.0}
        state = ev.get("state")
        if state in BUSY and self._last_state not in BUSY + ("awaiting_user",) \
                and self._baseline is None:
            # a new task begins: remember the project's files so the
            # end-of-session diff only shows this session's changes
            self._start_baseline()
        if state in ("working", "tool_running"):
            self.step_count += 1
        if state in ("completed", "failed") and self._last_state in \
                BUSY + ("awaiting_user",):
            if self.working_since and \
                    time.time() - self.working_since >= 3:
                self._finish_session(state)
            else:
                # A completion we never watched happen (e.g. session
                # history attached at startup) gets no score.
                self.dlog("score", "completion skipped: "
                                   "turn not watched live")
            self._baseline = None
        self._last_state = state or self._last_state
        with self._lock:
            self.adapter_state = dict(self.adapter.state)
            self.timeline.appendleft(ev)
            self.stats["events"] += 1
            for f in ev.get("files") or []:
                base = os.path.basename(f)
                if base and base not in self.stats["files_touched"]:
                    self.stats["files_touched"].append(base)
            if ev.get("state") in BUSY:
                if self.stats["started_at"] is None:
                    self.stats["started_at"] = time.time()
                if self.working_since is None:
                    self.working_since = time.time()
            elif ev.get("state") in ("completed", "failed", "awaiting_user"):
                self.working_since = self.working_since  # keep for stats

    def _start_baseline(self):
        if self._baseline is not None:
            return
        self._baseline = {}
        root = self.project_path

        def _snap():
            try:
                self._baseline = snapshot_files(root)
            except Exception:
                self._baseline = {}
        threading.Thread(target=_snap, daemon=True).start()

    def _finish_observed_turn(self, agent: str, final: str, secs: int):
        """End-of-turn results for adapter-less agents (Codex, ...):
        measured files + a System-1 verdict over the console tail."""
        if getattr(self, "_finalized_at", 0) \
                and time.time() - self._finalized_at < 15 \
                and self.results and self.results.get("observed"):
            return  # this turn was already finalized
        self._finalized_at = time.time()
        if self._baseline is not None:
            files = session_diff(self.project_path, self._baseline)
            self._baseline = None
        else:
            files = git_changes_scoped(self.project_path) or []
        self.results = {"files": files, "completion": None,
                        "safety": None, "source": "pending",
                        "final_state": final, "observed": True}
        results_ref = self.results
        excerpt = (getattr(self, "_screen_tail", "") or "")[-1200:]

        def _score():
            state_text = (
                f"Task handled by the {agent} coding agent, observed "
                f"from its terminal. Final state: {final}. Turn "
                f"length: {secs}s.\nConsole excerpt (end of "
                f"turn):\n{excerpt or '(unavailable)'}\n"
                f"Files changed during this turn (measured): "
                f"{len(files)}: "
                + ", ".join(f["file"] for f in files[:14]))
            self._apply_evaluation(results_ref, state_text, final,
                                   files, label=f"observed {agent}")
        threading.Thread(target=_score, daemon=True).start()
        self.dlog("score", f"observed turn finished ({agent}, "
                           f"{final}, {secs}s) — scoring")

    def _apply_evaluation(self, results_ref, state_text, final_state,
                          files, plan_ratio=None, label="session"):
        try:
            ev = self.liquid.evaluate_session(state_text) \
                if self.liquid.configured else None
        except Exception:
            ev = None
        if ev:
            results_ref["completion"] = ev["completion"]
            results_ref["safety"] = ev["safety"]
            results_ref["source"] = f"liquid {self.liquid.model}"
        else:
            ratio = plan_ratio if plan_ratio is not None else (
                0.75 if final_state == "completed" else 0.0)
            comp = round(4 * ratio)
            adds = sum(f["add"] for f in files)
            dels = sum(f["del"] for f in files)
            del_ratio = dels / max(adds + dels, 1)
            safe = 4 - (1 if len(files) > 8 else 0) \
                - (1 if del_ratio > 0.4 else 0) \
                - (1 if final_state == "failed" else 0)
            results_ref["completion"] = {
                "score": float(comp), "confidence": 0.0}
            results_ref["safety"] = {
                "score": float(max(0, min(4, safe))), "confidence": 0.0}
            results_ref["source"] = "heuristic (no LIQUID_API_KEY)"
        self.dlog(
            "score",
            f"to System-1 ({label}): files={len(files)} -> "
            f"completion={results_ref['completion']['score']}/4, "
            f"safety={results_ref['safety']['score']}/4 "
            f"[{results_ref['source']}]")

    # -- System-1 overlay gate -------------------------------------------
    def _gate_state_text(self, elapsed: float) -> str:
        st = self.agent_state
        plan = st.get("plan") or []
        plan_txt = "; ".join(
            f"[{i.get('status', '?')}] {i.get('text', '')}" for i in plan) \
            or "no plan yet"
        recent = []
        for ev in list(self.timeline)[:6]:
            txt = str(ev.get("activity") or ev.get("tool") or "").strip()
            if txt:
                recent.append(txt)
        return (
            "A coding agent is working in a terminal.\n"
            f"Task from the user: {st.get('task') or '(unknown)'}\n"
            f"Plan: {plan_txt}\n"
            f"Elapsed: {int(elapsed)}s, steps so far: {self.step_count}, "
            f"state: {st.get('state')}, current tool: {st.get('tool')}\n"
            f"Files touched: {', '.join(self.stats['files_touched'][:10]) or 'none yet'}\n"
            f"Recent activity: {' | '.join(recent) or 'starting'}")

    def gate_tick(self, min_seconds: float = 5.0):
        """Ask System 1 (Liquid d1.1) whether this task is a long one.

        Called from the UI tick. Decision is cached per task; a "short"
        answer is re-checked at most every 25 s, four times max. Without
        a Liquid key a documented heuristic decides instead.
        """
        st = self.agent_state
        state = st.get("state")
        if state not in BUSY:
            self._busy_since = None
            return self.gate
        now = time.time()
        if self._busy_since is None:
            self._busy_since = now
        elapsed = now - self._busy_since
        key = (st.get("session_id") or self.project_path,
               (st.get("task") or "")[:80])
        if key != self.gate["task_key"]:
            self.gate = {"task_key": key, "decision": None, "p": None,
                         "source": None, "asks": 0, "asking": False,
                         "last_ask": 0.0}
        g = self.gate
        if g["asking"] or g["decision"] == "long" or g["asks"] >= 4:
            return g
        if elapsed < min_seconds:
            return g
        if self.adapter_state and self.step_count < 2:
            return g  # wait until the agent has taken a couple of steps
        if g["decision"] == "short" and now - g["last_ask"] < 25:
            return g
        g["asking"] = True
        g["asks"] += 1
        g["last_ask"] = now
        state_text = self._gate_state_text(elapsed)
        plan_n = len(st.get("plan") or [])
        steps = self.step_count

        def _ask():
            try:
                p = self.liquid.predict_long_task(state_text) \
                    if self.liquid.configured else None
                if p is not None:
                    g["p"] = round(float(p), 3)
                    g["source"] = f"liquid {self.liquid.model}"
                    self.dlog("gate", f"decision={g.get('decision')} "
                                      f"p={p} (source={g['source']})")
                    g["decision"] = "long" if p >= 0.5 else "short"
                else:
                    long_task = plan_n >= 3 or steps >= 4 or elapsed >= 25
                    g["p"] = 0.85 if long_task else 0.3
                    g["source"] = "heuristic (no LIQUID_API_KEY)"
                    g["decision"] = "long" if long_task else "short"
            except Exception:
                g["decision"] = "short"
                g["source"] = "liquid error → short"
            finally:
                g["asking"] = False
        threading.Thread(target=_ask, daemon=True).start()
        return g

    # -- System-1 activity classification (ambiguous moments) -------------
    SCREEN_WORK = {"thinking", "searching_system", "searching_web",
                   "writing_code", "installing", "downloading",
                   "compiling", "running_tests", "waiting_for_user",
                   "asking_question"}
    # Settings: how long a non-work screen verdict must persist
    # before a screen-driven turn counts as ended.
    TURN_END_CONFIRM_S = float(
        os.environ.get("AAT_TURN_END_CONFIRM_S", "4"))

    def _screen_turn_tick(self, agent_name, choice, conf):
        """End-of-turn detection for adapter-less agents.

        The process observer often sees an interactive agent (Codex)
        as idle for its whole turn — the work happens in-process.
        The screen verdicts are then the only turn signal: work
        verdicts open a screen turn, and 'finished'/'idle_at_prompt'
        after a real turn arm the DONE window, just like the
        process-driven path does."""
        if choice in self.SCREEN_WORK and conf >= 0.45:
            self._screen_end_count = 0
            self._screen_end_since = 0.0
            if not getattr(self, "_screen_turn_active", False):
                self._screen_turn_active = True
                self._screen_turn_failed = False
                self._screen_turn_since = time.time()
                self._screen_turn_agent = agent_name
                self._finalized_at = 0.0
                self._start_baseline()
            oc = self._last_outcome
            if oc and oc.get("mode") == "done" \
                    and oc.get("agent") == agent_name:
                # Fresh work evidence cancels a stale DONE: the
                # agent demonstrably resumed (its previous
                # "ending" was a mid-turn prompt flash).
                self._last_outcome = None
                self.dlog("mode", "sticky done cleared: work "
                                  f"resumed ({agent_name})")
            return
        if choice == "failed":
            if getattr(self, "_screen_turn_active", False):
                self._screen_turn_failed = True
            vanish = getattr(self, "_vanish", None)
            if vanish and vanish["agent"] == agent_name:
                # Not instant: error lines may still scroll past
                # before a successful summary. Remember and let
                # the refresh loop resolve with the last verdict.
                vanish["last_choice"] = "failed"
            return
        if choice in ("finished", "idle_at_prompt"):
            active = getattr(self, "_screen_turn_active", False)
            started = getattr(self, "_screen_turn_since", 0) or 0
            vanish = getattr(self, "_vanish", None)
            if vanish and vanish["agent"] == agent_name:
                # The processes already proved a turn happened;
                # a success summary resolves at once, anything
                # else is remembered for the refresh loop.
                if choice == "finished" and conf >= 0.25:
                    self._last_outcome = {
                        "key": vanish.get("key"),
                        "agent": agent_name, "mode": "done"}
                    self._vanish = None
                    self.dlog("mode", "vanished agent finished -> "
                                      f"sticky done ({agent_name})")
                    self._screen_turn_active = False
                else:
                    vanish["last_choice"] = choice
                return
            if active and agent_name == getattr(
                    self, "_screen_turn_agent", None) \
                    and time.time() - started >= 5 \
                    and not getattr(self, "_screen_turn_failed",
                                    False):
                # One prompt flash mid-turn must not end it
                # (Codex shows its prompt between steps while
                # "Working (...)" continues). The end verdict
                # has to persist: >= 2 confident verdicts over
                # >= TURN_END_CONFIRM_S seconds. Weak verdicts
                # neither advance nor reset the confirmation.
                if conf >= 0.35:
                    now = time.time()
                    if not getattr(self, "_screen_end_since", 0):
                        self._screen_end_since = now
                        self._screen_end_count = 0
                    self._screen_end_count = getattr(
                        self, "_screen_end_count", 0) + 1
                    if self._screen_end_count >= 2 \
                            and now - self._screen_end_since \
                            >= self.TURN_END_CONFIRM_S:
                        self._last_outcome = {
                            "key": getattr(self,
                                           "_agent_key_now", None),
                            "agent": agent_name, "mode": "done"}
                        self.dlog("mode", "screen turn ended -> "
                                          "sticky done "
                                          f"({agent_name}, via "
                                          f"{choice}, confirmed "
                                          f"{self._screen_end_count}x)")
                        self._screen_end_count = 0
                        self._screen_end_since = 0.0
                        self._screen_turn_active = False
                        self._finish_observed_turn(
                            agent_name,
                            "failed" if getattr(
                                self, "_screen_turn_failed",
                                False) else "completed",
                            int(time.time() - started))
                return
            self._screen_turn_active = False

    def classify_tick(self, agent: dict, screen: str = ""):
        """When the event stream only says generic "working" (no tool),
        ask Liquid d1.1 what the agent is actually doing. Facts from the
        log always win; this only fills the ambiguous gaps, throttled
        to one call per 6 s, result trusted for 9 s.

        Agents without a log adapter (Codex, Claude Code) are instead
        understood from their terminal screen: System 1 reads the
        console text (see LiquidClient.classify_screen)."""
        if agent.get("agent") \
                and agent.get("source") != "agent_event" and not screen:
            if not getattr(self, "_noscreen_logged", False):
                self._noscreen_logged = True
                self.dlog("system1", "screen text unavailable: "
                                     "classifier idle")
            return
        if screen:
            self._screen_tail = screen[-1500:]
        if screen and agent.get("agent") \
                and agent.get("source") != "agent_event":
            if not self.liquid.configured:
                if not getattr(self, "_nokey_logged", False):
                    self._nokey_logged = True
                    self.dlog("system1", "screen understanding OFF: "
                                         "no LIQUID_API_KEY found")
                return
            if self._classifying:
                return
            now = time.time()
            if now - getattr(self, "_screen_at", 0) < 4:
                return
            self._screen_at = now
            self._classifying = True

            def _ask_screen():
                self.liquid_stats["screen_calls"] += 1
                if self.liquid_stats["screen_calls"] == 1:
                    self.dlog("system1", f"reading screen "
                                         f"({len(screen)} chars)")
                try:
                    out = self.liquid.classify_screen(
                        screen, agent.get("agent") or "agent")
                    _lines = [ln for ln in screen.splitlines()
                              if ln.strip()]
                    if out and out["choice"] in (
                            "idle_at_prompt", "finished") and re.search(
                            r"esc to interrupt|Working \(\d",
                            "\n".join(_lines[-8:])):
                        # The screen itself proves work is in
                        # progress ("Working (1m 42s - esc to
                        # interrupt)"); a model verdict cannot
                        # overrule visible text.
                        out = {**out, "choice": "thinking",
                               "mode": "flash",
                               "confidence": max(
                                   out["confidence"], 0.5)}
                        self.dlog("system1", "screen marker veto: "
                                             "working text visible, "
                                             "verdict -> thinking")
                    if out:
                        self.liquid_stats["screen_ok"] += 1
                        self.liquid_stats["last_choice"] = out["choice"]
                        self.liquid_stats["last_conf"] = out["confidence"]
                        self.dlog("system1",
                                  f"screen -> {out['choice']} "
                                  f"({out['confidence']:.2f}), "
                                  f"mode={out['mode']}")
                        self._screen_turn_tick(
                            agent.get("agent"), out["choice"],
                            out["confidence"])
                    if out and out["confidence"] >= 0.45 and not (
                            out["mode"] in ("shuffle", "question")
                            and out["confidence"] < 0.6):
                        # Display hysteresis: work verdicts apply
                        # at once, but a single "rest" verdict
                        # (blinky/done) between steps must not
                        # drop the animation and yank the pane
                        # back to the terminal — rest needs two
                        # consecutive samples, like turn ends.
                        if out["mode"] in ("blinky", "done"):
                            cur = getattr(self, "mode_override",
                                          None)
                            fresh = cur and time.time() \
                                - cur.get("at", 0) < 9
                            if fresh and cur.get("mode") not in (
                                    "blinky", "done", None):
                                self._rest_pending = getattr(
                                    self, "_rest_pending", 0) + 1
                                if self._rest_pending < 2:
                                    self.dlog(
                                        "system1",
                                        "rest verdict held "
                                        f"({self._rest_pending}/2)")
                                    out = None
                                else:
                                    self._rest_pending = 0
                            else:
                                self._rest_pending = 0
                        else:
                            self._rest_pending = 0
                    if out and out["confidence"] >= 0.45 and not (
                            out["mode"] in ("shuffle", "question")
                            and out["confidence"] < 0.6):
                        self.mode_override = {
                            "mode": out["mode"], "at": time.time(),
                            "choice": out["choice"],
                            "confidence": out["confidence"],
                            "screen": True}
                except Exception as exc:
                    self.liquid_stats["screen_err"] += 1
                    self.dlog("system1", f"screen classify error: {exc}")
                finally:
                    self._classifying = False
            threading.Thread(target=_ask_screen, daemon=True).start()
            return
        if not self.liquid.configured or self._classifying:
            return
        if agent.get("state") not in BUSY:
            return
        now = time.time()
        if now - self._classify_at < 6:
            return
        self._classify_at = now
        self._classifying = True
        state_text = self._gate_state_text(
            now - (self._busy_since or now)) + \
            f"\nCurrent activity line: {agent.get('activity') or ''}"

        def _ask():
            try:
                out = self.liquid.classify_activity(state_text)
                if out:
                    mode = {"thinking": "flash",
                            "searching_system": "glitch",
                            "searching_web": "trail",
                            "writing_code": "build",
                            "waiting_for_user": "shuffle"}.get(out["choice"])
                    if mode and not (mode == "shuffle"
                                     and out["confidence"] < 0.65):
                        self.mode_override = {
                            "mode": mode, "at": time.time(),
                            "choice": out["choice"],
                            "confidence": out["confidence"]}
            except Exception:
                pass
            finally:
                self._classifying = False
        threading.Thread(target=_ask, daemon=True).start()

    # -- end-of-session results ------------------------------------------
    def clear_outcome(self, reason: str):
        if self._last_outcome:
            self._last_outcome = None
            self.dlog("mode", f"outcome cleared ({reason})")

    def _finish_session(self, final_state: str):
        self._last_outcome = {
            "key": f"muse:{self.adapter.state.get('session_id') or self._session_key or ''}",
            "agent": "muse",
            "mode": "done" if final_state == "completed" else "fail"}
        if self._baseline is not None:
            files = session_diff(self.project_path, self._baseline)
        else:
            files = git_changes_scoped(self.project_path)
            if files is None:
                files = [{"file": f, "add": 1, "del": 0}
                         for f in self.stats["files_touched"]]
        self.results = {"files": files, "completion": None,
                        "safety": None, "source": "pending",
                        "final_state": final_state}
        st = dict(self.agent_state)
        stats = {"files_touched": list(self.stats["files_touched"]),
                 "events": self.stats["events"]}
        results_ref = self.results

        def _score():
            plan = st.get("plan") or []
            done = sum(1 for i in plan if i.get("status") == "done")
            adds = sum(f["add"] for f in files)
            dels = sum(f["del"] for f in files)
            tools = sorted({str(ev.get("tool")) for ev in self.timeline
                            if ev.get("tool")})
            reply = (st.get('last_reply') or st.get('activity') or '')[:2500]
            state_text = (
                f"Task given to a coding agent: {st.get('task') or '(unknown)'}\n"
                f"Plan: {done}/{len(plan)} steps done. "
                f"Final state: {final_state}.\n"
                f"Agent's final reply: {reply}\n"
                f"Files changed during this session (measured, not "
                f"the whole repo state): {len(files)} files, "
                f"+{adds} -{dels} lines. Files: "
                + ", ".join(f"{f['file']} (+{f['add']}/-{f['del']})"
                            for f in files[:14])
                + f"\nTools used: {', '.join(tools) or 'unknown'}. "
                f"Events: {stats['events']}.")
            try:
                ev = self.liquid.evaluate_session(state_text) \
                    if self.liquid.configured else None
            except Exception:
                ev = None
            if ev:
                results_ref["completion"] = ev["completion"]
                results_ref["safety"] = ev["safety"]
                results_ref["source"] = f"liquid {self.liquid.model}"
            else:
                ratio = (done / len(plan)) if plan else (
                    0.75 if final_state == "completed" else 0.0)
                comp = round(4 * ratio)
                del_ratio = dels / max(adds + dels, 1)
                safe = 4 - (1 if len(files) > 8 else 0) \
                    - (1 if del_ratio > 0.4 else 0) \
                    - (1 if final_state == "failed" else 0)
                results_ref["completion"] = {
                    "score": float(comp), "confidence": 0.0}
                results_ref["safety"] = {
                    "score": float(max(0, min(4, safe))), "confidence": 0.0}
                results_ref["source"] = "heuristic (no LIQUID_API_KEY)"
            self.dlog(
                "score",
                f"to System-1: task={_s(st.get('task') or '?', 40)!r} "
                f"files={len(files)} reply={len(reply)}ch -> "
                f"completion={results_ref['completion']['score']}/4 "
                f"({results_ref['completion'].get('confidence', 0):.0%}), "
                f"safety={results_ref['safety']['score']}/4 "
                f"[{results_ref['source']}]")

        threading.Thread(target=_score, daemon=True).start()

    def on_snapshot(self, snap: dict):
        self.snapshot = snap
        ev = snap.get("event")
        if ev:
            self.timeline.appendleft(ev)

    def dlog(self, tag: str, msg: str):
        """Decision log: every meaningful choice the visualization
        makes, with its source. Shown in Diagnostics, kept in
        DATA_DIR/decisions.log — this is how we prove what ran."""
        line = f"{time.strftime('%H:%M:%S')} [{tag}] {msg}"
        self.decisions.append(line)
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(DATA_DIR / "decisions.log", "a",
                      encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass

    def refresh_agent_state(self):
        """Adapter facts win; observer heuristics are the fallback."""
        if self.sim_feed is not None and self.sim_feed.state:
            self.agent_state = dict(self.sim_feed.state)
            return
        snap = self.snapshot
        inferred = snap.get("inferred") or {}
        fg_cmd = (snap.get("foreground") or {}).get("cmdline", "")
        cli = snap.get("agent_cli_detected")
        # Is an adapter-less agent actually working right now?
        obs_busy = False
        if cli:
            apid0 = snap.get("agent_pid")
            obs_busy = inferred.get("state") in ("working",
                                                 "tool_running")
            if apid0:
                fam = {apid0}
                changed = True
                tree0 = snap.get("process_tree") or []
                while changed:
                    changed = False
                    for pr in tree0:
                        if pr.get("ppid") in fam \
                                and pr.get("pid") not in fam:
                            fam.add(pr.get("pid"))
                            changed = True
                obs_busy = len(fam) > 1
        with self._lock:
            adapter_state = self.adapter_state
        if self.adapter.current_file and adapter_state:
            astate = adapter_state.get("state")
            # Adapter facts win — unless Muse is just sitting there
            # while another agent does the work.
            if astate in BUSY_STATES + ("awaiting_user",) or not obs_busy:
                merged = dict(adapter_state)
                merged["agent"] = "muse"
                self.agent_state = merged
                return
        if cli:
            # An interactive agent (Codex, ...) IS the foreground
            # process even when it just sits at its prompt. It is
            # only busy while it has child processes doing work.
            apid = snap.get("agent_pid")
            busy = inferred.get("state") in ("working", "tool_running")
            if apid:
                tree = snap.get("process_tree") or []
                family = {apid}
                changed = True
                while changed:
                    changed = False
                    for pr in tree:
                        if pr.get("ppid") in family \
                                and pr.get("pid") not in family:
                            family.add(pr.get("pid"))
                            changed = True
                busy = len(family) > 1
            self.agent_state = {
                "agent": cli, "state": "working" if busy else "idle",
                "activity": (snap.get("foreground") or {}).get("cmdline", ""),
                "command": fg_cmd,
                "source": "heuristic",
                "confidence": inferred.get("confidence"),
                "files": [], "plan": [],
            }
        else:
            self.agent_state = {
                "agent": None, "state": inferred.get("state", "idle"),
                "activity": fg_cmd[:110] or
                (snap.get("foreground") or {}).get("process", ""),
                "command": fg_cmd,
                "source": "heuristic", "files": [], "plan": [],
            }
        # Identity of the current agent session: the end-of-turn
        # outcome sticks to it; a new process/session floats again.
        _ag = self.agent_state.get("agent")
        akey = (f"{_ag}:{self.agent_state.get('session_id') or (self.snapshot or {}).get('agent_pid') or self.adapter.current_file or ''}"
                if _ag else None)
        self._agent_key_now = akey
        sig = (self.agent_state.get("agent"), self.agent_state.get("state"),
               self.agent_state.get("source"))
        if sig != self._prev_sig:
            prev = self._prev_sig
            self._prev_sig = sig
            self.dlog("state", f"{sig[0] or 'no-agent'} · {sig[1]} "
                               f"(source={sig[2]})")
            busy_states = ("working", "tool_running")
            if sig[1] in busy_states:
                if self._busy_started_at is None:
                    self._busy_started_at = time.time()
                if not prev or prev[1] not in busy_states:
                    self._finalized_at = 0.0
                    self._start_baseline()
                if self._last_outcome:
                    self._last_outcome = None
                    self.dlog("mode", "outcome cleared (new turn)")
            elif prev and prev[0] and prev[0] == sig[0] \
                    and prev[1] in busy_states and sig[1] == "idle" \
                    and sig[2] == "heuristic" \
                    and self._busy_started_at \
                    and time.time() - self._busy_started_at >= 5:
                # An adapter-less agent finished a real turn: the
                # outcome sticks until the next turn / new session.
                self._last_outcome = {"key": self._agent_key_now,
                                      "agent": sig[0], "mode": "done"}
                self.dlog("mode", f"turn ended -> sticky done "
                                  f"({sig[0]})")
                self._finish_observed_turn(
                    sig[0], "completed",
                    int(time.time() - self._busy_started_at))
            if sig[1] not in busy_states:
                if self._busy_started_at:
                    self._last_turn_secs = int(
                        time.time() - self._busy_started_at)
                self._busy_started_at = None
        # Agent vanished mid-turn (process exited while busy and
        # never reported an ending): a brief interrupted window
        # beats silently falling back to the float.
        prev_view = getattr(self, "_agent_view", (None, None))
        cur_agent = self.agent_state.get("agent")
        if prev_view[0] and prev_view[1] in (
                "working", "tool_running", "planning", "starting") \
                and not cur_agent \
                and not getattr(self, "_vanish", None):
            # Finished-and-exited looks exactly like died-mid-turn
            # for one tick. Give the screen a few seconds to tell
            # the ending before calling it an interruption.
            self._vanish = {"agent": prev_view[0], "at": time.time(),
                            "key": getattr(self, "_agent_key_now", None)}
            self.dlog("mode", "agent vanished; resolving ending "
                              f"({prev_view[0]})")
        self._agent_view = (cur_agent, self.agent_state.get("state"))
        if cur_agent:
            self._vanish = None
        vanish = getattr(self, "_vanish", None)
        if vanish and not cur_agent:
            age = time.time() - vanish["at"]
            last = vanish.get("last_choice")
            if age > 6 and last == "failed":
                self._last_outcome = {
                    "key": vanish.get("key"),
                    "agent": vanish["agent"], "mode": "fail"}
                self._vanish = None
                self.dlog("mode", "vanished agent failed -> sticky "
                                  f"fail ({vanish['agent']})")
                self._finish_observed_turn(
                    vanish["agent"], "failed",
                    getattr(self, "_last_turn_secs", 0))
            elif age > 3 and last == "finished":
                self._last_outcome = {
                    "key": vanish.get("key"),
                    "agent": vanish["agent"], "mode": "done"}
                self._vanish = None
                self.dlog("mode", "vanished agent finished -> sticky "
                                  f"done ({vanish['agent']})")
                self._finish_observed_turn(
                    vanish["agent"], "completed",
                    getattr(self, "_last_turn_secs", 0))
            elif age > 6 and last == "idle_at_prompt":
                self._interrupt_until = time.time() + 5
                self._interrupt_agent = vanish["agent"]
                self._vanish = None
                self.dlog("mode", "agent vanished mid-turn -> "
                                  f"interrupted ({vanish['agent']})")
                self._finish_observed_turn(
                    vanish["agent"], "interrupted",
                    getattr(self, "_last_turn_secs", 0))
            elif age > 14:
                self._interrupt_until = time.time() + 5
                self._interrupt_agent = vanish["agent"]
                self._vanish = None
                self.dlog("mode", "agent vanished mid-turn -> "
                                  "interrupted "
                                  f"({vanish['agent']}, unresolved)")
                self._finish_observed_turn(
                    vanish["agent"], "interrupted",
                    getattr(self, "_last_turn_secs", 0))

        if not cur_agent and time.time() < getattr(
                self, "_interrupt_until", 0):
            self.agent_state = dict(self.agent_state)
            self.agent_state.update({
                "agent": self._interrupt_agent, "interrupted": True,
                "activity": "Agent exited mid-turn"})
        oc = self._last_outcome
        if oc and oc.get("key") is None:
            oc["key"] = akey
        if oc and akey is not None and oc.get("key") != akey:
            self._last_outcome = None
            oc = None
            self.dlog("mode", "outcome cleared (new agent session)")
        if oc and self.agent_state.get("state") == "idle":
            if self.agent_state.get("agent"):
                self.agent_state = dict(self.agent_state)
                if oc["mode"] == "done":
                    self.agent_state["turn_done"] = True
                self.agent_state["sticky_outcome"] = oc["mode"]
            else:
                # Agent exited after finishing: keep showing how
                # it ended until something new happens.
                self.agent_state = {
                    "agent": oc.get("agent"), "state": "idle",
                    "sticky_outcome": oc["mode"],
                    "source": "heuristic", "files": [], "plan": []}

    # -- Muse Spark / Muse Image (threads) ------------------------------
    def enrich_async(self):
        if not self.project or self.enrich_status == "generating":
            return
        if not self.meta.configured:
            self.enrich_status = "no-key"
            return
        self.enrich_status = "generating"

        def _run():
            import json
            from backend.app.content import spark_cards
            try:
                cards = spark_cards(self.project, self.meta)
                if cards:
                    self.enriched = cards
                    CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    (CACHE_DIR / f"cards-{self.project['id']}.json").write_text(
                        json.dumps(cards, ensure_ascii=False))
                    self.enrich_status = "ready"
                else:
                    self.enrich_status = "error"
            except Exception:
                self.enrich_status = "error"
        threading.Thread(target=_run, daemon=True).start()

    def world_async(self):
        if not self.project or self.world_status == "generating":
            return
        path = CACHE_DIR / f"world-{self.project['id']}.webp"
        if path.is_file():
            self.world_path = path
            self.world_status = "ready"
            return
        if not self.meta.configured:
            self.world_status = "no-key"
            return
        self.world_status = "generating"

        def _run():
            from backend.app.meta_client import world_prompt
            try:
                data = self.meta.generate_image(world_prompt(self.project))
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                self.world_path = path
                self.world_status = "ready"
            except Exception:
                self.world_status = "error"
        threading.Thread(target=_run, daemon=True).start()

    def poster_async(self):
        if not self.project or self.poster_status == "generating":
            return
        path = CACHE_DIR / f"poster-{self.project['id']}.webp"
        if path.is_file() and self.poster_status == "ready":
            self.poster_path = path
            return
        if not self.meta.configured:
            self.poster_status = "no-key"
            return
        self.poster_status = "generating"

        def _run():
            from backend.app.meta_client import poster_prompt
            try:
                if self.world_path and self.world_path.is_file():
                    data = self.meta.edit_image(
                        self.world_path.read_bytes(),
                        poster_prompt(self.project,
                                      self.agent_state.get("activity", "")))
                else:
                    data = self.meta.generate_image(
                        poster_prompt(self.project,
                                      self.agent_state.get("activity", "")))
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                self.poster_path = path
                self.poster_status = "ready"
            except Exception:
                self.poster_status = "error"
        threading.Thread(target=_run, daemon=True).start()

    def simulate(self):
        """Dev fallback: replay the event contract without an agent."""
        if not self.project:
            return
        project = self.project

        def _run():
            def emit(**kw):
                ev = {"agent": "muse (demo)", "state": "working",
                      "activity": "", "tool": None, "files": [],
                      "source": "simulation",
                      "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                 time.gmtime())}
                ev.update(kw)
                self._on_agent_event(ev)
                self.adapter_state = ev
            emit(state="starting", activity="Muse session started (simulation)")
            time.sleep(1.2)
            emit(state="planning", activity="Task: explore the project",
                 plan=[{"status": "in_progress", "text": "Explore the structure"},
                       {"status": "pending", "text": "Update the modules"},
                       {"status": "pending", "text": "Run the tests"}])
            time.sleep(1.4)
            for c in project["components"][:4]:
                f = c["files"][0] if c["files"] else c["path"]
                emit(state="tool_running", tool="file_read",
                     activity=f"read: {f}", files=[f])
                time.sleep(1.1)
                emit(state="tool_running", tool="file_edit",
                     activity=f"edit: {f}", files=[f])
                time.sleep(1.1)
            emit(state="tool_running", tool="bash",
                 activity="bash: run tests", files=[])
            time.sleep(1.6)
            emit(state="completed",
                 activity="Done: modules described, tests pass")
        threading.Thread(target=_run, daemon=True).start()


class VtePtyInfo:
    """Adapts a VTE terminal to the SessionObserver pty interface."""

    def __init__(self, vte):
        self.vte = vte
        self.pid: int | None = None

    def foreground_pid(self) -> int | None:
        try:
            pty = self.vte.get_pty()
            return os.tcgetpgrp(pty.get_fd())
        except Exception:
            return self.pid


# ---------------------------------------------------------------------------
# UI helpers ported from the redesign brief
# ---------------------------------------------------------------------------

def short(s: str, n: int = 150) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def pretty_activity(agent: dict) -> str:
    import json
    import re
    text = str(agent.get("activity") or "")
    m = re.match(r"^(?:bash|read_file|file_edit|file_read):\s*(\{.*)$", text)
    if m:
        try:
            j = json.loads(m[1])
            text = j.get("command") or j.get("path") or j.get("file_path") or text
        except Exception:
            pass
    return short(str(text).split("\n")[0], 150)


def status_view(agent: dict, comp: dict | None):
    st = agent.get("state") or "idle"
    act = pretty_activity(agent)
    name = str(agent.get("agent") or "agent").upper()
    if st == "awaiting_user":
        return ("attention", f"{name} · NEEDS ATTENTION", "The agent needs you",
                act or "Your action is needed in the terminal")
    if st == "completed":
        return ("done", f"{name} · COMPLETED", "Done", act)
    if st == "failed":
        return ("failed", f"{name} · FAILED", "Something went wrong", act)
    if st == "planning":
        return ("busy", f"{name} · THINKING", "Thinking…", act)
    if st == "starting":
        return ("busy", f"{name} · STARTING", "Starting…", act)
    if st in ("working", "tool_running"):
        text = f"{agent.get('tool') or ''} {agent.get('activity') or ''}"
        files = agent.get("files") or []
        if any(k in text.lower() for k in ("pytest", "unittest", "test")):
            return ("busy", f"{name} · WORKING", "Running tests", act)
        tool = str(agent.get("tool") or "")
        if any(k in tool for k in ("read", "grep", "glob", "fetch")):
            return ("busy", f"{name} · WORKING",
                    f"Reading {comp['name']}" if comp else "Reading code",
                    files[0] if files else act)
        if any(k in tool for k in ("edit", "write", "patch")):
            return ("busy", f"{name} · WORKING",
                    f"Editing {comp['name']}" if comp else "Editing code",
                    files[0] if files else act)
        if tool == "bash":
            return ("busy", f"{name} · WORKING", "Running a command", act)
        return ("busy", f"{name} · WORKING",
                f"Working on {comp['name']}" if comp else "Working", act)
    return ("idle", "AGENT MODE", "Waiting for an agent",
            "Start Muse in the terminal — its work will appear here")


# ---------------------------------------------------------------------------
# Minimal overlay: system-theme surfaces, grotesk type, one animated square.
# The square is the agent-activity indicator (field-guide modes):
#   blinky   idle / done / no agent connected
#   glitch   searching the local system (files, code, processes)
#   trail    searching the web
#   shuffle  the agent needs the user - click returns to the terminal
#   flash    thinking / planning ("color flash")
#   build    writing code or files, running builds and tests ("build on")
# ---------------------------------------------------------------------------

PALETTES = {
    "dark": {  # Linear-style tokens (see README design notes)
        "bg": "#08090a", "surface": "#141516", "surface2": "#1c1c1f",
        "border": "#23252a", "border2": "#34343a",
        "text": "#f7f8f8", "body": "#d0d6e0", "muted": "#8a8f98",
        "faint": "#62666d", "square": (0.969, 0.973, 0.973),
        "vte_bg": (0.031, 0.035, 0.039), "vte_fg": (0.816, 0.839, 0.878),
        "bg_rgb": (0.031, 0.035, 0.039),
    },
    "light": {
        "bg": "#f7f8f8", "surface": "#ffffff", "surface2": "#eef0f2",
        "border": "#e4e5e9", "border2": "#d3d7dd",
        "text": "#0b0c0e", "body": "#34343a", "muted": "#62666d",
        "faint": "#8a8f98", "square": (0.078, 0.082, 0.09),
        "vte_bg": (0.969, 0.973, 0.973), "vte_fg": (0.11, 0.11, 0.12),
        "bg_rgb": (0.969, 0.973, 0.973),
    },
}

GROTESK = "'Inter', 'Manrope', 'Helvetica Neue', sans-serif"
MONO = "'JetBrains Mono', 'Berkeley Mono', monospace"


def build_css(dark: bool) -> bytes:
    p = PALETTES["dark" if dark else "light"]
    return f"""
.scene {{ background: {p['bg']}; }}
headerbar {{ background: {p['bg']}; color: {p['text']}; border-bottom: 1px solid {p['border']}; box-shadow: none; }}
headerbar button {{ background: transparent; color: {p['body']}; border: none; box-shadow: none; }}
headerbar button:hover {{ background: {p['surface2']}; color: {p['text']}; }}
.sys1-banner {{
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px; letter-spacing: 0.08em;
  color: {p['body']}; border: 1px solid {p['border']};
  border-radius: 999px; padding: 4px 12px;
}}
.content-soon {{
  color: {p['body']}; font-family: {GROTESK}; font-size: 30px; font-weight: 600;
  letter-spacing: -0.5px;
}}
.content-sub {{ color: {p['muted']}; font-family: {GROTESK}; font-size: 14px; }}
.content-kicker {{ color: {p['muted']}; font-family: {MONO}; font-size: 10px; letter-spacing: 2.5px; }}
.content-body {{ color: {p['body']}; font-family: {GROTESK}; font-size: 14px; }}
.content-q {{ color: {p['text']}; font-family: {GROTESK}; font-size: 16px; font-weight: 600; }}
button.choice {{ background: transparent; color: {p['body']}; border: 1px solid {p['border']}; border-radius: 12px; padding: 12px 14px; font-family: {GROTESK}; font-size: 15px; box-shadow: none; }}
button.choice:hover {{ border-color: {p['text']}; color: {p['text']}; }}
button.choice-right {{ background: {p['text']}; color: {p['bg']}; border-color: {p['text']}; }}
.fb-ok {{ font-size: 15px; font-weight: 600; color: {p['text']}; }}
button.react-on {{ background: {p['surface2']}; color: {p['text']}; }}
.fb-no {{ font-size: 15px; font-weight: 600; color: {p['muted']}; }}
button.choice-wrong {{ color: {p['muted']}; border-color: {p['muted']}; }}
button.game-next {{ background: transparent; color: {p['text']}; border: none; border-radius: 9999px; padding: 7px 16px; font-family: {GROTESK}; font-size: 13px; box-shadow: none; }}
button.game-next:hover {{ background: {p['surface2']}; }}
button.icon-btn {{ background: transparent; color: {p['body']}; border: none; border-radius: 9999px; min-width: 32px; min-height: 32px; padding: 4px; font-family: {GROTESK}; font-size: 15px; box-shadow: none; }}
button.icon-btn:hover {{ background: {p['surface2']}; color: {p['text']}; }}
button.quiet {{ color: {p['muted']}; background: transparent; border: none; box-shadow: none; font-family: {GROTESK}; font-size: 15px; padding: 4px 8px; }}
button.quiet:hover {{ color: {p['text']}; }}
.mode-label {{ color: {p['muted']}; font-family: {MONO}; font-size: 11px; letter-spacing: 3px; }}
.mode-title {{ color: {p['text']}; font-family: {GROTESK}; font-size: 24px; font-weight: 500; letter-spacing: -0.3px; }}
.mode-sub {{ color: {p['muted']}; font-family: {GROTESK}; font-size: 14px; }}
.hint {{ color: {p['faint']}; font-family: {GROTESK}; font-size: 12px; }}
.res-label {{ color: {p['muted']}; font-family: {MONO}; font-size: 10px; letter-spacing: 2px; }}
.panel {{ background: {p['surface']}; color: {p['body']}; border-left: 1px solid {p['border']}; }}
.panel textview, .panel textview text {{ background: {p['bg']}; color: {p['body']}; font-family: {MONO}; font-size: 11px; }}
.panel entry {{ background: {p['bg']}; color: {p['text']}; }}
""".encode()


MODE_CAPTIONS = {
    "question": ("QUESTION", "The agent is asking"),
    "fail_compile": ("BUILD FAILED", "Build failed"),
    "fail_link": ("LINK FAILED", "Linking failed"),
    "fail_test": ("TESTS FAILED", "Tests failed"),
    "fail": ("FAILED", "Something failed"),
    "download": ("DOWNLOADING", "Downloading"),
    "test": ("TESTING", "Running tests"),
    "git": ("GIT", "Working with git"),
    "extract": ("EXTRACTING", "Extracting"),
    "serve": ("SERVING", "Server running"),
    "delete": ("DELETING", "Deleting files"),
    "compile": ("COMPILING", "Compiling"),
    "link": ("LINKING", "Linking"),
    "copy": ("COPYING", "Copying files"),
    "install": ("INSTALLING", "Installing"),
    "done": ("DONE", "Done"),
    "blinky": ("", ""),
    "glitch": ("SCANNING", "Searching the system"),
    "trail": ("TRAIL", "Searching the web"),
    "shuffle": ("SHUFFLE", "The agent needs you"),
"flash": ("THINKING", "Thinking"),
    "build": ("BUILD ON", "Writing code"),
}


WEB_TOOLS = {"web_search", "web_fetch"}
LOCAL_SEARCH_TOOLS = {"search", "grep", "glob", "read_file", "file_read",
                      "read", "read_skill", "list_dir", "list_files"}


def _coerce_vte_text(out) -> str:
    if isinstance(out, tuple):
        out = out[0] if out else ""
    return out or ""


_VTE_BROKEN = set()


def _read_vte_text(term) -> str:
    """Best-effort read of the terminal's text. Some VTE builds
    return None from get_text() (a C-side attributes assertion,
    printed on every call), so try the alternatives — and never
    retry a strategy that already failed on this build, or the
    assertion floods the launching terminal."""
    if "get_text" not in _VTE_BROKEN:
        try:
            text = _coerce_vte_text(term.get_text())
            if text:
                return text
            _VTE_BROKEN.add("get_text")
        except Exception:
            _VTE_BROKEN.add("get_text")
    if "get_text_range" not in _VTE_BROKEN:
        try:
            rows = max(int(term.get_row_count()), 1)
            cols = max(int(term.get_column_count()), 1)
            text = _coerce_vte_text(
                term.get_text_range(0, 0, rows, cols))
            if text:
                return text
            _VTE_BROKEN.add("get_text_range")
        except Exception:
            _VTE_BROKEN.add("get_text_range")
    try:
        # get_text()/get_text_range() return None on some VTE builds
        # (deprecated attributes array); dumping the terminal to a
        # file always works.
        from gi.repository import Gio
        path = os.path.join(tempfile.gettempdir(), "aat-screen.txt")
        gfile = Gio.File.new_for_path(path)
        fos = gfile.replace(None, False, Gio.FileCreateFlags.NONE, None)
        ok = term.write_contents_sync(fos, 0, None)
        fos.close(None)
        if ok:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            if text.strip():
                return text
    except Exception:
        pass
    return ""


_s = lambda s, n=60: " ".join(str(s).split())[:n]


def mode_for_agent(agent: dict) -> str:
    """Map the normalized agent state/tool to a square animation mode."""
    import re
    st = agent.get("state") or "idle"
    if agent.get("interrupted"):
        return "fail"
    if st == "completed" or agent.get("turn_done"):
        return "done"
    if st == "awaiting_user":
        return "question" if agent.get("needs") == "answer" else "shuffle"
    if st == "failed":
        return "fail"
    if st == "idle":
        # The end of a turn sticks: no drifting back to the float.
        so = agent.get("sticky_outcome")
        if so in ("done", "fail"):
            return so
    lf = agent.get("last_failure")
    if lf and st in ("working", "tool_running"):
        at = lf.get("at")
        if at is None or time.time() - float(at) < 6:
            probe = {"state": "tool_running",
                     "tool": lf.get("tool") or "bash",
                     "command": lf.get("command") or "",
                     "activity": ""}
            return {"compile": "fail_compile", "link": "fail_link",
                    "test": "fail_test"}.get(mode_for_agent(probe), "fail")
    if st in ("planning", "starting"):
        return "flash"
    if st in ("working", "tool_running"):
        tool = (agent.get("tool") or "").lower()
        cmd = (agent.get("command") or "").lower()
        act = (agent.get("activity") or "").lower()
        if cmd:
            # What kind of shell work is this? (facts from the command)
            lead = r"(?:^|[;&|]\s*)(?:sudo\s+)?(?:-\S+\s+)*"
            if re.search(lead + r"(rm|unlink|shred|rmdir)\b", cmd):
                return "delete"
            if re.search(r"\b(apt|apt-get|dnf|yum|pacman)\b[^;&|]*\b"
                         r"(update|upgrade|dist-upgrade|install|remove)\b"
                         r"|\b(pip3?|uv|npm|pnpm|yarn|brew|gem|poetry"
                         r"|conda)\b[^;&|]*\b(install|add)\b"
                         r"|\bcargo\s+add\b|\bgo\s+get\b", cmd):
                return "install"
            if re.search(lead + r"(curl|wget|aria2c?)\b", cmd) \
                    or re.search(r"\bgit\s+(clone|fetch|pull)\b", cmd) \
                    or re.search(r"\bdocker\s+(pull|push)\b", cmd):
                return "download"
            if re.search(r"\bgit\s+(commit|push|add|merge|rebase|checkout"
                         r"|switch|branch|status|log|diff|stash)\b", cmd):
                return "git"
            if re.search(lead + r"(unzip|unar|gunzip|bunzip2|unxz)\b", cmd) \
                    or re.search(r"\btar\b[^;&|]*\bx", cmd) \
                    or re.search(r"\b7z\s+x\b", cmd):
                return "extract"
            if re.search(r"\b(uvicorn|gunicorn|waitress|hypercorn)\b"
                         r"|\bflask\s+run\b|\brunserver\b"
                         r"|\bhttp\.server\b|\bnpm\s+(start|run\s+dev)\b"
                         r"|\b(vite|next\s+dev|ng\s+serve)\b"
                         r"|\bdocker\s+(run|compose\s+up|up)\b"
                         r"|\bphp\s+-S\b", cmd):
                return "serve"
            if re.search(r"\b(pytest|tox|jest|vitest|mocha|phpunit"
                         r"|rspec|unittest)\b|\b(cargo|go|npm|pnpm|yarn)"
                         r"\s+test\b|\bpython3?\s+-m\s+(pytest"
                         r"|unittest)\b|\bmake\s+(test|check)\b", cmd):
                return "test"
            if re.search(lead + r"(ld|lld|gold|mold)\b", cmd) \
                    or "linking" in act \
                    or (re.search(r"\b(gcc|clang)\b|\bg\+\+", cmd)
                        and "-c" not in cmd and ".o" in cmd):
                return "link"
            if re.search(lead + r"(cp|rsync|scp|sftp|robocopy)\b", cmd):
                return "copy"
            if re.search(r"\b(make|cmake|ninja|gcc|clang|cc|rustc"
                         r"|javac|kotlinc|swiftc|tsc|webpack|mvn|gradle"
                         r"|dotnet)\b|\bg\+\+|\b(cargo|go|npm|pnpm|yarn)\s+"
                         r"(build|check)\b|\bnpm\s+run\s+build\b"
                         r"|\bpython3?\s+-m\s+build\b"
                         r"|\bdocker\s+(build|compose\s+build)\b", cmd):
                return "compile"
        if tool in WEB_TOOLS:
            return "trail"
        if tool in LOCAL_SEARCH_TOOLS:
            return "glitch"
        text = f"{tool} {agent.get('activity') or ''}".lower()
        if re.search(r"web_search|web_fetch|search the web|internet", text):
            return "trail"
        if re.search(r"search|grep|glob|find|read|list|scan", text):
            return "glitch"
        return "build"
    return "blinky"


def load_keys_file():
    """Load API keys from keys.sh by default (real env vars win).

    Search order: $AAT_KEYS_FILE, ./keys.sh, keys.sh next to the app
    (repo root / binary folder), ~/.config/agent-aware-terminal/keys.sh.
    Lines look like: export MODEL_API_KEY=... — values are never logged.
    """
    candidates = []
    if os.environ.get("AAT_KEYS_FILE"):
        candidates.append(Path(os.environ["AAT_KEYS_FILE"]))
    # The built binary lives at dist/agent-aware-terminal/ (plus a
    # PyInstaller _internal dir), so walk up from RES_ROOT to find
    # the project keys.sh wherever the app was launched from.
    candidates.append(Path.cwd() / "keys.sh")
    here = RES_ROOT
    for _ in range(5):
        candidates.append(here / "keys.sh")
        if here.parent == here:
            break
        here = here.parent
    candidates += [
        DATA_DIR / "keys.sh",
        Path.home() / ".config" / "agent-aware-terminal" / "keys.sh",
    ]
    for path in candidates:
        try:
            if not path.is_file():
                continue
            import re
            for line in path.read_text().splitlines():
                m = re.match(
                    r"\s*(?:export\s+)?([A-Z0-9_]+)\s*=\s*(.+?)\s*$", line)
                if not m:
                    continue
                name, value = m.group(1), m.group(2).strip("'\"")
                if name not in os.environ and value:
                    os.environ[name] = value
            return path
        except Exception:
            continue
    return None


def ensure_inter_font():
    """Register the bundled Inter (grotesk) with fontconfig, once."""
    import subprocess
    try:
        src = RES_ROOT / "desktop" / "fonts" / "Inter.ttf"
        if not src.is_file():
            src = RES_ROOT / "fonts" / "Inter.ttf"
        if not src.is_file():
            return
        dst_dir = Path.home() / ".local" / "share" / "fonts" / "agent-aware-terminal"
        dst = dst_dir / "Inter.ttf"
        if not dst.is_file():
            dst_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            subprocess.run(["fc-cache", "-f", str(dst_dir)],
                           capture_output=True, timeout=30)
    except Exception:
        pass


MOCK = os.environ.get("AAT_MOCKSTATE", "")


def run_gui(project_arg: str | None) -> int:
    # Input method: without IBus the app falls back to a bare context
    # and layout/language switching is lost. Force IBus when present
    # (a stray GTK_IM_MODULE from the launching shell must not win).
    import glob as _glob
    IBUS_OK = bool(_glob.glob("/usr/lib/*/gtk-3.0/3.0.0/immodules/im-ibus.so"))
    if IBUS_OK and not os.environ.get("AAT_NO_IBUS"):
        os.environ["GTK_IM_MODULE"] = "ibus"
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    gi.require_version("Vte", "2.91")
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Vte

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    S_KEYS = load_keys_file()
    if S_KEYS:
        print(f"keys loaded from {S_KEYS}", file=sys.stderr)
    ensure_inter_font()
    _settings = Gtk.Settings.get_default()

    def _system_dark() -> bool:
        if _settings is None:
            return False
        try:
            name = (_settings.get_property("gtk-theme-name") or "").lower()
            prefer = _settings.get_property(
                "gtk-application-prefer-dark-theme")
        except Exception:
            return False
        return bool(prefer) or "dark" in name

    # Theme preference: a three-state cycle (auto / light / dark),
    # persisted between runs. AAT_THEME=dark|light still wins as the
    # initial mode (screenshots); auto follows the system theme and
    # switches live when the system does.
    PREFS_PATH = DATA_DIR / "ui_prefs.json"

    def _load_prefs() -> dict:
        try:
            import json as _json
            data = _json.loads(PREFS_PATH.read_text())
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    _prefs = _load_prefs()
    _env_theme = os.environ.get("AAT_THEME", "").lower()
    THEME = {"mode": _env_theme if _env_theme in ("dark", "light")
             else (_prefs.get("theme_mode")
                   if _prefs.get("theme_mode") in ("auto", "light", "dark")
                   else "auto"),
             "dark": False}
    THEME["dark"] = THEME["mode"] == "dark" or (
        THEME["mode"] == "auto" and _system_dark())
    # PAL/PALC are mutated in place on a theme switch: every draw
    # callback already reads them, so they all pick up new tokens.
    PAL = dict(PALETTES["dark" if THEME["dark"] else "light"])

    def _rgb(v):
        if isinstance(v, str) and v.startswith("#"):
            h = v.lstrip("#")
            return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
        return v
    PALC = {k: _rgb(v) for k, v in PAL.items()}
    provider = Gtk.CssProvider()
    provider.load_from_data(build_css(THEME["dark"]))
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _save_prefs():
        try:
            import json as _json
            _prefs["theme_mode"] = THEME["mode"]
            if S.get("view_mode"):
                _prefs["view_mode"] = S["view_mode"]
            PREFS_PATH.write_text(_json.dumps(_prefs))
        except Exception:
            pass

    def apply_theme(mode: str | None = None):
        """Re-skin the whole window for THEME (auto/light/dark)."""
        if mode in ("auto", "light", "dark"):
            THEME["mode"] = mode
        dark = THEME["mode"] == "dark" or (
            THEME["mode"] == "auto" and _system_dark())
        THEME["dark"] = dark
        PAL.clear()
        PAL.update(PALETTES["dark" if dark else "light"])
        PALC.clear()
        PALC.update({k: _rgb(v) for k, v in PAL.items()})
        provider.load_from_data(build_css(dark))
        vte = S.get("vte")
        if vte is not None:
            vte.set_color_background(Gdk.RGBA(*PAL["vte_bg"], 1))
            vte.set_color_foreground(Gdk.RGBA(*PAL["vte_fg"], 1))
        btn = S.get("theme_btn")
        if btn is not None:
            btn.set_tooltip_text(
                {"auto": "Theme: Auto (system) — click for Light",
                 "light": "Theme: Light — click for Dark",
                 "dark": "Theme: Dark — click for Auto"}[THEME["mode"]])
        for widget in S.values():
            if isinstance(widget, Gtk.Widget):
                widget.queue_draw()
        sq = S.get("square")
        if sq is not None:
            sq.queue_draw()
        _save_prefs()
        try:
            core.dlog("ui", f"theme {THEME['mode']} "
                            f"({'dark' if dark else 'light'})")
        except Exception:
            pass

    project_path = project_arg or os.environ.get("AAT_PROJECT") \
        or str(ensure_demo_project())
    core = Core(project_path)
    from backend.app import content_engine as ce
    from backend.app.content_engine import (ContentEngine,
                                            default_content_dirs)
    _content_dirs = default_content_dirs(DATA_DIR, RES_ROOT,
                                         sys.executable)
    # Project-correlated content from the Content Researcher
    # agent lives in <project>/.aat/content and wins first.
    _content_dirs.insert(0, Path(project_path) / ".aat" / "content")
    # Tour/preview mode sandboxes the statistics: the deck and
    # score live in memory only, nothing is written to the real
    # content_state.json.
    _tour_raw = os.environ.get("AAT_TOUR", "")
    _tour_type = os.environ.get("AAT_TOUR_TYPE", "").strip().lower()
    _only_types = {_tour_type} if _tour_type in (
        "game", "news", "idea", "fact") else None
    engine = ContentEngine(
        _content_dirs,
        state_path=None if _tour_raw
        else DATA_DIR / "content_state.json",
        only_types=_only_types)
    core.dlog("content", f"{len(engine.packs)} content packs loaded"
              + (f" (type filter: {_tour_type})" if _only_types
                 else "")
              + (f"; pack errors: {'; '.join(engine.errors[:2])}"
                 if engine.errors else ""))
    from backend.app import embeddings as _emb
    from backend.app import vector_store as _vs
    from backend.app import content_selector as _cs
    vstore = _vs.VectorStore(DATA_DIR / "content_index.db")
    embedder = _emb.make_embedder(DATA_DIR)
    selector = _cs.ContentSelector(
        engine, vstore, embedder, core.liquid, core.dlog,
        project_path=project_path,
        project_content_dir=Path(project_path) / ".aat" / "content")
    threading.Thread(target=selector.index, daemon=True).start()

    app = Gtk.Application(
        application_id=os.environ.get("AAT_APP_ID",
                                      "dev.meta.agentawareterminal"),
                          flags=Gio.ApplicationFlags.FLAGS_NONE)
    S: dict = {"win": None, "overlay_on": False, "panel_on": False,
               "mode": "blinky", "user_closed_task": None,
               "results_shown_task": None, "layout": "overlay",
               "view_mode": (_prefs.get("view_mode")
                             if _prefs.get("view_mode") in (
                                 "term_vis", "feed_vis", "term_feed")
                             else "term_vis"),
               "_view_manual": False,
               "left_override": None, "_agent_busy": False,
               "_busy_since": None, "_busy_end": None,
               "_busy_since_keep": None,
               "_square_mode": "blinky"}

    # ---------------- end-of-session result squares ----------------
    class ResultSquare(Gtk.DrawingArea):
        """A small square: a file grid, or a level fill (score / 4)."""

        GREEN = (0.36, 0.72, 0.48)
        RED = (0.85, 0.42, 0.38)
        NEUTRAL = (0.55, 0.58, 0.62)

        def __init__(self):
            super().__init__()
            self.kind = "files"
            self.files: list = []
            self.frac: float | None = None
            self.set_size_request(86, 86)
            self.connect("draw", self._draw)

        def set_files(self, files):
            self.kind = "files"
            self.files = list(files)[:36]
            self.queue_draw()

        def set_fill(self, frac):
            self.kind = "fill"
            self.frac = frac
            self.queue_draw()

        def _draw(self, _w, cr):
            alloc = self.get_allocation()
            side = float(min(alloc.width, alloc.height))
            if side < 10:
                return False
            ox = (alloc.width - side) / 2
            oy = (alloc.height - side) / 2
            cr.save()
            cr.translate(ox, oy)
            w = h = side
            if self.kind == "files":
                n = max(len(self.files), 1)
                import math
                cols = int(math.ceil(math.sqrt(n)))
                cw = w / cols
                for i in range(cols * cols):
                    x, y = (i % cols) * cw, (i // cols) * cw
                    if i < len(self.files):
                        f = self.files[i]
                        col = self.GREEN if f["add"] >= f["del"] else self.RED
                        if f["add"] == 0 and f["del"] == 0:
                            col = self.NEUTRAL
                        cr.set_source_rgba(*col, 0.9)
                    else:
                        cr.set_source_rgba(*PAL["square"], 0.08)
                    cr.rectangle(x + 1.5, y + 1.5, cw - 3, cw - 3)
                    cr.fill()
            else:
                cr.set_source_rgba(*PAL["square"], 0.10)
                cr.rectangle(0, 0, w, h)
                cr.fill()
                if self.frac is not None:
                    fh = max(0.0, min(1.0, self.frac)) * h
                    cr.set_source_rgba(*PAL["square"], 0.92)
                    cr.rectangle(0, h - fh, w, fh)
                    cr.fill()
                cr.set_source_rgba(*PAL["square"], 0.45)
                cr.set_line_width(1.2)
                cr.rectangle(0.6, 0.6, w - 1.2, h - 1.2)
                cr.stroke()
            cr.restore()
            return False

    # ---------------- animated agent square ----------------
    class AgentSquare(Gtk.DrawingArea):
        """One square, six calm animations (see mode_for_agent)."""

        def __init__(self):
            super().__init__()
            self.mode = "blinky"
            self.prev_mode = None
            self.trans = 1.0
            self._am = 1.0
            self.t = 0.0
            self._timer = None
            self.set_size_request(280, 280)
            self.connect("draw", self._draw)

        def set_mode(self, mode):
            if mode != self.mode:
                self.prev_mode = self.mode
                self.mode = mode
                self.trans = 0.0
                self.queue_draw()

        def start(self):
            if self._timer is None:
                self._timer = GLib.timeout_add(33, self._step)

        def stop(self):
            if self._timer is not None:
                GLib.source_remove(self._timer)
                self._timer = None

        def _step(self):
            self.t += 0.033
            if self.trans < 1.0:
                self.trans = min(1.0, self.trans + 0.033 / 0.45)
            if self.mode == "done" and self.trans >= 1.0:
                return True  # static: no animation in the done state
            self.queue_draw()
            return True

        # -- drawing helpers --
        def _rr(self, cr, x, y, w, h, r):
            import math
            cr.new_path()
            cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
            cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
            cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
            cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
            cr.close_path()

        def _fill(self, cr, x, y, w, h, alpha, r=None):
            self._rr(cr, x, y, w, h, (0.045 * w) if r is None else r)
            cr.set_source_rgba(*PAL["square"], alpha * self._am)
            cr.fill()

        def _draw(self, _w, cr):
            alloc = self.get_allocation()
            if min(alloc.width, alloc.height) < 10:
                return False
            cx, cy = alloc.width / 2, alloc.height / 2
            if self.trans < 1.0 and self.prev_mode:
                p = self.trans
                ease = 1 - (1 - p) ** 2
                cr.save()
                cr.translate(cx, cy)
                cr.scale(1 - 0.04 * ease, 1 - 0.04 * ease)
                cr.translate(-cx, -cy)
                self._am = 1 - ease
                self._paint(cr, self.prev_mode)
                cr.restore()
                cr.save()
                cr.translate(cx, cy)
                cr.scale(0.96 + 0.04 * ease, 0.96 + 0.04 * ease)
                cr.translate(-cx, -cy)
                self._am = ease
                self._paint(cr, self.mode)
                cr.restore()
                self._am = 1.0
            else:
                self._paint(cr, self.mode)
            return False

        def _paint(self, cr, mode):
            import colorsys
            import math
            import random
            alloc = self.get_allocation()
            S = float(min(alloc.width, alloc.height))
            if S < 10:
                return
            m = 0.03 * S
            x = (alloc.width - S) / 2 + m
            y = (alloc.height - S) / 2 + m
            s = S - 2 * m
            t = self.t
            if mode == "done":
                self._fill(cr, x, y, s, s, 0.95)
                cr.set_source_rgba(*PAL["bg_rgb"], 0.95 * self._am)
                cr.set_line_width(0.075 * s)
                cr.set_line_cap(1)  # round
                cr.move_to(x + 0.19 * s, y + 0.53 * s)
                cr.line_to(x + 0.42 * s, y + 0.74 * s)
                cr.line_to(x + 0.83 * s, y + 0.27 * s)
                cr.stroke()
            elif mode == "blinky":
                # Idle: the square levitates — a slow, calm float
                # with a soft shadow breathing underneath.
                bob = math.sin(t * 1.25)
                yoff = 0.016 * s * bob
                sh_w = 0.46 * s * (1 - 0.07 * bob)
                cr.save()
                cr.translate(x + s / 2, y + 0.965 * s)
                cr.scale(1.0, 0.16)
                cr.arc(0, 0, sh_w / 2, 0, 2 * math.pi)
                cr.set_source_rgba(*PAL["square"],
                                   (0.10 + 0.03 * (1 - bob) / 2) * self._am)
                cr.fill()
                cr.restore()
                grow = 1 + 0.008 * math.sin(t * 1.25 + 0.7)
                side = s * 0.94 * grow
                self._fill(cr, x + (s - side) / 2, y + (s - side) / 2 + yoff,
                           side, side, 0.92, r=0.045 * s)
            elif mode == "glitch":
                # Searching the system: a field of small file squares;
                # one after another swells up as it is inspected.
                self._fill(cr, x, y, s, s, 0.08)
                self._rr(cr, x, y, s, s, 0.045 * s)
                cr.set_source_rgba(*PAL["square"], 0.35 * self._am)
                cr.set_line_width(1.5)
                cr.stroke()
                cols, rows = 5, 4
                cell = 0.105 * s
                gapx = (0.78 * s - cols * cell) / (cols - 1)
                gapy = (0.62 * s - rows * cell) / (rows - 1)
                gx = x + (s - 0.78 * s) / 2
                gy = y + (s - 0.62 * s) / 2
                active = int(t / 0.30) % (cols * rows)
                frac = (t % 0.30) / 0.30
                for i in range(cols * rows):
                    cx0 = gx + (i % cols) * (cell + gapx) + cell / 2
                    cy0 = gy + (i // cols) * (cell + gapy) + cell / 2
                    if i == active:
                        zoom = 1 + 0.75 * math.sin(math.pi * frac)
                        sz = cell * zoom
                        self._fill(cr, cx0 - sz / 2, cy0 - sz / 2, sz, sz,
                                   0.95, r=0.02 * s)
                    else:
                        self._fill(cr, cx0 - cell / 2, cy0 - cell / 2,
                                   cell, cell, 0.16, r=0.02 * s)
            elif mode == "trail":
                # Reviewing pages: identical small squares fly through
                # the big square left to right, slowing down and
                # swelling slightly as they cross the center.
                self._fill(cr, x, y, s, s, 0.08)
                self._rr(cr, x, y, s, s, 0.045 * s)
                cr.set_source_rgba(*PAL["square"], 0.35 * self._am)
                cr.set_line_width(1.5)
                cr.stroke()
                n = 5
                for i in range(n):
                    u = ((t / 3.6) + i / n) % 1.0
                    xx = u + (0.55 / (2 * math.pi)) * math.sin(2 * math.pi * u)
                    fade = max(0.0, min(1.0, min(xx, 1.0 - xx) / 0.10))
                    zoom = 1 + 0.5 * math.exp(
                        -((xx - 0.5) ** 2) / (2 * 0.10 ** 2))
                    sz = 0.16 * s * zoom
                    cx = x + xx * s
                    cy = y + s / 2
                    self._fill(cr, cx - sz / 2, cy - sz / 2, sz, sz,
                               0.92 * fade, r=0.03 * s)
            elif mode == "shuffle":
                cell = 0.47 * s
                gap = s - 2 * cell
                base = [(x, y), (x + cell + gap, y),
                        (x, y + cell + gap), (x + cell + gap, y + cell + gap)]
                order = [(0, 1, 2, 3), (1, 0, 3, 2),
                         (1, 3, 0, 2), (3, 2, 1, 0)][int(t / 0.85) % 4]
                frac = min((t % 0.85) / 0.3, 1.0)
                ease = 1 - (1 - frac) ** 3
                prev = [(0, 1, 2, 3), (1, 0, 3, 2),
                        (1, 3, 0, 2), (3, 2, 1, 0)][(int(t / 0.85) - 1) % 4]
                pos = {}
                for tile in range(4):
                    a = base[prev.index(tile)]
                    b = base[order.index(tile)]
                    pos[tile] = (a[0] + (b[0] - a[0]) * ease,
                                 a[1] + (b[1] - a[1]) * ease)
                pulse = 0.75 + 0.2 * math.sin(t * 2.4)
                for tile in range(4):
                    px, py = pos[tile]
                    self._fill(cr, px, py, cell, cell, pulse, r=0.03 * s)
                self._rr(cr, x - 0.02 * s, y - 0.02 * s,
                         s + 0.04 * s, s + 0.04 * s, 0.05 * s)
                cr.set_source_rgba(*PAL["square"], (0.25 + 0.15 * math.sin(t * 2.4)) * self._am)
                cr.set_line_width(1.5)
                cr.stroke()
            elif mode == "flash":
                # Thinking: monochrome. A small square rotates inside
                # the big one.
                self._fill(cr, x, y, s, s, 0.08)
                self._rr(cr, x, y, s, s, 0.045 * s)
                cr.set_source_rgba(*PAL["square"], 0.35 * self._am)
                cr.set_line_width(1.5)
                cr.stroke()
                inner = 0.34 * s
                cr.save()
                cr.translate(x + s / 2, y + s / 2)
                cr.rotate(t * 1.7)
                self._fill(cr, -inner / 2, -inner / 2, inner, inner,
                           0.92, r=0.03 * s)
                cr.restore()
            elif mode == "compile":
                # Building software: squares rain down and stack up
                # inside the big square, Tetris-style.
                self._fill(cr, x, y, s, s, 0.08)
                self._rr(cr, x, y, s, s, 0.045 * s)
                cr.set_source_rgba(*PAL["square"], 0.35 * self._am)
                cr.set_line_width(1.5)
                cr.stroke()
                cols = 5
                cw = 0.148 * s
                gx = x + (s - cols * cw) / 2
                floor = y + 0.93 * s
                pattern = [1, 3, 0, 4, 2, 2, 0, 4, 1, 3, 0, 2, 4]
                heights = [0] * cols
                landed_at = []
                for i, col in enumerate(pattern):
                    start = 0.35 + i * 0.55
                    target = floor - (heights[col] + 1) * cw
                    heights[col] += 1
                    landed_at.append((col, target, start))
                cycle = 0.35 + len(pattern) * 0.55 + 1.4
                tt = t % cycle
                fade = 1.0
                if tt > cycle - 0.8:
                    fade = max(0.0, (cycle - tt) / 0.8)
                for col, target, start in landed_at:
                    if tt < start:
                        continue
                    fp = min((tt - start) / 0.5, 1.0)
                    yy = y + 0.04 * s + (target - y - 0.04 * s) * fp ** 1.6
                    self._fill(cr, gx + col * cw + 1.5, yy + 1.5,
                               cw - 3, cw - 3, 0.92 * fade, r=0.02 * s)
            elif mode == "link":
                # Linking: two squares glide in from the sides and
                # snap into one.
                self._fill(cr, x, y, s, s, 0.08)
                self._rr(cr, x, y, s, s, 0.045 * s)
                cr.set_source_rgba(*PAL["square"], 0.35 * self._am)
                cr.set_line_width(1.5)
                cr.stroke()
                sz = 0.21 * s
                cyc = t % 2.8
                cx, cy = x + s / 2, y + s / 2
                if cyc < 1.0:
                    fp = 1 - (1 - cyc) ** 3
                    off = 0.31 * s - (0.31 - 0.105) * s * fp
                    self._fill(cr, cx - off - sz / 2, cy - sz / 2, sz, sz,
                               0.92, r=0.03 * s)
                    self._fill(cr, cx + off - sz / 2, cy - sz / 2, sz, sz,
                               0.92, r=0.03 * s)
                else:
                    hold = cyc - 1.0
                    bounce = 1 + 0.10 * math.exp(-hold * 5) * \
                        abs(math.sin(hold * 9))
                    msz = sz * bounce
                    alpha = 0.95 if cyc < 2.4 else \
                        max(0.0, 0.95 * (2.8 - cyc) / 0.4)
                    self._fill(cr, cx - msz / 2, cy - msz / 2, msz, msz,
                               alpha, r=0.03 * s)
            elif mode == "copy":
                # Copying: a square appears next to the original.
                self._fill(cr, x, y, s, s, 0.08)
                self._rr(cr, x, y, s, s, 0.045 * s)
                cr.set_source_rgba(*PAL["square"], 0.35 * self._am)
                cr.set_line_width(1.5)
                cr.stroke()
                sz = 0.21 * s
                cy = y + s / 2
                lx = x + s / 2 - 0.145 * s
                rx = x + s / 2 + 0.145 * s
                self._fill(cr, lx - sz / 2, cy - sz / 2, sz, sz, 0.92,
                           r=0.03 * s)
                cyc = t % 2.4
                if cyc < 0.25:
                    a, px = 0.92 * (cyc / 0.25), lx
                elif cyc < 1.15:
                    fp = (cyc - 0.25) / 0.9
                    fp = 1 - (1 - fp) ** 3
                    a, px = 0.55 + 0.37 * fp, lx + (rx - lx) * fp
                elif cyc < 2.0:
                    a, px = 0.92, rx
                else:
                    a, px = 0.92 * max(0.0, (2.4 - cyc) / 0.4), rx
                if cyc >= 0.05:
                    self._fill(cr, px - sz / 2, cy - sz / 2, sz, sz, a,
                               r=0.03 * s)
            elif mode == "install":
                # Installing: packages drop in one by one and settle
                # into a neat row.
                self._fill(cr, x, y, s, s, 0.08)
                self._rr(cr, x, y, s, s, 0.045 * s)
                cr.set_source_rgba(*PAL["square"], 0.35 * self._am)
                cr.set_line_width(1.5)
                cr.stroke()
                slots = 4
                sz = 0.16 * s
                gap = (0.84 * s - slots * sz) / (slots - 1)
                gx = x + (s - 0.84 * s) / 2
                base = y + 0.86 * s
                cyc = t % 4.2
                for i in range(slots):
                    start = 0.3 + i * 0.75
                    if cyc < start:
                        continue
                    fp = min((cyc - start) / 0.55, 1.0)
                    sx = gx + i * (sz + gap)
                    yy = y + 0.08 * s + (base - sz - y - 0.08 * s) * fp ** 1.5
                    squash = 1.0
                    since_land = cyc - start - 0.55
                    if fp >= 1.0 and 0 <= since_land < 0.3:
                        squash = 1 - 0.15 * math.sin(
                            math.pi * since_land / 0.3)
                    fade = 1.0
                    if cyc > 3.7:
                        fade = max(0.0, (4.2 - cyc) / 0.5)
                    hh = sz * squash
                    self._fill(cr, sx, yy + (sz - hh), sz, hh,
                               0.92 * fade, r=0.02 * s)
            elif mode == "question":
                # The agent is asking the user: a big breathing "?".
                self._fill(cr, x, y, s, s, 0.08)
                self._rr(cr, x, y, s, s, 0.045 * s)
                cr.set_source_rgba(*PAL["square"], 0.35 * self._am)
                cr.set_line_width(1.5)
                cr.stroke()
                cr.save()
                cr.translate(x + s / 2, y + s / 2)
                breathe = 1 + 0.045 * math.sin(t * 2.2)
                cr.scale(breathe, breathe)
                cr.select_font_face("Inter", 0, 1)
                cr.set_font_size(0.52 * s)
                ext = cr.text_extents("?")
                cr.set_source_rgba(*PAL["square"], 0.95 * self._am)
                cr.move_to(-ext.x_bearing - ext.width / 2,
                           -ext.y_bearing - ext.height / 2)
                cr.show_text("?")
                cr.restore()
            elif mode == "fail_compile":
                # Build broke: the next piece lands and shatters.
                self._fill(cr, x, y, s, s, 0.08)
                self._rr(cr, x, y, s, s, 0.045 * s)
                cr.set_source_rgba(*PAL["square"], 0.35 * self._am)
                cr.set_line_width(1.5)
                cr.stroke()
                cols = 5
                cw = 0.148 * s
                gx = x + (s - cols * cw) / 2
                floor = y + 0.93 * s
                for col in range(cols):
                    self._fill(cr, gx + col * cw + 1.5, floor - cw + 1.5,
                               cw - 3, cw - 3, 0.85, r=0.02 * s)
                for col in (0, 1, 3, 4):
                    self._fill(cr, gx + col * cw + 1.5, floor - 2 * cw + 1.5,
                               cw - 3, cw - 3, 0.85, r=0.02 * s)
                cyc = t % 2.8
                px = gx + 2 * cw
                py = floor - 3 * cw
                if cyc < 0.55:
                    fp = cyc / 0.55
                    yy = y + 0.04 * s + (py - y - 0.04 * s) * fp ** 1.5
                    self._fill(cr, px + 1.5, yy + 1.5, cw - 3, cw - 3,
                               0.95, r=0.02 * s)
                elif cyc < 1.25:
                    lp = (cyc - 0.55) / 0.7
                    mini = (cw - 3) / 2
                    for k in range(4):
                        dx = (k % 2 - 0.5) * 0.10 * s * lp
                        dy = 0.30 * s * lp ** 2 + (k // 2) * mini
                        self._fill(cr, px + 1.5 + (k % 2) * mini + dx,
                                   py + 1.5 + dy, mini - 1, mini - 1,
                                   0.92 * (1 - lp), r=2)
            elif mode == "fail_link":
                # Linking broke: the squares collide and bounce apart.
                self._fill(cr, x, y, s, s, 0.08)
                self._rr(cr, x, y, s, s, 0.045 * s)
                cr.set_source_rgba(*PAL["square"], 0.35 * self._am)
                cr.set_line_width(1.5)
                cr.stroke()
                sz = 0.21 * s
                cx, cy = x + s / 2, y + s / 2
                cyc = t % 2.2
                if cyc < 0.5:
                    fp = 1 - (1 - cyc / 0.5) ** 2
                    off = 0.30 * s - (0.30 - 0.105) * s * fp
                elif cyc < 1.0:
                    fp = (cyc - 0.5) / 0.5
                    off = 0.105 * s + 0.10 * s * (1 - (1 - fp) ** 3)
                else:
                    off = 0.205 * s + 0.004 * s * math.sin(t * 30)
                self._fill(cr, cx - off - sz / 2, cy - sz / 2, sz, sz,
                           0.92, r=0.03 * s)
                self._fill(cr, cx + off - sz / 2, cy - sz / 2, sz, sz,
                           0.92, r=0.03 * s)
                if 0.5 <= cyc < 0.85:  # impact spark: a small X
                    a = 0.9 * (1 - (cyc - 0.5) / 0.35)
                    r_ = 0.055 * s
                    cr.set_source_rgba(*PAL["square"], a * self._am)
                    cr.set_line_width(max(2.0, 0.014 * s))
                    cr.set_line_cap(1)
                    cr.move_to(cx - r_, cy - r_)
                    cr.line_to(cx + r_, cy + r_)
                    cr.move_to(cx + r_, cy - r_)
                    cr.line_to(cx - r_, cy + r_)
                    cr.stroke()
            elif mode == "fail_test":
                # Tests failed: checks pass, then one square gets an
                # X and shakes; the rest never run.
                self._fill(cr, x, y, s, s, 0.08)
                self._rr(cr, x, y, s, s, 0.045 * s)
                cr.set_source_rgba(*PAL["square"], 0.35 * self._am)
                cr.set_line_width(1.5)
                cr.stroke()
                n = 5
                sz = 0.145 * s
                gap = (0.82 * s - n * sz) / (n - 1)
                gx = x + (s - 0.82 * s) / 2
                cy = y + s / 2
                cyc = t % 3.0
                bad = 3
                for i in range(n):
                    on = cyc > 0.25 + i * 0.35 and i <= bad
                    sx = gx + i * (sz + gap)
                    if i == bad and on:
                        sx += 0.006 * s * math.sin(t * 34)
                    self._fill(cr, sx, cy - sz / 2, sz, sz,
                               0.95 if on else 0.15, r=3)
                    if on:
                        cr.set_source_rgba(*PAL["bg_rgb"], 0.92 * self._am)
                        cr.set_line_width(max(1.8, 0.11 * sz))
                        cr.set_line_cap(1)
                        if i == bad:
                            r_ = 0.26 * sz
                            cr.move_to(sx + sz / 2 - r_, cy - r_)
                            cr.line_to(sx + sz / 2 + r_, cy + r_)
                            cr.move_to(sx + sz / 2 + r_, cy - r_)
                            cr.line_to(sx + sz / 2 - r_, cy + r_)
                        else:
                            cr.move_to(sx + 0.27 * sz, cy + 0.03 * sz)
                            cr.line_to(sx + 0.43 * sz, cy + 0.18 * sz)
                            cr.line_to(sx + 0.74 * sz, cy - 0.18 * sz)
                        cr.stroke()
            elif mode == "fail":
                # Something broke: the square trembles, cracks, and
                # falls apart into shards that tumble down.
                self._fill(cr, x, y, s, s, 0.08)
                self._rr(cr, x, y, s, s, 0.045 * s)
                cr.set_source_rgba(*PAL["square"], 0.35 * self._am)
                cr.set_line_width(1.5)
                cr.stroke()
                inner = 0.40 * s
                ix, iy = x + (s - inner) / 2, y + (s - inner) / 2
                cyc = t % 3.2
                if cyc < 0.5:
                    tremble = 0.004 * s * (cyc / 0.5) * math.sin(t * 46)
                    self._fill(cr, ix + tremble, iy, inner, inner,
                               0.92, r=0.03 * s)
                elif cyc < 1.9:
                    lp = (cyc - 0.5) / 1.4
                    drop = 0.34 * s * lp ** 2
                    half = inner / 2
                    for qi in range(4):
                        qx = ix + (qi % 2) * half
                        qy = iy + (qi // 2) * half
                        ccx, ccy = qx + half / 2, qy + half / 2
                        drift = (qi % 2 - 0.5) * 0.13 * s * lp
                        spin = (1 if qi % 2 else -1) * 0.55 * lp
                        cr.save()
                        cr.translate(ccx + drift, ccy + drop)
                        cr.rotate(spin)
                        self._fill(cr, -half / 2 + 1, -half / 2 + 1,
                                   half - 2, half - 2,
                                   0.92 * max(0.0, 1 - lp), r=2)
                        cr.restore()
                # else: empty broken container, then it loops
            elif mode == "download":
                # Downloading: squares rain in and pile up at the
                # bottom of the big square.
                self._fill(cr, x, y, s, s, 0.08)
                self._rr(cr, x, y, s, s, 0.045 * s)
                cr.set_source_rgba(*PAL["square"], 0.35 * self._am)
                cr.set_line_width(1.5)
                cr.stroke()
                cols = 6
                cw = 0.115 * s
                gx = x + (s - cols * cw) / 2
                floor = y + 0.92 * s
                total = 12
                cyc = t % 3.4
                done_n = int(cyc / 0.24)
                for i in range(min(done_n, total)):
                    col, row = i % cols, i // cols
                    self._fill(cr, gx + col * cw + 1, floor - (row + 1) * cw + 1,
                               cw - 2, cw - 2, 0.85, r=2)
                if done_n < total:
                    col, row = done_n % cols, done_n // cols
                    fp = (cyc % 0.24) / 0.24
                    yy = y + 0.05 * s + (floor - (row + 1) * cw - y - 0.05 * s) * fp ** 1.4
                    self._fill(cr, gx + col * cw + 1, yy + 1, cw - 2, cw - 2,
                               0.95, r=2)
                if cyc > 3.0:
                    pass  # pile complete; loop restarts
            elif mode == "test":
                # Running tests: squares light up one by one as the
                # checks pass, then all hold together.
                self._fill(cr, x, y, s, s, 0.08)
                self._rr(cr, x, y, s, s, 0.045 * s)
                cr.set_source_rgba(*PAL["square"], 0.35 * self._am)
                cr.set_line_width(1.5)
                cr.stroke()
                n = 5
                sz = 0.145 * s
                gap = (0.82 * s - n * sz) / (n - 1)
                gx = x + (s - 0.82 * s) / 2
                cy = y + s / 2
                cyc = t % 2.9
                for i in range(n):
                    on = cyc > 0.25 + i * 0.35
                    pop = 1.0
                    if on and cyc - (0.25 + i * 0.35) < 0.18:
                        pop = 1 + 0.25 * math.sin(
                            math.pi * (cyc - 0.25 - i * 0.35) / 0.18)
                    d = sz * pop
                    sx = gx + i * (sz + gap) + (sz - d) / 2
                    self._fill(cr, sx, cy - d / 2, d, d,
                               0.95 if on else 0.15, r=3)
                    if on:
                        cr.set_source_rgba(*PAL["bg_rgb"], 0.92 * self._am)
                        cr.set_line_width(max(1.6, 0.10 * d))
                        cr.set_line_cap(1)
                        cr.move_to(sx + 0.27 * d, cy + 0.03 * d)
                        cr.line_to(sx + 0.43 * d, cy + 0.18 * d)
                        cr.line_to(sx + 0.74 * d, cy - 0.18 * d)
                        cr.stroke()
            elif mode == "git":
                # Git: history grows — squares conveyor upward, the
                # newest at the bottom, the oldest fading at the top.
                self._fill(cr, x, y, s, s, 0.08)
                self._rr(cr, x, y, s, s, 0.045 * s)
                cr.set_source_rgba(*PAL["square"], 0.35 * self._am)
                cr.set_line_width(1.5)
                cr.stroke()
                sz = 0.17 * s
                cx = x + s / 2
                base = y + 0.88 * s
                step = 0.215 * s
                shift = (t % 0.9) / 0.9
                for i in range(5):
                    yy = base - (i + shift) * step
                    if yy < y + 0.06 * s:
                        continue
                    alpha = 0.95
                    if i + shift > 3.4:
                        alpha = max(0.0, 0.95 * (4.4 - i - shift))
                    self._fill(cr, cx - sz / 2, yy - sz, sz, sz, alpha, r=3)
            elif mode == "extract":
                # Extracting: one archive square unfolds into a 3x3
                # of contents.
                self._fill(cr, x, y, s, s, 0.08)
                self._rr(cr, x, y, s, s, 0.045 * s)
                cr.set_source_rgba(*PAL["square"], 0.35 * self._am)
                cr.set_line_width(1.5)
                cr.stroke()
                inner = 0.34 * s
                ix, iy = x + (s - inner) / 2, y + (s - inner) / 2
                cyc = t % 3.0
                if cyc < 0.55:
                    self._fill(cr, ix, iy, inner, inner, 0.92, r=0.03 * s)
                else:
                    fp = min((cyc - 0.55) / 0.8, 1.0)
                    fp = 1 - (1 - fp) ** 3
                    fade = 1.0 if cyc < 2.5 else max(0.0, (3.0 - cyc) / 0.5)
                    mini = inner / 3
                    spread = 0.16 * s * fp
                    for r_i in range(3):
                        for c_i in range(3):
                            ox = (c_i - 1) * spread
                            oy = (r_i - 1) * spread
                            self._fill(cr, ix + c_i * mini + 1 + ox,
                                       iy + r_i * mini + 1 + oy,
                                       mini - 2, mini - 2,
                                       0.92 * fade, r=2)
            elif mode == "serve":
                # Serving: a steady square broadcasting — outline
                # waves expand and fade.
                self._fill(cr, x, y, s, s, 0.08)
                self._rr(cr, x, y, s, s, 0.045 * s)
                cr.set_source_rgba(*PAL["square"], 0.35 * self._am)
                cr.set_line_width(1.5)
                cr.stroke()
                cx, cy = x + s / 2, y + s / 2
                inner = 0.30 * s * (1 + 0.03 * math.sin(t * 2.0))
                self._fill(cr, cx - inner / 2, cy - inner / 2, inner, inner,
                           0.92, r=0.03 * s)
                for k in range(2):
                    fp = ((t * 0.45) + k * 0.5) % 1.0
                    w = inner + (0.86 * s - inner) * fp
                    cr.set_source_rgba(*PAL["square"],
                                       0.5 * (1 - fp) * self._am)
                    cr.set_line_width(2.0)
                    self._rr(cr, cx - w / 2, cy - w / 2, w, w, 0.05 * s)
                    cr.stroke()
            elif mode == "delete":
                # Deleting: the file square trembles, breaks into a
                # 3x3 of crumbs that rain down and fade away.
                self._fill(cr, x, y, s, s, 0.08)
                self._rr(cr, x, y, s, s, 0.045 * s)
                cr.set_source_rgba(*PAL["square"], 0.35 * self._am)
                cr.set_line_width(1.5)
                cr.stroke()
                inner = 0.34 * s
                ix, iy = x + (s - inner) / 2, y + (s - inner) / 2
                cyc = t % 2.6
                if cyc < 0.7:
                    shake = 0.006 * s * math.sin(t * 42) * (cyc / 0.7)
                    self._fill(cr, ix + shake, iy, inner, inner, 0.92,
                               r=0.03 * s)
                elif cyc < 2.0:
                    fp = (cyc - 0.7) / 1.3
                    mini = inner / 3
                    for r_i in range(3):
                        for c_i in range(3):
                            delay = 0.08 * ((r_i * 3 + c_i) % 4)
                            lp = max(0.0, min((fp - delay) / (1 - delay), 1.0))
                            fall = 0.55 * s * lp ** 2
                            drift = (c_i - 1) * 0.03 * s * lp
                            self._fill(
                                cr, ix + c_i * mini + 1 + drift,
                                iy + r_i * mini + 1 + fall,
                                mini - 2, mini - 2,
                                0.92 * (1 - lp) * (1 if fp > delay else 0),
                                r=2)
                # else: gone — a beat of emptiness, then the next file
            else:  # build — writing code/files
                # A small square is written line by line, becomes
                # solid, then files itself away to the left; repeat.
                self._fill(cr, x, y, s, s, 0.08)
                self._rr(cr, x, y, s, s, 0.045 * s)
                cr.set_source_rgba(*PAL["square"], 0.35 * self._am)
                cr.set_line_width(1.5)
                cr.stroke()
                inner = 0.34 * s
                ix, iy = x + (s - inner) / 2, y + (s - inner) / 2
                cyc = t % 3.0
                # the previously finished file rests at the left
                if cyc >= 1.85:
                    self._fill(cr, x + 0.10 * s, iy, inner, inner, 0.22,
                               r=0.03 * s)
                if cyc < 1.0:
                    # lines appear one by one
                    lines = 4
                    lh = inner / (lines * 1.9)
                    for i in range(lines):
                        ls = i * 0.25
                        if cyc < ls:
                            continue
                        wfrac = min((cyc - ls) / 0.22, 1.0)
                        self._fill(cr, ix + 0.12 * inner,
                                   iy + (0.16 + i * 0.2) * inner,
                                   0.76 * inner * wfrac, lh, 0.92, r=2)
                elif cyc < 1.45:
                    # lines condense into a solid square
                    fp = (cyc - 1.0) / 0.45
                    self._fill(cr, ix, iy, inner, inner, 0.25 + 0.7 * fp,
                               r=0.03 * s)
                elif cyc < 1.85:
                    # solid, then slides away to the left
                    fp = (cyc - 1.45) / 0.4
                    fp = 1 - (1 - fp) ** 2
                    nx = ix - (ix - (x + 0.10 * s)) * fp
                    self._fill(cr, nx, iy, inner, inner, 0.95 - 0.73 * fp,
                               r=0.03 * s)
                else:
                    # filed away; a fresh page is about to start
                    self._fill(cr, ix, iy, inner, inner, 0.10, r=0.03 * s)

    # ---------------- window & terminal ----------------
    def build():
        win = Gtk.ApplicationWindow(application=app,
                                    title="Agent-Aware Terminal")
        win.set_default_size(1280, 800)
        S["win"] = win

        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        # No title/subtitle in the header: the project name and the
        # agent status pill were noise (the square already says it).
        win.set_titlebar(header)
        S["header"] = header

        # The floating square: toggles the visualization mode. It is
        # a miniature of the big square — hollow when the mode is
        # off, filled by agent activity when on, and it breathes
        # when the agent needs the user.
        S["mode_btn"] = Gtk.DrawingArea()
        S["mode_btn"].set_size_request(30, 30)
        S["mode_btn"].connect("draw", draw_mode_btn)
        mode_ev = Gtk.EventBox()
        mode_ev.add(S["mode_btn"])
        mode_ev.connect("button-press-event",
                        lambda *_: set_overlay(not S["overlay_on"]))
        header.pack_end(mode_ev)

        # Theme cycle: Auto (system) -> Light -> Dark -> Auto, drawn
        # as a half-disc / sun / moon. Borderless icon button.
        S["theme_btn"] = Gtk.Button()
        S["theme_btn"].get_style_context().add_class("icon-btn")
        S["theme_btn"].set_relief(Gtk.ReliefStyle.NONE)
        theme_glyph = Gtk.DrawingArea()
        theme_glyph.set_size_request(18, 18)
        theme_glyph.connect("draw", draw_theme_btn)
        S["theme_btn"].add(theme_glyph)
        S["theme_glyph"] = theme_glyph
        S["theme_btn"].connect("clicked", lambda *_: cycle_theme())
        header.pack_end(S["theme_btn"])
        apply_theme()  # sets the tooltip for the current mode

        menu = Gio.Menu()
        menu.append("Open project…", "app.open-project")
        menu.append("Run Muse with a task", "app.muse-task")
        menu.append("Visualization mode", "app.split")
        menu.append("History & diagnostics", "app.panel")
        menu.append("Simulate events (dev)", "app.simulate")
        menu.append("Quit", "app.quit")
        menubtn = Gtk.MenuButton()
        menubtn.get_style_context().add_class("icon-btn")
        menubtn.set_relief(Gtk.ReliefStyle.NONE)
        menubtn.set_image(Gtk.Image.new_from_icon_name(
            "open-menu-symbolic", Gtk.IconSize.BUTTON))
        menubtn.set_menu_model(menu)
        header.pack_end(menubtn)

        # Belt and suspenders: set the IM module on GTK settings too,
        # before any widget (VTE creates its IM context at construction).
        if IBUS_OK and not os.environ.get("AAT_NO_IBUS"):
            _gs = Gtk.Settings.get_default()
            if _gs:
                _gs.set_property("gtk-im-module", "ibus")

        # VTE — the terminal itself, exactly like a normal terminal app.
        vte = Vte.Terminal()
        vte.set_scrollback_lines(6000)
        from gi.repository import Pango
        vte.set_font(Pango.FontDescription("Monospace 11"))
        vte.set_color_background(Gdk.RGBA(*PAL["vte_bg"], 1))
        vte.set_color_foreground(Gdk.RGBA(*PAL["vte_fg"], 1))
        scrolled = Gtk.ScrolledWindow()
        scrolled.add(vte)
        S["vte"] = vte
        pty_info = VtePtyInfo(vte)

        overlay = Gtk.Overlay()
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        # Left pane: the terminal, or — while the agent works — the
        # companion content space that earns the user's attention.
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                          spacing=10)
        content.get_style_context().add_class("scene")
        content.set_hexpand(True)
        content.set_vexpand(True)
        S["game_ctl"] = {"game": None, "answered": False,
                         "chosen": None, "rects": {}, "radii": [],
                         "center": (0.0, 0.0), "btns": {}}
        game_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                           spacing=8)
        game_box.set_margin_top(28)
        game_box.set_margin_bottom(22)
        game_box.set_margin_start(34)
        game_box.set_margin_end(34)
        top_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        game_box.pack_start(top_row, False, False, 0)
        S["game_title"] = Gtk.Label(label="")
        S["game_title"].get_style_context().add_class("content-soon")
        S["game_title"].set_xalign(0)
        S["game_title"].set_line_wrap(True)
        game_box.pack_start(S["game_title"], False, False, 2)
        S["game_hint"] = Gtk.Label(label="")
        S["game_hint"].get_style_context().add_class("content-sub")
        S["game_hint"].set_xalign(0)
        game_box.pack_start(S["game_hint"], False, False, 6)
        S["game_scene"] = Gtk.DrawingArea()
        S["game_scene"].set_size_request(-1, 300)
        S["game_scene"].set_hexpand(True)
        S["game_scene"].set_vexpand(True)
        S["game_scene"].connect("draw", draw_game_scene)
        scene_ev = Gtk.EventBox()
        S["scene_ev"] = scene_ev
        scene_ev.add(S["game_scene"])
        scene_ev.connect("button-press-event", on_game_scene_click)
        game_box.pack_start(scene_ev, True, True, 4)
        S["game_body"] = Gtk.Label(label="")
        S["game_body"].get_style_context().add_class("content-body")
        S["game_body"].set_xalign(0)
        S["game_body"].set_line_wrap(True)
        game_box.pack_start(S["game_body"], False, False, 2)
        S["game_q"] = Gtk.Label(label="")
        S["game_q"].get_style_context().add_class("content-q")
        S["game_q"].set_xalign(0)
        S["game_q"].set_line_wrap(True)
        game_box.pack_start(S["game_q"], False, False, 6)
        S["game_choices"] = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8)
        game_box.pack_start(S["game_choices"], False, False, 2)
        fb_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                         spacing=12)
        S["game_fb"] = Gtk.Label(label="")
        S["game_fb"].get_style_context().add_class("content-body")
        S["game_fb"].set_xalign(0)
        S["game_fb"].set_line_wrap(True)
        fb_row.pack_start(S["game_fb"], True, True, 0)
        S["game_next"] = Gtk.Button(label="Next")
        S["game_next"].get_style_context().add_class("game-next")
        S["game_next"].connect("clicked", lambda *_: serve_game(True))
        S["btn_open"] = Gtk.Button(label="Open link \u2197")
        S["btn_open"].get_style_context().add_class("game-next")
        S["btn_open"].connect("clicked", lambda *_: game_open_link())
        S["btn_save"] = Gtk.Button(label="Save idea")
        S["btn_save"].get_style_context().add_class("game-next")
        S["btn_save"].connect("clicked", lambda *_: game_save_idea())
        S["btn_like"] = Gtk.Button(label="\u2665")
        S["btn_like"].set_tooltip_text("More like this")
        S["btn_like"].get_style_context().add_class("icon-btn")
        S["btn_like"].connect(
            "clicked", lambda *_: game_react("like"))
        S["btn_dislike"] = Gtk.Button(label="\u2715")
        S["btn_dislike"].set_tooltip_text("Less like this")
        S["btn_dislike"].get_style_context().add_class("icon-btn")
        S["btn_dislike"].connect(
            "clicked", lambda *_: game_react("dislike"))
        fb_row.pack_end(S["game_next"], False, False, 0)
        fb_row.pack_end(S["btn_open"], False, False, 0)
        fb_row.pack_end(S["btn_save"], False, False, 0)
        fb_row.pack_end(S["btn_like"], False, False, 0)
        fb_row.pack_end(S["btn_dislike"], False, False, 0)
        game_box.pack_start(fb_row, False, False, 8)
        S["btn_idea"] = Gtk.Button()
        S["btn_idea"].set_tooltip_text(
            "Pick what content to generate next")
        S["btn_idea"].get_style_context().add_class("icon-btn")
        bulb = Gtk.DrawingArea()
        bulb.set_size_request(16, 16)
        bulb.connect("draw", draw_bulb)
        S["btn_idea"].add(bulb)
        S["btn_idea"].connect("clicked",
                              lambda *_: idea_pick_type())
        top_row.pack_end(S["btn_idea"], False, False, 0)
        S["game_box"] = game_box
        S["content_box"] = content
        S["content_top_row"] = top_row
        game_scroll = Gtk.ScrolledWindow()
        game_scroll.set_policy(Gtk.PolicyType.NEVER,
                               Gtk.PolicyType.AUTOMATIC)
        game_scroll.add(game_box)
        S["game_scroll"] = game_scroll
        content.pack_start(game_scroll, True, True, 0)
        empty_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                            spacing=10)
        empty_box.set_halign(Gtk.Align.CENTER)
        empty_box.set_valign(Gtk.Align.CENTER)
        soon = Gtk.Label(label="Soon content will appear")
        soon.get_style_context().add_class("content-soon")
        soon_sub = Gtk.Label(
            label="While the agent works, this space is yours.")
        soon_sub.get_style_context().add_class("content-sub")
        empty_box.pack_start(soon, False, False, 0)
        empty_box.pack_start(soon_sub, False, False, 0)
        S["game_empty"] = empty_box
        content.pack_start(empty_box, True, False, 0)
        S["left_stack"] = Gtk.Stack()
        S["left_stack"].set_transition_type(
            Gtk.StackTransitionType.CROSSFADE)
        S["left_stack"].set_transition_duration(180)
        S["left_stack"].add_named(scrolled, "terminal")
        S["left_stack"].add_named(content, "content")
        paned.pack1(S["left_stack"], True, True)
        overlay.add(paned)
        win.add(overlay)
        S["root_overlay"] = overlay
        S["paned"] = paned

        # ---- minimal overlay: system-theme surface + agent square ----
        scene = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        scene.get_style_context().add_class("scene")
        scene.set_hexpand(True)
        scene.set_vexpand(True)
        atop = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        atop.set_margin_top(14)
        atop.set_margin_start(20)
        atop.set_margin_end(20)
        S["scene_top_row"] = atop
        btn_more = Gtk.Button(label="···")
        btn_more.get_style_context().add_class("quiet")
        btn_more.connect("clicked", lambda *_: toggle_panel(True))
        atop.pack_end(btn_more, False, False, 8)
        # View cycle (replaces the old Feed button): a borderless
        # icon showing the current two-pane pairing. Clicks cycle
        # Terminal+Visualisation -> Feed+Visualisation ->
        # Terminal+Feed -> back. In the Terminal+Feed pairing the
        # button moves to the top of the Feed pane (apply_view), so
        # it is always on the right pane and always reachable.
        S["view_btn"] = Gtk.Button()
        S["view_btn"].get_style_context().add_class("icon-btn")
        S["view_btn"].set_relief(Gtk.ReliefStyle.NONE)
        view_glyph = Gtk.DrawingArea()
        view_glyph.set_size_request(28, 20)
        view_glyph.connect("draw", draw_view_btn)
        S["view_btn"].add(view_glyph)
        S["view_glyph"] = view_glyph
        S["view_btn"].connect("clicked", lambda *_: cycle_view())
        atop.pack_end(S["view_btn"], False, False, 0)
        scene.pack_start(atop, False, False, 0)

        S["sys1_banner"] = Gtk.Label(label="")
        S["sys1_banner"].get_style_context().add_class("sys1-banner")
        S["sys1_banner"].set_halign(Gtk.Align.CENTER)
        S["sys1_banner"].set_margin_top(10)
        scene.pack_start(S["sys1_banner"], False, False, 0)
        S["sys1_banner"].hide()

        center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=26)
        center.set_halign(Gtk.Align.CENTER)
        center.set_valign(Gtk.Align.CENTER)
        S["square"] = AgentSquare()
        evbox = Gtk.EventBox()
        evbox.add(S["square"])
        evbox.connect("button-press-event", lambda *_: toggle_left())
        center.pack_start(evbox, False, False, 0)
        captions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        S["mode_label"] = Gtk.Label(label="BLINKY")
        S["mode_label"].get_style_context().add_class("mode-label")
        S["mode_title"] = Gtk.Label(label="Idle")
        S["mode_title"].get_style_context().add_class("mode-title")
        S["mode_sub"] = Gtk.Label(label="")
        S["mode_sub"].get_style_context().add_class("mode-sub")
        S["mode_sub"].set_ellipsize(3)
        for lab in (S["mode_label"], S["mode_title"], S["mode_sub"]):
            lab.set_halign(Gtk.Align.CENTER)
            captions.pack_start(lab, False, False, 0)
        center.pack_start(captions, False, False, 0)

        res_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=22)
        res_row.set_halign(Gtk.Align.CENTER)
        S["res_squares"] = {}
        S["res_labels"] = {}
        for key, caption in (("files", "FILES"), ("completion", "DONE"),
                             ("safety", "SAFE")):
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
            sq = ResultSquare()
            sq.set_hexpand(False)
            sq.set_vexpand(False)
            sq.set_halign(Gtk.Align.CENTER)
            col.pack_start(sq, False, False, 0)
            lab = Gtk.Label(label=caption)
            lab.get_style_context().add_class("res-label")
            lab.set_halign(Gtk.Align.CENTER)
            col.pack_start(lab, False, False, 0)
            res_row.pack_start(col, False, False, 0)
            S["res_squares"][key] = sq
            S["res_labels"][key] = lab
        S["res_row"] = res_row
        res_row.set_visible(False)
        center.pack_start(res_row, False, False, 0)
        scene.pack_start(center, True, True, 0)

        S["hint"] = Gtk.Label(label="")
        S["hint"].get_style_context().add_class("hint")
        S["hint"].set_halign(Gtk.Align.CENTER)
        S["hint"].set_margin_bottom(18)
        scene.pack_start(S["hint"], False, False, 0)

        def _fit_square(_w, alloc):
            factor = 0.58 if S.get("overlay_on") else 0.30
            side = int(min(factor * alloc.width, 0.58 * alloc.height))
            if side > 40 and S["square"].get_size_request().width != side:
                S["square"].set_size_request(side, side)
        scene.connect("size-allocate", _fit_square)
        S["scene"] = scene
        scene.set_visible(False)
        overlay.add_overlay(scene)

        # ---- side panel: history + diagnostics ----
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        panel.get_style_context().add_class("panel")
        panel.set_size_request(360, -1)
        nb = Gtk.Notebook()
        hist_scroll = Gtk.ScrolledWindow()
        S["hist_text"] = Gtk.TextView()
        S["hist_text"].set_editable(False)
        S["hist_text"].set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        S["hist_text"].set_monospace(True)
        hist_scroll.add(S["hist_text"])
        nb.append_page(hist_scroll, Gtk.Label(label="History"))
        diag_scroll = Gtk.ScrolledWindow()
        S["diag_text"] = Gtk.TextView()
        S["diag_text"].set_editable(False)
        S["diag_text"].set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        S["diag_text"].set_monospace(True)
        diag_scroll.add(S["diag_text"])
        nb.append_page(diag_scroll, Gtk.Label(label="Diagnostics"))
        settings_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        settings_box.set_margin_top(14)
        settings_box.set_margin_start(12)
        settings_box.set_margin_end(12)
        S["set_autoshow"] = Gtk.CheckButton(label="Auto-show overlay while the agent works")
        S["set_autoshow"].set_active(True)
        settings_box.pack_start(S["set_autoshow"], False, False, 0)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.pack_start(Gtk.Label(label="Ask System 1 after, sec"), False, False, 0)
        S["set_threshold"] = Gtk.SpinButton.new_with_range(2, 120, 1)
        S["set_threshold"].set_value(5)
        row.pack_start(S["set_threshold"], False, False, 0)
        settings_box.pack_start(row, False, False, 0)
        nb.append_page(settings_box, Gtk.Label(label="Settings"))
        panel.pack_start(nb, True, True, 0)
        S["panel"] = panel
        panel.set_halign(Gtk.Align.END)
        panel.set_visible(False)
        overlay.add_overlay(panel)

        win.connect("key-press-event", on_key)
        win.show_all()
        scene.set_visible(False)
        panel.set_visible(False)

        # spawn the shell inside VTE
        def on_spawn(term, pid, error, _data=None):
            if pid:
                pty_info.pid = pid
                from backend.app.session_observer import SessionObserver
                obs = SessionObserver(pty_info, core.on_snapshot)
                obs.start()
                S["observer"] = obs
        vte.spawn_async(
            Vte.PtyFlags.DEFAULT, core.project_path,
            [os.environ.get("SHELL", "/bin/bash"), "-i"],
            None, GLib.SpawnFlags.DEFAULT, None, None, -1, None,
            on_spawn, None)
        vte.grab_focus()
        serve_game(False)

        # Game tour (review aid): AAT_TOUR=<seconds> opens the
        # visualization with the content pane up and auto-serves
        # the next game on an interval — the whole deck, one by
        # one, at real size with live animation. Pauses while
        # the user looks at the terminal.
        if _tour_raw:
            _tour_manual = _tour_raw in ("manual", "0", "play")
            _tour_secs = None
            if not _tour_manual:
                try:
                    _tour_secs = max(2, int(_tour_raw))
                except ValueError:
                    _tour_secs = 6
            set_overlay(True)
            set_view("feed_vis")
            if _tour_secs:
                def _tour_tick():
                    if _content_visible():
                        cur = S["game_ctl"].get("game") or {}
                        # Facts, news and ideas are reading
                        # content: they change only when the
                        # user presses Next, never on the clock.
                        if cur.get("type", "game") == "game":
                            serve_game(True)
                    return True
                GLib.timeout_add_seconds(_tour_secs, _tour_tick)
            core.dlog("ui", "game tour on ("
                      + (f"{_tour_secs}s per game" if _tour_secs
                         else "manual Next")
                      + "; statistics sandboxed)")

        def _anim_frame(_widget, _clock, _data=None):
            # Frame-clock redraw: synced to the display refresh
            # (60 Hz) instead of a coarse 50 ms timer, so game
            # motion is sampled smoothly. Cheap guard first.
            ctl = S["game_ctl"]
            game = ctl.get("game")
            verdict_live = ctl.get("verdict") is not None and \
                time.time() - ctl.get("verdict_t0", 0) < 1.2
            if S["overlay_on"] and game \
                    and ((game.get("scene") or {}).get("anim")
                         or game.get("_paint") or verdict_live) \
                    and _content_visible():
                S["game_scene"].queue_draw()
            return True
        S["game_scene"].add_tick_callback(_anim_frame, None)
        GLib.timeout_add(280, tick)
        if os.environ.get("AAT_OPEN_OVERLAY"):
            GLib.timeout_add(1300, lambda: (set_overlay(True), False)[1])
        if os.environ.get("AAT_SPLIT"):
            GLib.timeout_add(900, lambda: (set_overlay(True), False)[1])

    # ---------------- actions ----------------
    def draw_mode_btn(area, cr):
        import math
        w, h = area.get_allocated_width(), area.get_allocated_height()
        s = max(min(w, h) - 8, 10)
        x, y = (w - s) / 2, (h - s) / 2
        mode = S.get("_square_mode", "blinky")

        def rr(px, py, ps, r):
            cr.new_path()
            cr.arc(px + ps - r, py + r, r, -1.5708, 0)
            cr.arc(px + ps - r, py + ps - r, r, 0, 1.5708)
            cr.arc(px + r, py + ps - r, r, 1.5708, 3.1416)
            cr.arc(px + r, py + r, r, 3.1416, 4.7124)
            cr.close_path()

        if mode in ("shuffle", "question"):
            pulse = 0.35 + 0.30 * abs(math.sin(time.time() * 2.6))
            rr(x - 3, y - 3, s + 6, 5)
            cr.set_source_rgba(*PALC["text"], pulse)
            cr.set_line_width(1.2)
            cr.stroke()
        if not S["overlay_on"]:
            rr(x, y, s, 3)
            cr.set_source_rgba(*PAL["square"], 0.95)
            cr.set_line_width(1.7)
            cr.stroke()
            return False
        if mode == "blinky":
            rr(x, y, s, 3)
            cr.set_source_rgba(*PAL["square"], 0.25)
            cr.fill_preserve()
            cr.set_source_rgba(*PAL["square"], 0.55)
            cr.set_line_width(1.2)
            cr.stroke()
        elif mode == "done":
            rr(x, y, s, 3)
            cr.set_source_rgba(*PAL["square"], 0.92)
            cr.fill()
            cr.set_source_rgba(*PALC["bg"], 1)
            cr.set_line_width(2.0)
            cr.set_line_cap(1)
            cr.move_to(x + 0.28 * s, y + 0.52 * s)
            cr.line_to(x + 0.44 * s, y + 0.68 * s)
            cr.line_to(x + 0.74 * s, y + 0.34 * s)
            cr.stroke()
        elif mode.startswith("fail"):
            rr(x, y, s, 3)
            cr.set_source_rgba(*PAL["square"], 0.85)
            cr.fill()
            cr.set_source_rgba(*PALC["bg"], 1)
            cr.set_line_width(1.8)
            cr.move_to(x + 0.32 * s, y + 0.32 * s)
            cr.line_to(x + 0.68 * s, y + 0.68 * s)
            cr.move_to(x + 0.68 * s, y + 0.32 * s)
            cr.line_to(x + 0.32 * s, y + 0.68 * s)
            cr.stroke()
        elif mode == "question":
            rr(x, y, s, 3)
            cr.set_source_rgba(*PAL["square"], 0.92)
            cr.fill()
            cr.set_source_rgba(*PALC["bg"], 1)
            cr.select_font_face("Inter", 0, 1)
            cr.set_font_size(0.62 * s)
            ext = cr.text_extents("?")
            cr.move_to(x + s / 2 - (ext.width / 2 + ext.x_bearing),
                       y + s / 2 - (ext.height / 2 + ext.y_bearing))
            cr.show_text("?")
        elif mode == "shuffle":
            for dx, dy in ((0.06, 0.06), (0.52, 0.52)):
                rr(x + dx * s, y + dy * s, 0.42 * s, 2)
                cr.set_source_rgba(*PAL["square"], 0.92)
                cr.fill()
        else:
            rr(x, y, s, 3)
            cr.set_source_rgba(*PAL["square"], 0.92)
            cr.fill()
        return False

    # ---------------- theme & view cycle buttons ----------------
    THEME_CYCLE = ("auto", "light", "dark")
    VIEW_CYCLE = ("term_vis", "feed_vis", "term_feed")
    VIEW_LABELS = {
        "term_vis": "Terminal + visualisation",
        "feed_vis": "Feed + visualisation",
        "term_feed": "Terminal + Feed",
    }

    def cycle_theme():
        nxt = THEME_CYCLE[(THEME_CYCLE.index(THEME["mode"]) + 1)
                          % len(THEME_CYCLE)]
        apply_theme(nxt)

    def draw_theme_btn(area, cr):
        import math
        w, h = area.get_allocated_width(), area.get_allocated_height()
        cx, cy = w / 2, h / 2
        r = max(min(w, h) / 2 - 2.5, 4)
        cr.set_source_rgba(*PALC["text"], 0.95)
        cr.set_line_width(1.4)
        if THEME["mode"] == "light":  # sun: disc + rays
            cr.arc(cx, cy, r * 0.52, 0, 2 * math.pi)
            cr.stroke()
            for i in range(8):
                a = i * math.pi / 4
                cr.move_to(cx + math.cos(a) * r * 0.72,
                           cy + math.sin(a) * r * 0.72)
                cr.line_to(cx + math.cos(a) * r * 0.98,
                           cy + math.sin(a) * r * 0.98)
                cr.stroke()
        elif THEME["mode"] == "dark":  # moon: disc minus offset disc
            cr.arc(cx, cy, r * 0.82, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgba(*PALC["bg"], 1)
            cr.arc(cx + r * 0.42, cy - r * 0.28, r * 0.68, 0, 2 * math.pi)
            cr.fill()
        else:  # auto: half-filled disc (system decides the halves)
            cr.arc(cx, cy, r * 0.78, -math.pi / 2, math.pi / 2)
            cr.close_path()
            cr.fill()
            cr.arc(cx, cy, r * 0.78, 0, 2 * math.pi)
            cr.stroke()
        return False

    def draw_view_btn(area, cr):
        import math
        w, h = area.get_allocated_width(), area.get_allocated_height()
        bw, bh = max(w - 6, 12), max(h - 4, 10)
        x, y = (w - bw) / 2, (h - bh) / 2
        mid = x + bw / 2
        kinds = {"term_vis": ("term", "vis"),
                 "feed_vis": ("feed", "vis"),
                 "term_feed": ("term", "feed")}[S["view_mode"]]

        def rr(px, py, pw, ph, rad):
            cr.new_path()
            cr.arc(px + pw - rad, py + rad, rad, -1.5708, 0)
            cr.arc(px + pw - rad, py + ph - rad, rad, 0, 1.5708)
            cr.arc(px + rad, py + ph - rad, rad, 1.5708, 3.1416)
            cr.arc(px + rad, py + rad, rad, 3.1416, 4.7124)
            cr.close_path()

        def glyph(kind, cx, cy):
            cr.set_source_rgba(*PALC["text"], 0.95)
            if kind == "vis":  # the agent square
                s = min(bw, bh) * 0.30
                rr(cx - s / 2, cy - s / 2, s, s, 1.5)
                cr.fill()
            elif kind == "feed":  # content lines
                cr.set_line_width(1.3)
                for i, frac in enumerate((0.95, 0.75, 0.55)):
                    ly = cy - 3.4 + i * 3.4
                    cr.move_to(cx - bw * 0.17, ly)
                    cr.line_to(cx - bw * 0.17 + bw * 0.34 * frac, ly)
                    cr.stroke()
            else:  # terminal prompt: >_
                cr.set_line_width(1.3)
                cr.move_to(cx - bw * 0.16, cy - 3.2)
                cr.line_to(cx - bw * 0.05, cy)
                cr.line_to(cx - bw * 0.16, cy + 3.2)
                cr.stroke()
                cr.move_to(cx + bw * 0.01, cy + 3.4)
                cr.line_to(cx + bw * 0.17, cy + 3.4)
                cr.stroke()

        rr(x, y, bw, bh, 3)
        cr.set_source_rgba(*PALC["text"], 0.75)
        cr.set_line_width(1.1)
        cr.stroke()
        cr.move_to(mid, y + 2)
        cr.line_to(mid, y + bh - 2)
        cr.stroke()
        glyph(kinds[0], x + bw / 4, y + bh / 2)
        glyph(kinds[1], x + 3 * bw / 4, y + bh / 2)
        return False

    # ---------------- companion content (games) ----------------
    def _content_context():
        task = (core.agent_state or {}).get("task") or ""
        cwd = (core.snapshot or {}).get("cwd") or ""
        root = project_path
        if cwd and cwd != project_path and Path(cwd).is_dir():
            # The terminal may have wandered into another
            # project (~/dev/yue2 while the app was launched
            # elsewhere): context must follow the user.
            probe = Path(cwd)
            for _ in range(4):
                if (probe / ".git").exists():
                    break
                if probe.parent == probe:
                    break
                probe = probe.parent
            root = str(probe)
        key = (root, task)
        if S.get("_ctx_key") != key:
            domains = ""
            try:
                from backend.app import content_researcher as _cr2
                prof = _cr2.profile_project(root, task)
                domains = ", ".join(
                    f"{d['label']} ({', '.join(d['evidence'][:4])})"
                    for d in prof.get("domains", [])[:2])
            except Exception:
                pass
            name = (core.project or {}).get("name") \
                if root == project_path else Path(root).name
            S["_ctx_base"] = (f"Project: {name}. Domain: "
                              f"{domains or 'general software'}. "
                              f"Current task: {task or 'coding'}")
            S["_ctx_key"] = key
        tail = getattr(core, "_screen_tail", "") or ""
        lines = [ln.strip() for ln in tail.splitlines()
                 if ln.strip()]
        excerpt = " ".join(lines[-4:])[:400]
        return S["_ctx_base"] + (
            f" On screen: {excerpt}" if excerpt else "")

    def _close_previous_serve():
        ctl = S["game_ctl"]
        prev = ctl.get("serving_id")
        if prev and not _tour_raw:
            dwell = time.time() - ctl.get("serving_at", time.time())
            vstore.record_serve(prev, project_path, dwell_s=dwell)
        ctl["serving_id"] = None

    def game_react(kind):
        game = S["game_ctl"].get("game")
        if not game:
            return
        if not _tour_raw:
            vstore.add_reaction(game["id"], project_path, kind)
        for name, btn in (("like", S["btn_like"]),
                          ("dislike", S["btn_dislike"])):
            ctx = btn.get_style_context()
            if name == kind:
                ctx.add_class("react-on")
            else:
                ctx.remove_class("react-on")
        core.dlog("content", f"reaction {kind} on {game['id']}")

    def _kick_pick():
        """Prefetch the next contextual pick off the GTK thread.
        pick() embeds with the local model and asks System-1 —
        seconds of work that must never block the UI (the pane
        froze on open when this ran synchronously)."""
        if S.get("_pick_busy") or not selector.indexed:
            return
        S["_pick_busy"] = True

        def _work():
            try:
                ctx = _content_context()
                exclude = set(engine._served)
                pending = S.get("_pending_pick")
                if pending:
                    exclude.add(pending[0])
                picked = selector.pick(ctx, project_path, exclude)
                if picked:
                    S["_pending_pick"] = picked
                    core.dlog("content",
                              f"pick prefetched: {picked[0]} "
                              f"[{picked[1]}]")
                    if picked[1] == "global":
                        selector.evaluate_generation(
                            ctx, project_path, 0.0, picked[2])
            except Exception:
                pass
            finally:
                S["_pick_busy"] = False

        threading.Thread(target=_work, daemon=True).start()

    def serve_game(advance=True):
        ctl = S["game_ctl"]
        game = None
        if advance:
            _close_previous_serve()
            pending = S.pop("_pending_pick", None)
            if pending:
                pid, source, sim = pending
                pack = engine.pack_by_id(pid)
                if pack is not None and pid not in engine._served:
                    game = engine.serve_specific(pack)
                    core.dlog("content",
                              f"selector -> {pid} [{source}, "
                              f"sim {sim:.2f}]")
            if game is None:
                game = engine.next_game()
            _kick_pick()
            if game is not None and not _tour_raw:
                ctl["serving_id"] = game["id"]
                ctl["serving_at"] = time.time()
                vstore.record_serve(game["id"], project_path)
        else:
            game = engine.current()
        ctl.update(game=game, answered=False, chosen=None, btns={},
                   t0=time.time(), verdict=None, verdict_t0=0.0)
        for btn in (S["btn_like"], S["btn_dislike"]):
            btn.get_style_context().remove_class("react-on")
        if game is None:
            S["game_scroll"].hide()
            S["game_empty"].show_all()
            return
        S["game_empty"].hide()
        S["game_scroll"].show_all()
        ptype = game.get("type", "game")
        S["game_title"].set_text(game.get("title", ""))
        S["game_hint"].set_text(game.get("hint", ""))
        S["game_body"].set_text(game.get("body", ""))
        S["game_q"].set_text(game.get("question", ""))
        for child in list(S["game_choices"].get_children()):
            S["game_choices"].remove(child)
        for ch in (game.get("choices") or []) \
                if ptype == "game" else []:
            btn = Gtk.Button(label=ch.get("label", ""))
            btn.get_style_context().add_class("choice")
            lbl = btn.get_child()
            if lbl is not None:
                lbl.set_xalign(0.0)
                lbl.set_margin_start(4)
            btn.connect("clicked",
                        lambda _b, cid=ch["id"]: game_answer(cid))
            ctl["btns"][ch["id"]] = btn
            S["game_choices"].pack_start(btn, False, False, 0)
            btn.show()
        S["game_fb"].set_text("")
        _fb_ctx = S["game_fb"].get_style_context()
        _fb_ctx.remove_class("fb-ok")
        _fb_ctx.remove_class("fb-no")
        S["btn_save"].set_sensitive(True)
        # Type drives the actions: games answer for points and
        # get Next after answering; facts/news skip freely;
        # news opens its link; ideas can be saved for an agent.
        S["game_next"].set_label(
            "Next" if ptype == "game" else "Skip / Next \u2192")
        if ptype == "game":
            S["game_next"].hide()
        else:
            S["game_next"].show()
        if ptype == "news" and game.get("link"):
            S["btn_open"].show()
        else:
            S["btn_open"].hide()
        if ptype == "idea":
            S["btn_save"].show()
        else:
            S["btn_save"].hide()
        S["scene_ev"].set_visible(bool(game.get("scene")
                                       or game.get("_paint")))
        S["game_scene"].queue_draw()
        core.dlog("content", f"serving {game.get('id')}")

    def draw_bulb(area, cr):
        # A lightbulb stroked in the text colour — monochrome
        # in both themes (the emoji was full colour).
        w = area.get_allocated_width()
        h = area.get_allocated_height()
        cx, cy = w / 2, h / 2 - 1.5
        cr.set_source_rgba(*PALC["text"], 0.95)
        cr.set_line_width(1.3)
        cr.arc(cx, cy, 4.6, 0, 6.2832)
        cr.stroke()
        for dy in (4.2, 6.0):
            cr.move_to(cx - 2.2, cy + dy)
            cr.line_to(cx + 2.2, cy + dy)
            cr.stroke()
        return False

    def choose_content_type():
        # TODO: choose from deck statistics instead of dice —
        # underrepresented types first, weighted by the project
        # domain and what the user actually answers/saves.
        import random
        return random.choice(["game", "fact", "news", "idea"])

    def idea_pick_type():
        picked = choose_content_type()
        S["game_fb"].set_text(
            f"Next content to generate: {picked}. (Random pick "
            "for now — statistics-based choice is a TODO; the "
            "researcher agent will create it.)")
        core.dlog("content",
                  f"generation type picked: {picked} (random)")

    def game_open_link():
        game = S["game_ctl"].get("game") or {}
        url = game.get("link") or ""
        if not url:
            return
        try:
            Gtk.show_uri_on_window(S["win"], url, Gdk.CURRENT_TIME)
            core.dlog("content", f"news link opened: {game.get('id')}")
        except Exception as exc:
            S["game_fb"].set_text(f"Could not open the link: {exc}")

    def game_save_idea():
        game = S["game_ctl"].get("game") or {}
        from backend.app import content_researcher as _cr
        path = _cr.save_idea(project_path, game)
        S["btn_save"].set_sensitive(False)
        S["game_fb"].set_text(
            "Saved. A coding agent can pick this up from "
            ".aat/research/saved_ideas.jsonl")
        core.dlog("content", f"idea saved: {game.get('id')} -> {path}")

    def game_answer(target):
        ctl = S["game_ctl"]
        game = ctl.get("game")
        if not game or ctl["answered"]:
            return
        ctl["answered"] = True
        ctl["chosen"] = target
        out = engine.answer(target)
        ctl["verdict"] = out["correct"]
        ctl["verdict_t0"] = time.time()
        expl = out.get("explanation") or ""
        head = {True: "\u2713 Correct. ",
                False: "\u2717 Not quite. "}.get(out["correct"], "")
        ctx = S["game_fb"].get_style_context()
        ctx.remove_class("fb-ok")
        ctx.remove_class("fb-no")
        if out["correct"] is True:
            ctx.add_class("fb-ok")
        elif out["correct"] is False:
            ctx.add_class("fb-no")
        tail = ""
        if game.get("kind", "quiz") == "quiz":
            sc = engine.score
            tail = (f"  Score {sc['correct']}/{sc['answered']}"
                    f" · streak {sc['streak']}")
        S["game_fb"].set_text((head + expl + tail).strip())
        for cid, b in ctl["btns"].items():
            b.set_sensitive(False)
            if cid == game.get("answer"):
                b.get_style_context().add_class("choice-right")
            elif cid == target:
                b.get_style_context().add_class("choice-wrong")
        S["game_next"].show()
        S["game_scene"].queue_draw()

        def _to_bottom():
            adj = S["game_scroll"].get_vadjustment()
            adj.set_value(max(adj.get_upper() - adj.get_page_size(), 0))
            return False
        GLib.idle_add(_to_bottom)
        core.dlog("content", f"answer {target} in {game.get('id')}: "
                             f"correct={out['correct']}")

    def _scene_geom(game, w, h):
        ctl = S["game_ctl"]
        scene = game.get("scene") or {}
        if scene.get("type") == "orbits":
            ctl.update(center=(w / 2, h / 2),
                       radii=ce.ring_radii(w, h,
                                            int(scene.get("rings", 4))),
                       rects={})
        elif scene.get("type") == "diagram":
            ctl.update(rects=ce.node_rects(scene.get("nodes") or [],
                                           w, h), radii=[])
        else:
            ctl.update(rects={}, radii=[])

    def draw_game_scene(area, cr):
        ctl = S["game_ctl"]
        game = ctl.get("game")
        if not game:
            return False
        w, h = area.get_allocated_width(), area.get_allocated_height()
        _scene_geom(game, w, h)  # keeps hit-test geometry fresh
        try:
            ce.paint_scene(cr, game, w, h, PALC,
                           chosen=ctl["chosen"],
                           answered=ctl["answered"],
                           t=time.time() - (ctl.get("t0") or time.time()))
        except Exception as exc:
            if not ctl.get("err_logged"):
                ctl["err_logged"] = True
                core.dlog("content", f"game {game.get('id')} failed "
                                     f"to render: {exc}")
            cr.select_font_face("Inter", 0, 0)
            cr.set_font_size(14)
            cr.set_source_rgba(*PALC["muted"], 0.9)
            cr.move_to(24, 40)
            cr.show_text("This game failed to render.")
        verdict = ctl.get("verdict")
        if verdict is not None \
                and game.get("type", "game") == "game":
            # End-of-game badge: a filled square pops into the
            # centre of the scene with a check or a cross —
            # the same language as the status square's DONE.
            dt = max(time.time() - ctl.get("verdict_t0", 0.0), 0.0)
            pop = min(1.0, dt / 0.35)
            ease = 1.0 - (1.0 - pop) ** 3
            side = min(w, h) * 0.30 * (0.55 + 0.45 * ease)
            alpha = min(1.0, dt / 0.12) * 0.96
            cx, cy = w / 2, h / 2
            x, y = cx - side / 2, cy - side / 2
            r = side * 0.16
            cr.new_path()
            cr.arc(x + side - r, y + r, r, -1.5708, 0)
            cr.arc(x + side - r, y + side - r, r, 0, 1.5708)
            cr.arc(x + r, y + side - r, r, 1.5708, 3.1416)
            cr.arc(x + r, y + r, r, 3.1416, 4.7124)
            cr.close_path()
            cr.set_source_rgba(*PALC["square"], alpha)
            cr.fill()
            glyph = "\u2713" if verdict else "\u2717"
            cr.select_font_face("Inter", 0, 1)
            cr.set_font_size(side * 0.6)
            ext = cr.text_extents(glyph)
            cr.move_to(cx - ext.width / 2 - ext.x_bearing,
                       cy - ext.height / 2 - ext.y_bearing)
            cr.set_source_rgba(*PALC["bg"], alpha)
            cr.show_text(glyph)
        return False

    def on_game_scene_click(_w, event):
        ctl = S["game_ctl"]
        game = ctl.get("game")
        if not game or ctl["answered"]:
            return False
        if (game.get("type", "game") != "game"):
            return False  # only games take scene answers
        w = S["game_scene"].get_allocated_width()
        h = S["game_scene"].get_allocated_height()
        target = ce.pack_target_at(game, w, h, event.x, event.y)
        if target:
            game_answer(target)
        return False

    def _content_visible() -> bool:
        # Actual widget placement, not the requested mode: on the
        # first entry into term_feed the content box is not yet in
        # the right pane, and it must still count as "not shown" so
        # a game gets served.
        if S["content_box"].get_parent() is S["paned"]:
            return True
        return bool(S["overlay_on"]
                    and S["left_stack"].get_visible_child_name()
                    == "content")

    def _maybe_serve():
        ctl = S["game_ctl"]
        if ctl["game"] is None or ctl["answered"]:
            serve_game(True)

    def _half_position():
        return max(S["win"].get_size()[0] // 2, 200)

    def _place_content_left():
        content = S["content_box"]
        if content.get_parent() is not S["left_stack"]:
            if content.get_parent() is not None:
                content.get_parent().remove(content)
            S["left_stack"].add_named(content, "content")

    def _place_content_right():
        content = S["content_box"]
        if content.get_parent() is not S["paned"]:
            if content.get_parent() is not None:
                content.get_parent().remove(content)
            S["paned"].pack2(content, True, False)
            S["paned"].set_position(_half_position())

    def _place_scene_right():
        scene = S["scene"]
        if scene.get_parent() is not S["paned"]:
            if scene.get_parent() is not None:
                scene.get_parent().remove(scene)
            S["paned"].pack2(scene, True, False)
            S["paned"].set_position(_half_position())
        scene.set_visible(True)

    def _move_view_btn(to_feed: bool):
        btn = S["view_btn"]
        slot = S["content_top_row"] if to_feed else S["scene_top_row"]
        if btn.get_parent() is not slot:
            if btn.get_parent() is not None:
                btn.get_parent().remove(btn)
            slot.pack_end(btn, False, False, 0)
        nxt = VIEW_CYCLE[(VIEW_CYCLE.index(S["view_mode"]) + 1)
                         % len(VIEW_CYCLE)]
        btn.set_tooltip_text(
            f"View: {VIEW_LABELS[S['view_mode']]} — click for "
            f"{VIEW_LABELS[nxt]}")

    def apply_view():
        """Place the two panes for S['view_mode'].

        term_vis / feed_vis: the visualisation pane is on the right,
        the terminal or the Feed on the left. term_feed: terminal on
        the left, Feed on the right, no visualisation pane — and the
        view button moves onto the Feed pane so the cycle can always
        be continued."""
        mode = S["view_mode"]
        was_content = _content_visible()
        scene = S["scene"]
        if mode == "term_feed":
            if scene.get_parent() is S["paned"]:
                S["paned"].remove(scene)
            scene.set_visible(False)
            S["square"].stop()
            _place_content_right()
            S["left_stack"].set_visible_child_name("terminal")
            _move_view_btn(True)
        else:
            _place_content_left()
            _place_scene_right()
            S["square"].start()
            target = "content" if mode == "feed_vis" else "terminal"
            if S["left_stack"].get_visible_child_name() != target:
                S["left_stack"].set_visible_child_name(target)
            _move_view_btn(False)
        if _content_visible() and not was_content:
            _maybe_serve()
        if S.get("_view_logged") != mode:
            S["_view_logged"] = mode
            core.dlog("ui", f"view -> {mode} ({VIEW_LABELS[mode]})")
        if S.get("view_btn") is not None:
            S["view_btn"].queue_draw()
        S["mode_btn"].queue_draw()

    def set_view(mode: str, manual: bool = True):
        if mode not in VIEW_CYCLE:
            return
        S["view_mode"] = mode
        if manual:
            S["_view_manual"] = True
            S["left_override"] = "content" if mode == "feed_vis" \
                else "terminal"
        _save_prefs()
        if not S["overlay_on"]:
            set_overlay(True)
        else:
            apply_view()

    def cycle_view():
        """The view icon: cycle the two-pane pairing, like the
        theme icon cycles auto -> light -> dark."""
        nxt = VIEW_CYCLE[(VIEW_CYCLE.index(S["view_mode"]) + 1)
                         % len(VIEW_CYCLE)]
        set_view(nxt)

    def set_overlay(show: bool):
        """Split mode on/off. On: two panes in the chosen view
        pairing (apply_view). Off: a plain terminal, like any other."""
        S["overlay_on"] = show
        scene = S["scene"]
        if show:
            apply_view()
            tick()
        else:
            if scene.get_parent() is S["paned"]:
                S["paned"].remove(scene)
            scene.set_visible(False)
            _place_content_left()
            _move_view_btn(False)
            S["left_stack"].set_visible_child_name("terminal")
            S["square"].stop()
            S["vte"].grab_focus()
        if S.get("_ui_logged") != show:
            S["_ui_logged"] = show
            core.dlog("ui", f"visualization {'on' if show else 'off'}")
        S["mode_btn"].queue_draw()

    def set_layout(mode: str):
        # Kept for old call sites: there is one pane layout now.
        set_overlay(mode != "off")

    def current_left_target() -> str:
        if not S["overlay_on"] or S["view_mode"] == "term_feed":
            return "terminal"
        if S.get("left_override"):
            return S["left_override"]
        # Content earns the left pane once the agent has been
        # away for 7 s — short tasks never interrupt typing.
        if S.get("_agent_busy") and S.get("_busy_since") \
                and time.time() - S["_busy_since"] >= 7:
            return "content"
        return "terminal"

    def apply_left():
        if S["view_mode"] == "term_feed":
            if S["left_stack"].get_visible_child_name() != "terminal":
                S["left_stack"].set_visible_child_name("terminal")
            return
        target = current_left_target()
        if S["left_stack"].get_visible_child_name() != target:
            S["left_stack"].set_visible_child_name(target)
            # Keep the view icon in sync with what is on screen.
            S["view_mode"] = "feed_vis" if target == "content" \
                else "term_vis"
            core.dlog("ui", f"left -> {target} "
                            f"({'manual' if S.get('left_override') else 'auto'})")
            if target == "terminal":
                S["vte"].grab_focus()
            elif target == "content":
                _kick_pick()
                _maybe_serve()
            _move_view_btn(False)  # syncs tooltip + redraws the icon
        S["mode_btn"].queue_draw()

    def toggle_left():
        """Clicking the square swaps the left pane by hand. The
        manual choice lasts until the agent changes phase."""
        if S["view_mode"] == "term_feed":
            set_view("term_vis")
        else:
            set_view("term_vis" if S["view_mode"] == "feed_vis"
                     else "feed_vis")

    def show_terminal():
        set_view("term_vis")

    def show_content():
        """Bring the companion content forward (Feed + visualisation)."""
        set_view("feed_vis")

    def user_close():
        """The user explicitly leaves the overlay: never auto-reopen
        for the same task afterwards (the original stuck-overlay bug)."""
        S["user_closed_task"] = core.gate.get("task_key")
        set_overlay(False)

    def toggle_panel(show=None):
        S["panel_on"] = (not S["panel_on"]) if show is None else show
        S["panel"].set_visible(S["panel_on"])
        if S["panel_on"]:
            refresh_panel()

    def on_key(win, event):
        kv = event.keyval
        ctrl_shift = (event.state & Gdk.ModifierType.CONTROL_MASK) and \
                     (event.state & Gdk.ModifierType.SHIFT_MASK)
        if ctrl_shift and kv in (Gdk.KEY_O, Gdk.KEY_o) or \
                ctrl_shift and kv in (Gdk.KEY_S, Gdk.KEY_s):
            set_overlay(not S["overlay_on"])
            return True
        if kv == Gdk.KEY_F11:
            if S.get("_fullscreen"):
                win.unfullscreen()
                S["_fullscreen"] = False
            else:
                win.fullscreen()
                S["_fullscreen"] = True
            return True
        if kv == Gdk.KEY_Escape:
            if S.get("_fullscreen"):
                win.unfullscreen()
                S["_fullscreen"] = False
            elif S["panel_on"]:
                toggle_panel(False)
            elif S["overlay_on"] and _content_visible():
                show_terminal()
            elif S["overlay_on"]:
                user_close()
            return True
        return False

    def act_open_project():
        dlg = Gtk.FileChooserDialog(
            title="Open project", parent=S["win"],
            action=Gtk.FileChooserAction.SELECT_FOLDER)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                        "Open", Gtk.ResponseType.OK)
        if dlg.run() == Gtk.ResponseType.OK:
            path = dlg.get_filename()
            core.reindex(path)
            S["vte"].feed_child(f"cd {path}\n".encode())
        dlg.destroy()

    def act_muse_task():
        set_overlay(False)
        task = ("Explore this project. Add a Components section to README.md "
                "with a short description of each module, then run the tests "
                "and report the result.")
        # Trusted demo repo only: --yolo avoids a headless approval
        # round-trip. Real projects: run interactive `muse` here and
        # approve in the terminal; the overlay shows "The agent needs you".
        S["vte"].feed_child(
            f'cd {core.project_path} && muse exec --yolo "{task}"\n'.encode())
        S["vte"].grab_focus()

    def add_action(name, fn):
        a = Gio.SimpleAction.new(name, None)
        a.connect("activate", lambda *_: fn())
        app.add_action(a)

    app.connect("activate", lambda *_: build())
    add_action("open-project", act_open_project)
    add_action("muse-task", act_muse_task)
    add_action("panel", lambda: toggle_panel())
    add_action("split", lambda: set_overlay(not S["overlay_on"]))
    add_action("simulate", lambda: core.simulate())
    add_action("quit", lambda: app.quit())

    # ---------------- display state (mock for screenshots) ----------------
    def screen_text():
        term = S.get("term") or S.get("vte")
        if term is None:
            return ""
        try:
            text = _read_vte_text(term)
        except Exception:
            return ""
        if not text:
            return ""
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return "\n".join(lines[-40:])

    def display_state():
        core.refresh_agent_state()
        agent = dict(core.agent_state)
        comp = core.touched_component()
        if not MOCK:
            return agent, comp
        base = {"agent": "muse", "source": "agent_event"}
        mocks = {
            "idle": ({"state": "idle", "activity": ""}, None),
            "working": ({"state": "tool_running", "tool": "file_edit",
                         "activity": "storage/db.py",
                         "files": ["storage/db.py"]}, "storage"),
            "thinking": ({"state": "planning",
                          "activity": "Checking core and storage links before editing",
                          "plan": [
                              {"status": "in_progress", "text": "Explore core ↔ storage links"},
                              {"status": "pending", "text": "Update storage"},
                              {"status": "pending", "text": "Run the tests"}]}, "core"),
            "attention": ({"state": "awaiting_user",
                           "activity": "Approval needed: run the project tests"}, "tests"),
            "completed": ({"state": "completed",
                           "activity": "Modules updated, tests pass"}, "storage"),
            "glitch": ({"state": "tool_running", "tool": "search",
                        "activity": "Searching the codebase for storage usage",
                        "files": []}, "core"),
            "trail": ({"state": "tool_running", "tool": "web_search",
                       "activity": "Searching the web for overlay examples",
                       "files": []}, None),
            "compile": ({"state": "tool_running", "tool": "bash",
                         "command": "cargo build --release",
                         "activity": "bash: cargo build --release",
                         "files": []}, "core"),
            "link": ({"state": "tool_running", "tool": "bash",
                      "command": "ld -o pulseboard main.o engine.o",
                      "activity": "bash: ld -o pulseboard main.o engine.o",
                      "files": []}, "core"),
            "copy": ({"state": "tool_running", "tool": "bash",
                      "command": "cp -r assets/ dist/assets/",
                      "activity": "bash: cp -r assets/ dist/assets/",
                      "files": []}, "storage"),
            "download": ({"state": "tool_running", "tool": "bash",
                          "command": "curl -O https://example.com/app.tar.gz",
                          "activity": "bash: curl -O https://example.com/app.tar.gz",
                          "files": []}, None),
            "test": ({"state": "tool_running", "tool": "bash",
                      "command": "python -m pytest -q",
                      "activity": "bash: python -m pytest -q",
                      "files": []}, "tests"),
            "git": ({"state": "tool_running", "tool": "bash",
                     "command": "git push origin main",
                     "activity": "bash: git push origin main",
                     "files": []}, None),
            "extract": ({"state": "tool_running", "tool": "bash",
                         "command": "tar -xzf app.tar.gz",
                         "activity": "bash: tar -xzf app.tar.gz",
                         "files": []}, None),
            "serve": ({"state": "tool_running", "tool": "bash",
                       "command": "uvicorn app:main --port 8000",
                       "activity": "bash: uvicorn app:main --port 8000",
                       "files": []}, "api"),
            "question": ({"state": "awaiting_user", "needs": "answer",
                          "activity": "Should I also update the README?"}, None),
            "fail": ({"state": "tool_running", "tool": "bash",
                      "command": "frobnicate --all",
                      "activity": "bash: frobnicate --all",
                      "files": []}, "core"),
            "failcompile": ({"state": "tool_running", "tool": "bash",
                             "command": "g++ -c main.cpp -o main.o",
                             "activity": "bash: g++ -c main.cpp -o main.o",
                             "files": []}, "core"),
            "failtest": ({"state": "tool_running", "tool": "bash",
                          "command": "python -m pytest -q",
                          "activity": "bash: python -m pytest -q",
                          "files": []}, "tests"),
            "delete": ({"state": "tool_running", "tool": "bash",
                        "command": "rm -rf cpp_app",
                        "activity": "bash: rm -rf cpp_app",
                        "files": []}, None),
            "install": ({"state": "tool_running", "tool": "bash",
                         "command": "pip install tenacity",
                         "activity": "bash: pip install tenacity",
                         "files": []}, "storage"),
        }
        m, comp_name = mocks.get(MOCK, mocks["idle"])
        agent.update(base)
        agent.update(m)
        if MOCK.startswith("fail"):
            agent["last_failure"] = {
                "tool": "bash", "command": m.get("command"),
                "reason": "process exited with status 1",
                "at": time.time()}
        if comp_name and core.project:
            comp = next((c for c in core.project["components"]
                         if c["name"] == comp_name), comp)
        elif MOCK == "idle":
            comp = None
        return agent, comp

    # ---------------- rendering ----------------
    def refresh_panel():
        lines = []
        for ev in list(core.timeline)[:30]:
            title = ev.get("state") or ev.get("kind") or "event"
            lines.append(f"{title}  {short(ev.get('activity') or ev.get('process') or '', 90)}"
                         f"\n    {(ev.get('timestamp') or '')[11:19]} · {ev.get('source') or ''}")
        S["hist_text"].get_buffer().set_text("\n".join(lines) or "No events yet")
        snap = core.snapshot
        tree = "\n".join(f"  {p['pid']} {p['process']} — {p['cmdline']}"
                         for p in snap.get("process_tree", []))
        agent = core.agent_state
        S["diag_text"].get_buffer().set_text(
            f"cwd (observed): {snap.get('cwd', '—')}\n"
            f"foreground (observed): {(snap.get('foreground') or {}).get('process', '—')}\n"
            f"shell pid: {snap.get('shell_pid', '—')} · command: "
            f"{'running' if snap.get('command_running') else 'none'}\n"
            f"agent CLI (observed): {snap.get('agent_cli_detected') or 'not found'}\n"
            f"adapter session: {core.adapter.current_file or 'not attached'}\n"
            f"state (source={agent.get('source', '—')}): {agent.get('state', '—')}\n"
            f"Muse Spark: {core.enrich_status} · Muse Image world: {core.world_status}\n"
            f"input method: GTK_IM_MODULE={os.environ.get('GTK_IM_MODULE', '—')}"
            f" · settings={Gtk.Settings.get_default().get_property('gtk-im-module') if Gtk.Settings.get_default() else '—'}\n"
            f"System-1 gate (Liquid): {core.gate.get('source') or 'not asked yet'}"
            f" · decision={core.gate.get('decision')} · p={core.gate.get('p')}"
            f" · steps={core.step_count}"
            f" · key={'set' if core.liquid.configured else 'LIQUID_API_KEY missing'}\n"
            f"System-1 screen: {core.liquid_stats['screen_ok']}/"
            f"{core.liquid_stats['screen_calls']} calls ok"
            f" · errors={core.liquid_stats['screen_err']}"
            f" · last={core.liquid_stats['last_choice'] or '—'} "
            f"({core.liquid_stats['last_conf']:.2f})\n\n"
            f"Decision log (latest):\n"
            f"{chr(10).join(list(core.decisions)[-8:]) or '—'}\n\n"
            f"Process tree:\n{tree or '—'}")

    def tick():
        agent, _comp = display_state()
        if S.get("mode_btn"):
            S["mode_btn"].queue_draw()
        # Auto theme follows the system live (checked on this timer;
        # cheap property reads, no GTK settings signal wiring).
        if THEME["mode"] == "auto" and _system_dark() != THEME["dark"]:
            apply_theme()
        chk = core.liquid_check
        if chk["status"] == "ok":
            S["sys1_banner"].hide()
        else:
            S["sys1_banner"].set_text({
                "no-key": "SYSTEM 1 OFFLINE — no LIQUID_API_KEY · "
                          "screen understanding off",
                "failed": f"SYSTEM 1 CHECK FAILED — {chk['detail']}",
                "pending": "SYSTEM 1 — checking connection…",
            }.get(chk["status"], ""))
            S["sys1_banner"].show()
        state = agent.get("state")

        # Mock completion gets demo results for reproducible screenshots.
        if MOCK == "completed" and not core.results:
            core.results = {
                "files": [{"file": f, "add": a, "del": d} for f, a, d in (
                    ("README.md", 18, 2), ("storage/db.py", 12, 9),
                    ("core/engine.py", 30, 4), ("api/routes.py", 3, 14),
                    ("tests/test_engine.py", 22, 0), ("ui/cli.py", 5, 5))],
                "completion": {"score": 3.2, "confidence": 0.0},
                "safety": {"score": 3.8, "confidence": 0.0},
                "source": "mock", "final_state": "completed"}

        # System-1 gate: ask Liquid d1.1 whether this is a long task.
        core.gate_tick(min_seconds=S["set_threshold"].get_value())
        gate = core.gate
        task_key = gate.get("task_key")
        suppressed = S["user_closed_task"] == task_key and task_key is not None
        if S["set_autoshow"].get_active() and not S["overlay_on"] \
                and not suppressed:
            if state in ("completed", "failed") and core.results \
                    and S["results_shown_task"] != task_key:
                S["results_shown_task"] = task_key
                set_overlay(True)  # show what the agent did
            elif gate.get("decision") == "long":
                set_overlay(True)  # System 1 predicted a long task

        if S["overlay_on"]:
            mode = mode_for_agent(agent)
            if getattr(core, "_vanish", None) and S.get("_last_mode"):
                # Ending unresolved: hold the last pose briefly.
                mode = S["_last_mode"]
            # System-1 fills the gaps: generic "working" with no tool
            # is ambiguous — d1.1 classifies the real activity.
            try:
                scr = screen_text()
                if scr and len([l for l in scr.splitlines()
                                if l.strip()]) <= 1:
                    core.clear_outcome("terminal cleared")
                ca = agent
                if not ca.get("agent") and getattr(core, "_vanish",
                                                   None):
                    # Keep reading the screen under the vanished
                    # agent's name so its ending can be resolved.
                    ca = {"agent": core._vanish["agent"],
                          "state": "idle", "source": "heuristic"}
                core.classify_tick(ca, scr)
            except Exception as exc:  # the square must never freeze
                if S.get("_scr_err") != str(exc):
                    S["_scr_err"] = str(exc)
                    core.dlog("system1", f"screen pipeline error: {exc}")
                scr = ""
            ov = core.mode_override
            ambiguous = not agent.get("tool") or agent.get("state") == "working"
            via = "deterministic"
            if ov and ambiguous and mode in ("build", "blinky") \
                    and time.time() - ov["at"] < 9:
                # A 'failed' screen verdict may not interrupt while
                # processes/log facts say the agent is still working:
                # mid-turn error text is not the ending.
                if not (str(ov["mode"]).startswith("fail")
                        and state in ("working", "tool_running")):
                    mode = ov["mode"]
                    via = ("system1-screen" if ov.get("screen")
                           else "system1-activity")
            S["mode"] = mode
            S["square"].set_mode(mode)
            S["_square_mode"] = mode
            # "Busy" for the attention model: log states where we
            # have them, and the screen-evidenced visual mode for
            # adapter-less agents (Codex reasons and browses
            # childless — no processes to observe, but the square
            # already knows it is working).
            busy_now = state in BUSY_STATES or (
                agent.get("source") == "heuristic"
                and mode not in ("blinky", "shuffle", "question",
                                 "done", "fail", "fail_compile",
                                 "fail_link", "fail_test"))
            if busy_now and not S.get("_agent_busy"):
                # A busy blip (< 4 s gap in the screen verdicts) is
                # the same phase: keep the clock and the user's
                # manual pane choice.
                resumed = S.get("_busy_end") \
                    and time.time() - S["_busy_end"] < 4 \
                    and S.get("_busy_since_keep")
                if not resumed:
                    S["_busy_since"] = time.time()
                    S["left_override"] = None  # new phase: auto
                    S["_view_manual"] = False
                    if S["view_mode"] == "term_feed":
                        # A manual Terminal+Feed pairing also ends
                        # with the phase: back to the visualisation.
                        S["view_mode"] = "term_vis"
                        apply_view()
                S["_agent_busy"] = True
            elif not busy_now and S.get("_agent_busy"):
                S["_agent_busy"] = False
                S["_busy_end"] = time.time()
                S["_busy_since_keep"] = S.get("_busy_since")
                S["_busy_since"] = None
            apply_left()
            if S.get("_last_mode") != mode:
                core.dlog("mode", f"{S.get('_last_mode') or '—'} -> {mode} "
                                  f"via {via} (agent={agent.get('agent')}, "
                                  f"state={state}, "
                                  f"src={agent.get('source')})")
                S["_last_mode"] = mode
            label, title = MODE_CAPTIONS[mode]
            if state == "completed":
                title = "Done"
            elif state == "failed":
                title = "Failed"
            S["mode_label"].set_text(label)
            if S["mode_title"].get_text() != title:
                S["mode_title"].set_text(title)
            busy = state in BUSY or state in ("awaiting_user", "completed",
                                              "failed")
            S["mode_sub"].set_text(
                short(pretty_activity(agent), 90) if busy else "")
            if agent.get("source") == "heuristic" and scr:
                # For adapter-less agents the console text is the
                # most honest activity line we have.
                S["mode_sub"].set_text(short(scr.splitlines()[-1], 90))
            S["hint"].set_text(
                "Click the square to return to the terminal"
                if mode == "shuffle"
                else "Esc \u2014 back to the terminal \u00b7 Ctrl+Shift+O")

            # end-of-session results: three squares under the main one
            res = core.results
            show_res = res is not None and (
                state in ("completed", "failed")
                or agent.get("turn_done")
                or agent.get("sticky_outcome")
                or agent.get("interrupted"))
            S["res_row"].set_visible(bool(show_res))
            if show_res:
                files = res["files"]
                S["res_squares"]["files"].set_files(files)
                S["res_labels"]["files"].set_text(f"FILES · {len(files)}")
                for key, caption in (("completion", "DONE"),
                                     ("safety", "SAFE")):
                    sc = res.get(key)
                    if sc:
                        lvl = round(sc["score"])
                        S["res_squares"][key].set_fill(sc["score"] / 4.0)
                        conf = sc.get("confidence") or 0
                        extra = f" · {int(conf * 100)}%" if conf else ""
                        S["res_labels"][key].set_text(
                            f"{caption} · {lvl}/4{extra}")
                    else:
                        S["res_squares"][key].set_fill(None)
                        S["res_labels"][key].set_text(f"{caption} · …")
            if S["panel_on"]:
                refresh_panel()
        return True

    return app.run([sys.argv[0]])


def main():
    args = sys.argv[1:]
    project = None
    if "--project" in args:
        project = args[args.index("--project") + 1]
    return run_gui(project)


if __name__ == "__main__":
    raise SystemExit(main())
