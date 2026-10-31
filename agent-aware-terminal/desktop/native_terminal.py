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
BUSY = ("starting", "planning", "working", "tool_running")


def ensure_demo_project() -> Path:
    dst = DATA_DIR / "demo_project"
    if not dst.is_dir():
        src = RES_ROOT / "demo_project"
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns(
                ".git", "__pycache__", ".venv", ".pytest_cache",
                "pulseboard-data.json"))
    return dst


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

    def on_snapshot(self, snap: dict):
        self.snapshot = snap
        ev = snap.get("event")
        if ev:
            self.timeline.appendleft(ev)

    def refresh_agent_state(self):
        """Adapter facts win; observer heuristics are the fallback."""
        with self._lock:
            adapter_state = self.adapter_state
        if self.adapter.current_file and adapter_state:
            merged = dict(adapter_state)
            merged["agent"] = "muse"
            self.agent_state = merged
            return
        snap = self.snapshot
        inferred = snap.get("inferred") or {}
        cli = snap.get("agent_cli_detected")
        if cli:
            self.agent_state = {
                "agent": cli, "state": inferred.get("state", "working"),
                "activity": (snap.get("foreground") or {}).get("cmdline", ""),
                "source": "heuristic",
                "confidence": inferred.get("confidence"),
                "files": [], "plan": [],
            }
        else:
            self.agent_state = {
                "agent": None, "state": inferred.get("state", "idle"),
                "activity": (snap.get("foreground") or {}).get("process", ""),
                "source": "heuristic", "files": [], "plan": [],
            }

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
    },
    "light": {
        "bg": "#f7f8f8", "surface": "#ffffff", "surface2": "#eef0f2",
        "border": "#e4e5e9", "border2": "#d3d7dd",
        "text": "#0b0c0e", "body": "#34343a", "muted": "#62666d",
        "faint": "#8a8f98", "square": (0.078, 0.082, 0.09),
        "vte_bg": (0.969, 0.973, 0.973), "vte_fg": (0.11, 0.11, 0.12),
    },
}

GROTESK = "'Inter', 'Manrope', 'Helvetica Neue', sans-serif"
MONO = "'JetBrains Mono', 'Berkeley Mono', monospace"


def build_css(dark: bool) -> bytes:
    p = PALETTES["dark" if dark else "light"]
    return f"""
.scene {{ background: {p['bg']}; }}
.ov-project {{ color: {p['text']}; font-family: {GROTESK}; font-size: 15px; font-weight: 600; letter-spacing: -0.15px; }}
.mode-agent {{ color: {p['muted']}; font-family: {MONO}; font-size: 10px; letter-spacing: 2.5px; }}
button.term-return {{ color: {p['text']}; background: transparent; border: 1px solid {p['text']}; border-radius: 9999px; padding: 7px 16px; font-family: {GROTESK}; font-size: 13px; font-weight: 500; }}
button.term-return:hover {{ background: {p['surface2']}; }}
button.quiet {{ color: {p['muted']}; background: transparent; border: none; font-family: {GROTESK}; font-size: 15px; padding: 4px 8px; }}
button.quiet:hover {{ color: {p['text']}; }}
.mode-label {{ color: {p['muted']}; font-family: {MONO}; font-size: 11px; letter-spacing: 3px; }}
.mode-title {{ color: {p['text']}; font-family: {GROTESK}; font-size: 24px; font-weight: 500; letter-spacing: -0.3px; }}
.mode-sub {{ color: {p['muted']}; font-family: {GROTESK}; font-size: 14px; }}
.hint {{ color: {p['faint']}; font-family: {GROTESK}; font-size: 12px; }}
.panel {{ background: {p['surface']}; color: {p['body']}; border-left: 1px solid {p['border']}; }}
.panel textview, .panel textview text {{ background: {p['bg']}; color: {p['body']}; font-family: {MONO}; font-size: 11px; }}
.panel entry {{ background: {p['bg']}; color: {p['text']}; }}
""".encode()


MODE_CAPTIONS = {
    "blinky": ("BLINKY", "Idle"),
    "glitch": ("GLITCH", "Searching the system"),
    "trail": ("TRAIL", "Searching the web"),
    "shuffle": ("SHUFFLE", "The agent needs you"),
    "flash": ("COLOR FLASH", "Thinking"),
    "build": ("BUILD ON", "Writing code"),
}


