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


CSS = b"""
window { background: #10151b; }
headerbar { background: #10151b; color: #e9edf0; border-bottom: 1px solid rgba(255,255,255,0.08); }
headerbar .subtitle { color: #96a4ad; font-size: 11px; }
.veil { background: linear-gradient(to bottom, rgba(16,21,27,0.55), rgba(16,21,27,0.05) 22%, rgba(16,21,27,0.12) 55%, rgba(16,21,27,0.93) 92%); }
.ov-project { color: #f2f6f8; font-size: 15px; font-weight: 600; }
.ov-mode { color: #8cc8be; font-size: 10px; font-weight: 700; letter-spacing: 2px; }
.ov-mode.attention { color: #e8b26a; }
.ov-mode.done { color: #9fd6a8; }
.ov-mode.failed { color: #e88a7a; }
button.term-return { color: #f2f6f8; background: rgba(16,21,27,0.6); border: 1px solid rgba(255,255,255,0.22); border-radius: 999px; padding: 6px 15px; font-size: 13px; }
button.term-return:hover { border-color: #8cc8be; color: #8cc8be; }
button.term-return.attention { border-color: #e8b26a; color: #e8b26a; }
.status-kicker { color: #8cc8be; font-size: 10px; font-weight: 700; letter-spacing: 2.5px; }
.status-kicker.attention { color: #e8b26a; }
.status-kicker.done { color: #9fd6a8; }
.status-kicker.failed { color: #e88a7a; }
.status-title { color: #f2f6f8; font-size: 30px; font-weight: 600; }
.status-sub { color: #b9c6cd; font-size: 14px; }
.status-plan { color: #8fa0aa; font-size: 12px; }
progressbar trough { background: rgba(255,255,255,0.14); min-height: 2px; border-radius: 2px; border: none; }
progressbar progress { background: #8cc8be; min-height: 2px; border-radius: 2px; border: none; }
.insight-kind { color: #96a4ad; font-size: 10px; font-weight: 700; letter-spacing: 2.5px; }
.insight-count { color: #71828d; font-size: 10px; }
.insight-title { color: #eef4f6; font-size: 16px; font-weight: 600; }
.insight-body { color: #c8d3d9; font-size: 14px; }
.insight-why { color: #96a4ad; font-size: 12px; }
button.quiet { color: #c8d3d9; background: transparent; border: 1px solid transparent; border-radius: 6px; font-size: 13px; padding: 5px 9px; }
button.quiet:hover { border-color: rgba(255,255,255,0.25); color: #ffffff; }
button.quiz { color: #dfe8ec; background: rgba(16,21,27,0.5); border: 1px solid rgba(255,255,255,0.16); border-radius: 8px; padding: 6px 10px; font-size: 13px; }
button.quiz.correct { border-color: #9fd6a8; color: #9fd6a8; }
button.quiz.wrong { border-color: #e88a7a; color: #e88a7a; }
button.pin { background: rgba(255,255,255,0.65); border: none; border-radius: 999px; min-width: 8px; min-height: 8px; padding: 0; box-shadow: 0 0 8px rgba(0,0,0,0.6); }
button.pin.active { background: #8cc8be; min-width: 13px; min-height: 13px; box-shadow: 0 0 0 6px rgba(140,200,190,0.25), 0 0 26px #8cc8be; }
.pin-label { color: #eef4f6; font-size: 11px; font-weight: 600; text-shadow: 0 1px 6px rgba(0,0,0,0.9); }
.panel { background: #19212a; color: #e9edf0; border-left: 1px solid rgba(255,255,255,0.09); }
.panel textview, .panel textview text { background: #10151b; color: #c8d3d9; font-family: monospace; font-size: 11px; }
.panel entry { background: #10151b; color: #e9edf0; }
"""

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
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    project_path = project_arg or os.environ.get("AAT_PROJECT") \
        or str(ensure_demo_project())
    core = Core(project_path)

    app = Gtk.Application(application_id="dev.meta.agentawareterminal",
                          flags=Gio.ApplicationFlags.FLAGS_NONE)
    S: dict = {"win": None, "overlay_on": False, "panel_on": False,
               "pin_sig": "", "insight_sig": "", "insight_idx": 0,
               "cards_cache": [], "quiz": {}, "selected_comp": None,
               "settings": {"autoshow": True, "threshold": 5}}

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
        menu.append("Enrich · Muse Spark", "app.enrich")
        menu.append("History & diagnostics", "app.panel")
        menu.append("Session poster · Muse Image", "app.poster")
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
        vte.set_color_background(Gdk.RGBA(0.043, 0.063, 0.082, 1))
        vte.set_color_foreground(Gdk.RGBA(0.84, 0.88, 0.91, 1))
        scrolled = Gtk.ScrolledWindow()
        scrolled.add(vte)
        S["vte"] = vte
        pty_info = VtePtyInfo(vte)

        overlay = Gtk.Overlay()
        overlay.add(scrolled)
        win.add(overlay)

        # ---- overlay scene (native widgets over the terminal) ----
        S["world_img"] = Gtk.Image()
        S["world_img"].set_hexpand(True)
        S["world_img"].set_vexpand(True)
        world_layer = Gtk.Overlay()
        world_layer.add(S["world_img"])
        veil = Gtk.Box()
        veil.get_style_context().add_class("veil")
        veil.set_hexpand(True)
        veil.set_vexpand(True)
        world_layer.add_overlay(veil)
        S["pins_fixed"] = Gtk.Fixed()
        S["pins_fixed"].set_hexpand(True)
        S["pins_fixed"].set_vexpand(True)
        world_layer.add_overlay(S["pins_fixed"])
        # content column over the scene
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.set_hexpand(True)
        content.set_vexpand(True)
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        top.set_margin_top(14)
        top.set_margin_start(20)
        top.set_margin_end(20)
        lbl_proj = Gtk.Label(label=core.project["name"] if core.project else "project")
        lbl_proj.get_style_context().add_class("ov-project")
        lbl_proj.set_xalign(0)
        S["ov_mode"] = Gtk.Label(label="AGENT MODE")
        S["ov_mode"].get_style_context().add_class("ov-mode")
        titlebox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        titlebox.pack_start(lbl_proj, False, False, 0)
        titlebox.pack_start(S["ov_mode"], False, False, 0)
        top.pack_start(titlebox, False, False, 0)
        top.pack_end(btn_return := Gtk.Button(label="↩ Terminal"), False, False, 0)
        btn_return.get_style_context().add_class("term-return")
        S["btn_return"] = btn_return
        btn_return.connect("clicked", lambda *_: set_overlay(False))
        btn_more = Gtk.Button(label="···")
        btn_more.get_style_context().add_class("quiet")
        btn_more.connect("clicked", lambda *_: toggle_panel(True))
        top.pack_end(btn_more, False, False, 8)
        content.pack_start(top, False, False, 0)
        content.pack_start(Gtk.Box(), True, True, 0)

        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=48)
        bottom.set_margin_start(20)
        bottom.set_margin_end(20)
        bottom.set_margin_bottom(22)
        status = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        S["kicker"] = Gtk.Label(label="AGENT MODE")
        S["kicker"].get_style_context().add_class("status-kicker")
        S["kicker"].set_xalign(0)
        S["title"] = Gtk.Label(label="Waiting for an agent")
        S["title"].get_style_context().add_class("status-title")
        S["title"].set_xalign(0)
        S["sub"] = Gtk.Label(label="")
        S["sub"].get_style_context().add_class("status-sub")
        S["sub"].set_xalign(0)
        S["sub"].set_ellipsize(3)
        S["bar"] = Gtk.ProgressBar()
        S["bar"].set_size_request(420, 2)
        S["plan"] = Gtk.Label(label="")
        S["plan"].get_style_context().add_class("status-plan")
        S["plan"].set_xalign(0)
        for w in (S["kicker"], S["title"], S["sub"], S["bar"], S["plan"]):
            status.pack_start(w, False, False, 0)
        bottom.pack_start(status, True, True, 0)

        insight = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        insight.set_size_request(400, -1)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        S["i_kind"] = Gtk.Label(label="PROJECT INSIGHT")
        S["i_kind"].get_style_context().add_class("insight-kind")
        S["i_kind"].set_xalign(0)
        S["i_count"] = Gtk.Label(label="")
        S["i_count"].get_style_context().add_class("insight-count")
        head.pack_start(S["i_kind"], True, True, 0)
        head.pack_end(S["i_count"], False, False, 0)
        insight.pack_start(head, False, False, 0)
        S["i_title"] = Gtk.Label(label="")
        S["i_title"].get_style_context().add_class("insight-title")
        S["i_title"].set_xalign(0)
        insight.pack_start(S["i_title"], False, False, 0)
        S["i_body"] = Gtk.Label(label="")
        S["i_body"].get_style_context().add_class("insight-body")
        S["i_body"].set_xalign(0)
        S["i_body"].set_line_wrap(True)
        S["i_body"].set_max_width_chars(46)
        insight.pack_start(S["i_body"], False, False, 0)
        S["i_opts"] = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        insight.pack_start(S["i_opts"], False, False, 0)
        S["i_why"] = Gtk.Label(label="")
        S["i_why"].get_style_context().add_class("insight-why")
        S["i_why"].set_xalign(0)
        S["i_why"].set_line_wrap(True)
        insight.pack_start(S["i_why"], False, False, 0)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        S["btn_save"] = Gtk.Button(label="♡ Save")
        S["btn_save"].get_style_context().add_class("quiet")
        S["btn_action"] = Gtk.Button(label="")
        S["btn_action"].get_style_context().add_class("quiet")
        S["btn_action"].set_visible(False)
        S["btn_next"] = Gtk.Button(label="Next →")
        S["btn_next"].get_style_context().add_class("quiet")
        S["btn_save"].connect("clicked", lambda *_: on_save())
        S["btn_action"].connect("clicked", lambda *_: on_action())
        S["btn_next"].connect("clicked", lambda *_: on_next())
        actions.pack_start(S["btn_save"], False, False, 0)
        actions.pack_start(S["btn_action"], False, False, 0)
        actions.pack_start(S["btn_next"], False, False, 0)
        insight.pack_start(actions, False, False, 4)
        bottom.pack_start(insight, False, False, 0)
        content.pack_start(bottom, False, False, 0)

        scene_layer = Gtk.Overlay()
        # world at the back, content above; scene becomes the overlay child
        scene_root = Gtk.Box()
        scene_root.set_hexpand(True)
        scene_root.set_vexpand(True)
        scene_root.pack_start(world_layer, True, True, 0)
        outer = Gtk.Overlay()
        outer.add(scene_root)
        outer.add_overlay(content)
        content.set_hexpand(True)
        content.set_vexpand(True)
        S["scene"] = outer
        outer.set_visible(False)
        overlay.add_overlay(outer)
        # pins live in world_layer's fixed; re-position on resize
        S["world_layer"] = world_layer

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
        outer.set_visible(False)
        panel.set_visible(False)
        core.world_async()

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
            core.world_async()
            refresh_scene(force=True)
            refresh_insight(force=True)
        else:
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
            core.world_async()
            S["cards_cache"] = []
            refresh_insight(force=True)
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
    add_action("enrich", lambda: core.enrich_async())
    add_action("panel", lambda: toggle_panel())
    add_action("poster", lambda: (core.poster_async(), set_overlay(True)))
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
    def refresh_scene(force=False):
        agent, comp = display_state()
        # world image (cover-fit)
        if core.world_path and Path(core.world_path).is_file():
            try:
                alloc = S["world_layer"].get_allocation()
                pb = GdkPixbuf.Pixbuf.new_from_file(str(core.world_path))
                iw, ih = pb.get_width(), pb.get_height()
                w = max(alloc.width, 2)
                h = max(alloc.height, 2)
                scale = max(w / iw, h / ih)
                nw, nh = max(int(iw * scale), w), max(int(ih * scale), h)
                scaled = pb.scale_simple(nw, nh, GdkPixbuf.InterpType.BILINEAR)
                x = max((nw - w) // 2, 0)
                y = max((nh - h) // 2, 0)
                S["world_img"].set_from_pixbuf(
                    scaled.new_subpixbuf(x, y, w, h))
            except Exception:
                import traceback
                traceback.print_exc()
        # pins
        if not core.project:
            return
        alloc = S["pins_fixed"].get_allocation()
        w, h = max(alloc.width, 2), max(alloc.height, 2)
        comps = core.project["components"][:8]
        sig = f"{comp['id'] if comp else ''}|{w}x{h}|{len(comps)}"
        if sig == S["pin_sig"] and not force:
            return
        S["pin_sig"] = sig
        for child in S["pins_fixed"].get_children():
            S["pins_fixed"].remove(child)
        import math
        for i, c in enumerate(comps):
            angle = (i / max(len(comps), 1)) * 2 * math.pi - math.pi / 2
            x = int((0.5 + 0.33 * math.cos(angle)) * w)
            y = int((0.42 + 0.30 * math.sin(angle)) * h)
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
            dot = Gtk.Button()
            dot.get_style_context().add_class("pin")
            active = comp and c["id"] == comp["id"]
            if active:
                dot.get_style_context().add_class("active")
            dot.connect("clicked", lambda _b, cid=c["id"]: select_comp(cid))
            box.pack_start(dot, False, False, 0)
            if active:
                lab = Gtk.Label(label=c["name"])
                lab.get_style_context().add_class("pin-label")
                box.pack_start(lab, False, False, 0)
            S["pins_fixed"].put(box, x, y)
        S["pins_fixed"].show_all()

    def select_comp(cid):
        S["selected_comp"] = cid
        S["insight_idx"] = 0
        refresh_insight(force=True)

    def build_insights():
        agent, comp = display_state()
        cards = core.cards()
        out, seen = [], set()

        def push(item):
            if item["id"] not in seen:
                seen.add(item["id"])
                out.append(item)
        if agent.get("state") == "completed" and not MOCK:
            files = core.stats["files_touched"]
            push({"id": "completed", "kind": "SESSION",
                  "title": "Session complete",
                  "body": f"Files touched: {len(files)} · events: {core.stats['events']}",
                  "action": ("Session poster", "poster")})
        focus_id = S["selected_comp"] or (comp["id"] if comp else None)
        by_comp = {}
        for c in cards:
            if c.get("component_id"):
                by_comp.setdefault(c["component_id"], c)
        if focus_id and core.project:
            focus = next((c for c in core.project["components"]
                          if c["id"] == focus_id), None)
            card = by_comp.get(focus_id)
            if focus:
                push({"id": f"focus-{focus_id}",
                      "kind": "CURRENT COMPONENT" if comp and comp["id"] == focus_id else "COMPONENT",
                      "title": focus["name"],
                      "body": card["body"] if card else focus["description"],
                      "card": card})
        for c in cards:
            if c["kind"] == "quiz":
                push({"id": c["id"], "kind": "QUICK QUESTION", "title": "",
                      "body": c["body"], "card": c,
                      "options": c.get("options"),
                      "answer": c.get("answer_index"), "why": c.get("why")})
        for c in cards:
            if c["kind"] == "overview":
                push({"id": c["id"], "kind": "PROJECT INSIGHT",
                      "title": c["title"], "body": c["body"], "card": c})
            elif c["kind"] in ("explanation", "component") and c.get("component_id") != focus_id:
                push({"id": c["id"], "kind": "PROJECT INSIGHT",
                      "title": c["title"], "body": c["body"], "card": c})
        if not out:
            out = [{"id": "empty", "kind": "PROJECT INSIGHT", "title": "",
                    "body": "Open a project — facts about its architecture will appear here."}]
        return out

    def refresh_insight(force=False):
        items = build_insights()
        idx = S["insight_idx"] % len(items)
        cur = items[idx]
        sig = cur["id"] + str(S["quiz"].get(cur["id"], ""))
        if sig == S["insight_sig"] and not force:
            return
        S["insight_sig"] = sig
        S["current_insight"] = cur
        S["i_kind"].set_text(cur["kind"])
        S["i_count"].set_text(f"{idx + 1} / {len(items)}" if len(items) > 1 else "")
        S["i_title"].set_text(cur.get("title") or "")
        S["i_body"].set_text(short(cur.get("body") or "", 260))
        for ch in S["i_opts"].get_children():
            S["i_opts"].remove(ch)
        S["i_why"].set_text("")
        if cur.get("options"):
            picked = S["quiz"].get(cur["id"])
            for i, opt in enumerate(cur["options"]):
                b = Gtk.Button(label=opt)
                b.get_style_context().add_class("quiz")
                if picked is not None:
                    if i == cur["answer"]:
                        b.get_style_context().add_class("correct")
                    elif i == picked:
                        b.get_style_context().add_class("wrong")
                b.connect("clicked", lambda _b, i=i, cid=cur["id"]:
                          (S["quiz"].__setitem__(cid, i), refresh_insight(force=True)))
                S["i_opts"].pack_start(b, False, False, 0)
            if picked is not None:
                S["i_why"].set_text(cur.get("why") or "")
            S["i_opts"].show_all()
        act = cur.get("action")
        S["btn_action"].set_visible(bool(act))
        if act:
            S["btn_action"].set_label(act[0])
        S["items_len"] = len(items)

    def on_save():
        cur = S.get("current_insight") or {}
        card = cur.get("card") or {
            "id": cur.get("id", "insight"), "kind": "insight",
            "title": cur.get("title") or cur.get("kind", ""),
            "body": cur.get("body", "")}
        core.store.save(card)
        toast("Saved")

    def on_action():
        cur = S.get("current_insight") or {}
        act = cur.get("action")
        if act and act[1] == "poster":
            core.poster_async()

    def on_next():
        n = S.get("items_len", 1)
        if n > 1:
            S["insight_idx"] = (S["insight_idx"] + 1) % n
            S["insight_sig"] = ""
            refresh_insight(force=True)

    toast_win = {"w": None}
    def toast(text):
        if toast_win["w"] is not None:
            toast_win["w"].destroy()
        w = Gtk.Window(type=Gtk.WindowType.POPUP)
        lab = Gtk.Label(label=text)
        lab.set_margin_top(8); lab.set_margin_bottom(8)
        lab.set_margin_start(16); lab.set_margin_end(16)
        w.add(lab)
        w.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        w.set_transient_for(S["win"])
        w.show_all()
        toast_win["w"] = w
        GLib.timeout_add(2400, lambda: (w.destroy(), False)[1])

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
        agent, comp = display_state()
        mode, kicker, title, sub = status_view(agent, comp)
        S["pill"].set_text(f"● {agent.get('agent') or 'agent'} · {agent.get('state')}")
        # autoshow (TZ: keep the user in context on long agent runs)
        if S["set_autoshow"].get_active() and not S["overlay_on"]:
            if agent.get("state") in BUSY and core.working_since \
                    and time.time() - core.working_since >= S["set_threshold"].get_value():
                set_overlay(True)
        if S["overlay_on"]:
            S["kicker"].set_text(kicker)
            for cls in ("attention", "done", "failed"):
                S["kicker"].get_style_context().remove_class(cls)
                S["ov_mode"].get_style_context().remove_class(cls)
            if mode != "busy":
                S["kicker"].get_style_context().add_class(mode)
                S["ov_mode"].get_style_context().add_class(mode)
            S["ov_mode"].set_text(
                "NEEDS ATTENTION" if mode == "attention"
                else "COMPLETED" if mode == "done"
                else "FAILED" if mode == "failed" else "AGENT MODE")
            ctx = S["btn_return"].get_style_context()
            ctx.remove_class("attention")
            if mode == "attention":
                ctx.add_class("attention")
            if S["title"].get_text() != title:
                S["title"].set_text(title)
            S["sub"].set_text(sub)
            plan = agent.get("plan") or []
            if plan:
                done = sum(1 for p in plan
                           if p.get("status") in ("completed", "done"))
                S["bar"].set_fraction(done / len(plan))
                cur = next((p for p in plan
                            if p.get("status") == "in_progress"), None)
                S["plan"].set_text(
                    f"Step {plan.index(cur) + 1} of {len(plan)} · {cur['text']}"
                    if cur else f"Steps: {len(plan)} · done {done}")
            elif agent.get("state") in BUSY:
                S["bar"].pulse()
                S["plan"].set_text("")
            else:
                S["bar"].set_fraction(
                    1.0 if agent.get("state") == "completed" else 0.0)
                S["plan"].set_text("")
            refresh_scene()
            refresh_insight()
            if core.poster_status == "ready" and core.poster_path \
                    and not S.get("poster_shown"):
                S["poster_shown"] = True
                show_poster()
        if core.enrich_status == "ready" and not S.get("enrich_seen"):
            S["enrich_seen"] = True
            S["insight_sig"] = ""
            refresh_insight(force=True)
        return True

    def show_poster():
        dlg = Gtk.Dialog(title="Session poster", parent=S["win"])
        dlg.set_default_size(560, -1)
        box = dlg.get_content_area()
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                str(core.poster_path), 520, 360, True)
            box.add(Gtk.Image.new_from_pixbuf(pb))
        except Exception:
            pass
        files = core.stats["files_touched"]
        lab = Gtk.Label(label=f"{core.project['name']} · files touched: {len(files)}"
                              f" · events: {core.stats['events']}")
        lab.set_margin_top(8)
        box.add(lab)
        dlg.add_button("Save PNG", 1)
        dlg.add_button("Close", Gtk.ResponseType.CLOSE)
        if dlg.run() == 1:
            out = DATA_DIR / "agent-session-poster.png"
            try:
                pb2 = GdkPixbuf.Pixbuf.new_from_file(str(core.poster_path))
                pb2.savev(str(out), "png", [], [])
                toast(f"Saved: {out}")
            except Exception:
                pass
        dlg.destroy()

    return app.run([sys.argv[0]])


def main():
    args = sys.argv[1:]
    project = None
    if "--project" in args:
        project = args[args.index("--project") + 1]
    return run_gui(project)


if __name__ == "__main__":
    raise SystemExit(main())
