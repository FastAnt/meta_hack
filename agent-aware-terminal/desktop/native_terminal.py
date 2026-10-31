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
        self._done_until = 0.0
        self._done_agent = None
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
            root = self.project_path
            self._baseline = {}

            def _snap():
                try:
                    self._baseline = snapshot_files(root)
                except Exception:
                    self._baseline = {}
            threading.Thread(target=_snap, daemon=True).start()
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
                    if out:
                        self.liquid_stats["screen_ok"] += 1
                        self.liquid_stats["last_choice"] = out["choice"]
                        self.liquid_stats["last_conf"] = out["confidence"]
                        self.dlog("system1",
                                  f"screen -> {out['choice']} "
                                  f"({out['confidence']:.2f}), "
                                  f"mode={out['mode']}")
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
    def _finish_session(self, final_state: str):
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
                self._done_until = 0.0
            elif prev and prev[0] and prev[0] == sig[0] \
                    and prev[1] in busy_states and sig[1] == "idle" \
                    and sig[2] == "heuristic" \
                    and self._busy_started_at \
                    and time.time() - self._busy_started_at >= 5:
                # An adapter-less agent finished a real turn: give
                # the DONE check its moment before the float.
                self._done_until = time.time() + 7
                self._done_agent = sig[0]
                self.dlog("mode", f"turn ended -> done window "
                                  f"({sig[0]})")
            if sig[1] not in busy_states:
                self._busy_started_at = None
        # Agent vanished mid-turn (process exited while busy and
        # never reported an ending): a brief interrupted window
        # beats silently falling back to the float.
        prev_view = getattr(self, "_agent_view", (None, None))
        cur_agent = self.agent_state.get("agent")
        if prev_view[0] and prev_view[1] in (
                "working", "tool_running", "planning", "starting") \
                and not cur_agent:
            self._interrupt_until = time.time() + 5
            self._interrupt_agent = prev_view[0]
            self.dlog("mode", "agent vanished mid-turn -> interrupted "
                              f"({prev_view[0]})")
        self._agent_view = (cur_agent, self.agent_state.get("state"))
        if not cur_agent and time.time() < getattr(
                self, "_interrupt_until", 0):
            self.agent_state = dict(self.agent_state)
            self.agent_state.update({
                "agent": self._interrupt_agent, "interrupted": True,
                "activity": "Agent exited mid-turn"})
        if self.agent_state.get("state") == "idle" \
                and self.agent_state.get("agent") \
                and self.agent_state.get("agent") == self._done_agent \
                and time.time() < self._done_until:
            self.agent_state = dict(self.agent_state)
            self.agent_state["turn_done"] = True

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
.ov-project {{ color: {p['text']}; font-family: {GROTESK}; font-size: 15px; font-weight: 600; letter-spacing: -0.15px; }}
.sys1-banner {{
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px; letter-spacing: 0.08em;
  color: {p['body']}; border: 1px solid {p['border']};
  border-radius: 999px; padding: 4px 12px;
}}
.mode-agent {{ color: {p['muted']}; font-family: {MONO}; font-size: 10px; letter-spacing: 2.5px; }}
button.term-return {{ color: {p['text']}; background: transparent; border: 1px solid {p['text']}; border-radius: 9999px; padding: 7px 16px; font-family: {GROTESK}; font-size: 13px; font-weight: 500; }}
button.term-return:hover {{ background: {p['surface2']}; }}
button.quiet {{ color: {p['muted']}; background: transparent; border: none; font-family: {GROTESK}; font-size: 15px; padding: 4px 8px; }}
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