def mode_for_agent(agent: dict) -> str:
    """Map the normalized agent state/tool to a square animation mode."""
    import re
    st = agent.get("state") or "idle"
    if st == "awaiting_user":
        return "shuffle"
    if st in ("planning", "starting"):
        return "flash"
    if st in ("working", "tool_running"):
        text = f"{agent.get('tool') or ''} {agent.get('activity') or ''}".lower()
        if re.search(r"web|internet|https?://|\burl\b|fetch", text) \
                and "file" not in text:
            return "trail"
        if re.search(r"search|grep|glob|find|read|list|scan", text):
            return "glitch"
        return "build"
    return "blinky"


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
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    gi.require_version("Vte", "2.91")
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Vte

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
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

    app = Gtk.Application(application_id="dev.meta.agentawareterminal",
                          flags=Gio.ApplicationFlags.FLAGS_NONE)
    S: dict = {"win": None, "overlay_on": False, "panel_on": False,
               "mode": "blinky"}

    # ---------------- animated agent square ----------------
    class AgentSquare(Gtk.DrawingArea):
        """One square, six calm animations (see mode_for_agent)."""

        def __init__(self):
            super().__init__()
            self.mode = "blinky"
            self.t = 0.0
            self._timer = None
            self.set_size_request(280, 280)
            self.connect("draw", self._draw)

        def set_mode(self, mode):
            if mode != self.mode:
                self.mode = mode
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
            cr.set_source_rgba(*PAL["square"], alpha)
            cr.fill()

        def _draw(self, _w, cr):
            import colorsys
            import math
            import random
            alloc = self.get_allocation()
            S = float(min(alloc.width, alloc.height))
            if S < 10:
                return False
            m = 0.03 * S
            x = (alloc.width - S) / 2 + m
            y = (alloc.height - S) / 2 + m
            s = S - 2 * m
            t = self.t
            mode = self.mode
            if mode == "blinky":
                phase = t % 3.4
                sy = 1.0
                if phase < 0.22:  # a calm blink
                    sy = max(0.1, abs(phase - 0.11) / 0.11)
                self._fill(cr, x, y + s * (1 - sy) / 2, s, s * sy, 0.92)
            elif mode == "glitch":
                self._fill(cr, x, y, s, s, 0.16)
                active = (t % 1.6) < 0.22
                rng = random.Random(int(t * 9) if active else 0)
                rows = 6
                for i in range(rows):
                    off = (rng.uniform(-0.09, 0.09) * s) if active else 0.0
                    yy = y + s * i / rows
                    self._fill(cr, x + off, yy + 1, s, s / rows - 2,
                               0.55 + 0.35 * rng.random(), r=2)
            elif mode == "trail":
                w2 = 0.52 * s
                for k in range(6, -1, -1):
                    tt = t - k * 0.085
                    cx = x + s / 2 + 0.21 * s * math.sin(tt * 1.25)
                    cy = y + s / 2 + 0.17 * s * math.sin(tt * 1.65 + 1.0)
                    self._fill(cr, cx - w2 / 2, cy - w2 / 2, w2, w2,
                               0.10 + 0.13 * (6 - k) if k else 0.95)
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
                cr.set_source_rgba(*PAL["square"], 0.25 + 0.15 * math.sin(t * 2.4))
                cr.set_line_width(1.5)
                cr.stroke()
            elif mode == "flash":
                hue = (t * 0.045) % 1.0
                r, g, b = colorsys.hsv_to_rgb(hue, 0.42, 0.88)
                self._rr(cr, x, y, s, s, 0.045 * s)
                cr.set_source_rgba(r, g, b, 0.28 + 0.1 * math.sin(t * 1.8))
                cr.fill()
                self._rr(cr, x, y, s, s, 0.045 * s)
                cr.set_source_rgba(r, g, b, 0.9)
                cr.set_line_width(max(2.0, 0.012 * s))
                cr.stroke()
                inner = 0.36 * s
                self._rr(cr, x + (s - inner) / 2, y + (s - inner) / 2,
                         inner, inner, 0.04 * s)
                cr.set_source_rgba(r, g, b, 0.55 + 0.3 * math.sin(t * 1.8))
                cr.fill()
            else:  # build
                self._rr(cr, x, y, s, s, 0.045 * s)
                cr.set_source_rgba(*PAL["square"], 0.35)
                cr.set_line_width(1.5)
                cr.stroke()
                cells = 4
                cw = s / cells
                filled = int(((t % 3.6) / 3.6) * cells * cells + 0.999)
                for i in range(cells * cells):
                    col = i % cells
                    row = cells - 1 - (i // cells)  # build bottom-up
                    alpha = 0.92 if i < filled else 0.07
                    self._fill(cr, x + col * cw + 2, y + row * cw + 2,
                               cw - 4, cw - 4, alpha, r=3)
            return False

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
        S["pill"] = Gtk.Label(label="idle")
        header.pack_end(S["pill"])

        menu = Gio.Menu()
        menu.append("Open project…", "app.open-project")
        menu.append("Run Muse with a task", "app.muse-task")
        menu.append("History & diagnostics", "app.panel")
        menu.append("Simulate events (dev)", "app.simulate")
        menu.append("Quit", "app.quit")
        menubtn = Gtk.MenuButton()
        menubtn.set_image(Gtk.Image.new_from_icon_name(
            "open-menu-symbolic", Gtk.IconSize.BUTTON))
        menubtn.set_menu_model(menu)
        header.pack_end(menubtn)

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
        overlay.add(scrolled)
        win.add(overlay)

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
        btn_return.connect("clicked", lambda *_: set_overlay(False))
        atop.pack_end(btn_return, False, False, 0)
        scene.pack_start(atop, False, False, 0)

        center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=26)
        center.set_halign(Gtk.Align.CENTER)
        center.set_valign(Gtk.Align.CENTER)
        S["square"] = AgentSquare()
        evbox = Gtk.EventBox()
        evbox.add(S["square"])
        evbox.connect("button-press-event", lambda *_: (
            set_overlay(False) if S["mode"] == "shuffle" else None))
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
        scene.pack_start(center, True, True, 0)

        S["hint"] = Gtk.Label(label="")
        S["hint"].get_style_context().add_class("hint")
        S["hint"].set_halign(Gtk.Align.CENTER)
        S["hint"].set_margin_bottom(18)
        scene.pack_start(S["hint"], False, False, 0)

        def _fit_square(_w, alloc):
            side = int(min(0.30 * alloc.width, 0.58 * alloc.height))
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
        row.pack_start(Gtk.Label(label="Show after, sec"), False, False, 0)
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

    # ---------------- actions ----------------
    def set_overlay(show: bool):
        S["overlay_on"] = show
        S["scene"].set_visible(show)
        if show:
            S["square"].start()
            tick()
        else:
            S["square"].stop()
            S["vte"].grab_focus()

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
        if kv == Gdk.KEY_Escape:
            if S["panel_on"]:
                toggle_panel(False)
            elif S["overlay_on"]:
                set_overlay(False)
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
    add_action("simulate", lambda: core.simulate())
    add_action("quit", lambda: app.quit())

    # ---------------- display state (mock for screenshots) ----------------
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
        }
        m, comp_name = mocks.get(MOCK, mocks["idle"])
        agent.update(base)
        agent.update(m)
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
            f"Muse Spark: {core.enrich_status} · Muse Image world: {core.world_status}\n\n"
            f"Process tree:\n{tree or '—'}")

    def tick():
        agent, _comp = display_state()
        S["pill"].set_text(f"● {agent.get('agent') or 'agent'} · {agent.get('state')}")
        # autoshow: keep the user in context on long agent runs
        if S["set_autoshow"].get_active() and not S["overlay_on"]:
            if agent.get("state") in BUSY and core.working_since \
                    and time.time() - core.working_since >= S["set_threshold"].get_value():
                set_overlay(True)
        if S["overlay_on"]:
            mode = mode_for_agent(agent)
            S["mode"] = mode
            S["square"].set_mode(mode)
            label, title = MODE_CAPTIONS[mode]
            st = agent.get("state")
            if st == "completed":
                title = "Done"
            elif st == "failed":
                title = "Failed"
            elif st == "idle" and not agent.get("agent"):
                title = "Waiting for an agent"
            S["mode_label"].set_text(label)
            if S["mode_title"].get_text() != title:
                S["mode_title"].set_text(title)
            busy = st in BUSY or st in ("awaiting_user", "completed", "failed")
            S["mode_sub"].set_text(
                short(pretty_activity(agent), 90) if busy else "")
            S["mode_agent"].set_text(
                (agent.get("agent") or "NO AGENT").upper())
            S["hint"].set_text(
                "Click the square to return to the terminal"
                if mode == "shuffle"
                else "Esc \u2014 back to the terminal \u00b7 Ctrl+Shift+O")
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