def _read_vte_text(term) -> str:
    """Best-effort read of the terminal's text. Some VTE builds
    return None from get_text() (a C-side attributes assertion), so
    try the alternatives and take the first that yields text."""
    try:
        text = _coerce_vte_text(term.get_text())
        if text:
            return text
    except Exception:
        pass
    try:
        rows = max(int(term.get_row_count()), 1)
        cols = max(int(term.get_column_count()), 1)
        text = _coerce_vte_text(term.get_text_range(0, 0, rows, cols))
        if text:
            return text
    except Exception:
        pass
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
    _theme = ((_settings.get_property("gtk-theme-name") or "").lower()
              if _settings else "")
    _prefer_dark = (_settings.get_property("gtk-application-prefer-dark-theme")
                    if _settings else False)
    _env_theme = os.environ.get("AAT_THEME", "").lower()
    DARK = _env_theme == "dark" or (
        _env_theme != "light" and (_prefer_dark or "dark" in _theme))
    PAL = PALETTES["dark" if DARK else "light"]
    provider = Gtk.CssProvider()
    provider.load_from_data(build_css(DARK))
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    project_path = project_arg or os.environ.get("AAT_PROJECT") \
        or str(ensure_demo_project())
    core = Core(project_path)

    app = Gtk.Application(
        application_id=os.environ.get("AAT_APP_ID",
                                      "dev.meta.agentawareterminal"),
                          flags=Gio.ApplicationFlags.FLAGS_NONE)
    S: dict = {"win": None, "overlay_on": False, "panel_on": False,
               "mode": "blinky", "user_closed_task": None,
               "results_shown_task": None, "layout": "overlay"}

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
        header.props.title = "Agent-Aware Terminal"
        header.props.subtitle = core.project["name"] if core.project else ""
        win.set_titlebar(header)
        S["header"] = header

        btn_overlay = Gtk.Button(label="Overlay")
        btn_overlay.connect("clicked", lambda *_: set_overlay(True))
        header.pack_end(btn_overlay)
        S["btn_split"] = Gtk.ToggleButton(label="Split")
        S["btn_split"].connect("toggled", lambda b: set_layout(
            "split" if b.get_active() else "overlay"))
        header.pack_end(S["btn_split"])
        S["pill"] = Gtk.Label(label="idle")
        header.pack_end(S["pill"])

        menu = Gio.Menu()
        menu.append("Open project…", "app.open-project")
        menu.append("Run Muse with a task", "app.muse-task")
        menu.append("Split screen", "app.split")
        menu.append("History & diagnostics", "app.panel")
        menu.append("Simulate events (dev)", "app.simulate")
        menu.append("Quit", "app.quit")
        menubtn = Gtk.MenuButton()
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
        paned.pack1(scrolled, True, True)
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
        lbl_proj = Gtk.Label(label=core.project["name"] if core.project else "project")
        lbl_proj.get_style_context().add_class("ov-project")
        lbl_proj.set_xalign(0)
        S["mode_agent"] = Gtk.Label(label="AGENT")
        S["mode_agent"].get_style_context().add_class("mode-agent")
        titlebox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        titlebox.pack_start(lbl_proj, False, False, 0)
        titlebox.pack_start(S["mode_agent"], False, False, 0)
        atop.pack_start(titlebox, False, False, 0)
        btn_more = Gtk.Button(label="···")
        btn_more.get_style_context().add_class("quiet")
        btn_more.connect("clicked", lambda *_: toggle_panel(True))
        atop.pack_end(btn_more, False, False, 8)
        btn_return = Gtk.Button(label="\u2190 Terminal")
        btn_return.get_style_context().add_class("term-return")
        btn_return.connect("clicked", lambda *_: user_close())
        atop.pack_end(btn_return, False, False, 0)
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
        evbox.connect("button-press-event", lambda *_: user_close())
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
            factor = 0.58 if S.get("layout") == "split" else 0.30
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
        GLib.timeout_add(280, tick)
        if os.environ.get("AAT_OPEN_OVERLAY"):
            GLib.timeout_add(1300, lambda: (set_overlay(True), False)[1])
        if os.environ.get("AAT_SPLIT"):
            GLib.timeout_add(900, lambda: (
                S["btn_split"].set_active(True), set_overlay(True), False)[-1])

    # ---------------- actions ----------------
    def set_overlay(show: bool):
        S["overlay_on"] = show
        scene = S["scene"]
        if S["layout"] == "split":
            paned = S["paned"]
            if show:
                if scene.get_parent() is not paned:
                    if scene.get_parent() is not None:
                        scene.get_parent().remove(scene)
                    paned.pack2(scene, True, False)
                    paned.set_position(max(S["win"].get_size()[0] // 2, 200))
                scene.set_visible(True)
                S["square"].start()
                tick()
            else:
                if scene.get_parent() is paned:
                    paned.remove(scene)
                S["square"].stop()
                S["vte"].grab_focus()
            return
        scene.set_visible(show)
        if show:
            S["square"].start()
            tick()
        else:
            S["square"].stop()
            S["vte"].grab_focus()

    def set_layout(mode: str):
        """overlay = visualization floats over the terminal;
        split = terminal | visualization side by side, 50/50."""
        if mode == S.get("layout"):
            return
        S["layout"] = mode
        scene = S["scene"]
        if scene.get_parent() is not None:
            scene.get_parent().remove(scene)
        if mode == "overlay":
            S["root_overlay"].add_overlay(scene)
        if S["overlay_on"]:
            set_overlay(True)
        elif mode == "overlay":
            scene.set_visible(False)

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
        if ctrl_shift and kv in (Gdk.KEY_O, Gdk.KEY_o):
            set_overlay(not S["overlay_on"])
            return True
        if ctrl_shift and kv in (Gdk.KEY_S, Gdk.KEY_s):
            S["btn_split"].set_active(not S["btn_split"].get_active())
            return True
        if kv == Gdk.KEY_Escape:
            if S["panel_on"]:
                toggle_panel(False)
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
            S["header"].props.subtitle = \
                core.project["name"] if core.project else ""
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
    add_action("split", lambda: S["btn_split"].set_active(
        not S["btn_split"].get_active()))
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
        S["pill"].set_text(f"● {agent.get('agent') or 'agent'} · {agent.get('state')}")
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
            # System-1 fills the gaps: generic "working" with no tool
            # is ambiguous — d1.1 classifies the real activity.
            try:
                scr = screen_text()
                core.classify_tick(agent, scr)
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
                mode = ov["mode"]
                via = ("system1-screen" if ov.get("screen")
                       else "system1-activity")
            S["mode"] = mode
            S["square"].set_mode(mode)
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
            S["mode_agent"].set_text(
                (agent.get("agent") or "NO AGENT").upper())
            S["hint"].set_text(
                "Click the square to return to the terminal"
                if mode == "shuffle"
                else "Esc \u2014 back to the terminal \u00b7 Ctrl+Shift+O")

            # end-of-session results: three squares under the main one
            res = core.results
            show_res = state in ("completed", "failed") and res is not None
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
