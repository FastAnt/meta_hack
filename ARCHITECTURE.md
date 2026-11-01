# Agent-Aware Terminal — Architecture

*A Linux terminal that knows when an AI coding agent is working
inside it, shows what the agent is doing as one calm animated
square, and fills long waits with project-relevant mini-games,
facts, news and ideas — chosen by context, not at random.*

This document is the canonical architecture description. For
day-to-day agent work read `AGENTS.md` (root) first; for games
read `agent-aware-terminal/games-base/AGENTS.md` and
`FACTS.md`; for the content-creator component read
`agent-aware-terminal/content-researcher/`.

## Product principles (invariants — do not break)

1. **No web anything in the product.** The app is a single
   native process: GTK 3 + VTE terminal, cairo-drawn status
   pane. No WebKit, no HTML, no local server. (Opening a news
   link in the system browser is a URI hand-off, not a UI.)
2. **AI draws, code tells the truth.** Generated/AI content
   never carries factual numbers. Everything factual on screen
   is measured by code: changed files come from a filesystem
   baseline diff, states from agent logs/commands; models only
   *judge* (completion/safety scores, picks) and their outputs
   are labelled with confidence.
3. **Deterministic signals outrank models.** Agent state comes
   first from logs and running commands; System-1 (Liquid
   d1.1) is an arbiter for ambiguity (screen reading, picks,
   scores), never the primary source. History/replay is
   context, never live truth: an unwatched turn earns no DONE
   and no scores.
4. **Minimal, system-native UI.** Pane follows the system
   theme (dark/light), bundled Inter grotesk, monochrome
   everywhere — no chromatic accents, no drop shadows.
5. **Two agents, separate jobs.** The *coding* agent works in
   the terminal; a separate *content researcher* agent creates
   games/facts about the project and writes only into the
   project's hidden `.aat/` folder. Neither does the other's
   job.
6. **Content is data, not code in the binary.** Games and
   facts are external packs hot-loaded from disk; the binary
   contains zero content.

## Repository map

| Path | What it is |
| --- | --- |
| `README.md` | Public pitch |
| `AGENTS.md` | Agent quickstart + hard rules (read first) |
| `ARCHITECTURE.md` | This file |
| `agent_aware_terminal_tz_v0_2.md` | Original hackathon brief |
| `agent-aware-terminal/` | The application |
| `agent-aware-terminal/desktop/native_terminal.py` | The whole GUI + `Core` orchestration (~3 600 lines, single file) |
| `agent-aware-terminal/backend/app/` | Backend modules (see below) |
| `agent-aware-terminal/backend/tests/` | Unit tests (unittest) |
| `agent-aware-terminal/tools/` | `test_game.py`, `simulate.py`, `check_states.py`, `content_researcher.py` |
| `agent-aware-terminal/games-base/` | **Git submodule** → separate repo `FastAnt/agent-aware-terminal-games`: 300+ games, 500 facts, authoring guides (`AGENTS.md`, `FACTS.md`, `REVIEW.md`, `FACTS_REVIEW.md`) |
| `agent-aware-terminal/content-researcher/` | The creator-agent component: `SKILL.md`, skills, templates, agent prompt |
| `agent-aware-terminal/demo_project/` | Toy project "pulseboard" for demos/tests |
| `agent-aware-terminal/sims/` | JSONL terminal simulations for headless state checks |
| `agent-aware-terminal/build-binary.sh` | PyInstaller build → `dist/agent-aware-terminal/agent-aware-terminal` (+ copies `games-base` next to it) |
| `agent-aware-terminal/run.sh` | Run from source (system GTK) |
| `agent-aware-terminal/keys.sh` | **Local secrets, never committed** (git-ignored). Auto-loaded: `MODEL_API_KEY` (Meta), `SYSTEM_1_MODEL_API_KEY`/`LIQUID_API_KEY` (Liquid) |

## Runtime architecture

One process. `Core` (in `native_terminal.py`) owns state;
backend modules are pure services it calls.

```
terminal (VTE) ──► session_observer (/proc foreground tree)
       │                     │
       ▼                     ▼
muse_adapter (session      Core.refresh_agent_state()
 JSONL logs) ─────────────►  ├─ source priority: adapter logs >
                             │  observer child-process busy >
                             │  screen text → System-1 classify
                             ├─ mode_for_agent() → square mode
                             ├─ turn lifecycle: baseline snapshot
                             │  → session_diff (FILES) → System-1
                             │  scoring (DONE/SAFE) at turn end
                             └─ decision log (every judgment)
GUI (GTK): header (theme cycle button, mode square, menu — no
title, no status pill), two panes whose pairing the view cycle
button chooses (terminal/feed/visualisation), right pane
(AgentSquare + result squares)
content_engine ◄── packs from disk (games-base, user dir,
                             project .aat/content) ──► content_selector
                             (vector RAG + System-1 pick +
                             reactions + generation requests)
```

### Agent state pipeline

States: `starting, planning, working, tool_running,
awaiting_user, completed, failed, idle`. Three sources, kept
separate by design:

- **`muse_adapter`** tails Muse session JSONL (tool calls,
  bash commands, final replies, failures, approval waits).
  Replayed history is clamped: it can never emit live
  completions (a fresh terminal must not open on DONE).
- **`session_observer`** watches the PTY foreground process
  tree via `/proc`: a foreign agent (Codex, …) counts as busy
  only while it has descendant processes; the user's own
  shell commands are classified too (`sudo apt update` →
  installing).
- **Screen + System-1**: for adapter-less agents the VTE text
  is classified by Liquid d1.1 (`classify_screen`) into the
  same modes; confidence gates apply, deterministic facts
  (child processes, commands) always win.

`mode_for_agent()` maps state+tool+command to one of ~20
square animations, all monochrome: floating (idle), thinking
(rotating inner square), system search (grid), web search
(flying squares), code writing (lines), compiling (Tetris),
linking, copying, installing, downloading, testing (checks),
git, extracting, serving, deleting, shuffle (approval),
question (?), done (static check), fail family (shattering
square). Bash commands are classified by verb tables
(apt/pip/cargo/git/tar/pytest/make/…).

Turn lifecycle: busy start snapshots the project tree;
turn end diffs it (**FILES** = measured truth), then System-1
scores completion and safety 0–4 with confidence (**DONE**,
**SAFE**). A sticky outcome keeps DONE/FAIL visible after the
agent exits, until the next turn. Every judgment lands in the
**decision log** (`~/.local/share/agent-aware-terminal/decisions.log`,
tags `[state] [gate] [system1] [mode] [score] [ui] [content]`).

### Attention model (UI)

The Visualization-mode button (mini square in the header)
toggles the two-pane split. Inside the split, a borderless view
icon (on the right pane) cycles the pairing like the theme icon
cycles auto → light → dark: Terminal + visualisation → Feed +
visualisation → Terminal + Feed → back (in Terminal + Feed the
icon sits on the Feed pane, so the cycle is always reachable).
Left pane rule: agent busy ≥ 7 s → companion content; agent
waits/finishes → terminal. Clicking the big square swaps panes
manually until the phase changes. Esc peels back layer by layer.
The header theme icon cycles Auto (follows the system, switches
live) → Light → Dark; the choice persists in `ui_prefs.json`
(together with the view pairing). Icon-only buttons carry no
border/outline anywhere; hover alone highlights them.

### Content system

Packs are single files, discovered in priority order:
`$AAT_CONTENT_DIR` → project `.aat/content` → user data dir →
`games-base` (next to the binary / repo). Hot-reload on file
change. Two forms:

- **Python module** (`GAME` dict + `paint(cr, view)`, optional
  `target_at`) — required for games and for bespoke visuals.
- **JSON card** — simple text packs.

Every pack declares `"type"` explicitly (guarded by tests):

| type | Behaviour |
| --- | --- |
| `game` | quiz, scored (points + streaks) |
| `fact` | read-only, Skip/Next |
| `news` | Skip/Next + Open link (http/https `link`) |
| `idea` | Save → `.aat/research/saved_ideas.jsonl` for a future agent |

The deck never repeats until every pack has been served
(persisted in `content_state.json`). **One fact = one bespoke
visual** authored for it — reused scene skeletons fail the
test suite (`FactBespokeVisualTests`).

### Contextual selection (`content_selector`)

Replaces random serving:

1. **Context** = project name + researched domains
   (`content_researcher.profile_project`) + the agent's
   current task.
2. **Retrieval** — a local vector index (SQLite
   `content_index.db`): **EmbeddingGemma-2** int8 ONNX
   (Apache 2.0; downloaded once; mean pooling, MRL 256-dim;
   documents as `title: … | text: …`, queries as
   `task: search result | query: …`), with a zero-dependency
   hashing-vector fallback. Vectors are model-tagged, never
   mixed. Project `.aat/content` pool first (closest 10 not
   yet shown), else the global catalogue's top 10.
3. **System-1 picks** the single best of the 10
   (`LiquidClient.pick_best`); ranking order is the fallback.
4. **Reactions** (♥/✕ under the content) persist per project
   and bend ranking: +0.15·cos(liked centroid),
   −0.25·cos(disliked centroid); strong relevance still wins.
   Serve dwell is recorded (fast skip < 3.5 s = weak negative).
5. **Generation decision** — project pool missing/mismatched
   (< 0.35) *and* global fit weak (< 0.50) *and* 30 min
   cooldown passed *and* System-1 agrees a gap exists → a
   request is appended to
   `<project>/.aat/research/generation_requests.jsonl`
   (`want_type` = least-represented type, liked examples,
   avoid-similar list). The researcher agent creates the
   content; the app never generates content itself.

### Content researcher (separate agent)

`content-researcher/` is a self-contained component with its
own lifecycle (`profiled → briefed → drafting → validating →
deposited | failed`, recorded in `.aat/research/`). It
profiles the project's domain (audio DSP, cloud, crypto, ML,
systems, web), reads the latest user task, and deposits packs
into `<project>/.aat/content/` — served by the app with top
priority and meant to be committed so teammates find them.
The deterministic profiler/CLI is
`backend/app/content_researcher.py` + `tools/content_researcher.py`
(no model calls, no keys); creation itself is done by an
agent following `SKILL.md`.

### Meta pipelines (parked)

`meta_client` (Muse Spark/Image) powers deterministic-cards
and project-imagery experiments in the backend. They are not
part of the current UI (principle 1/2); keep them working but
do not surface generated imagery in the pane.

## On-disk data

| Location | Contents |
| --- | --- |
| `~/.local/share/agent-aware-terminal/` | `decisions.log`, `content_state.json` (deck progress), `content_index.db` (vectors/reactions/serves), `ui_prefs.json` (theme mode + view pairing), `models/embeddinggemma2/` (~350 MB, downloaded once), `demo_project/` runtime copy |
| `<project>/.aat/content/` | Researcher-deposited packs (commit these) |
| `<project>/.aat/research/` | `domain_profile.json`, `brief.md`, `lifecycle.jsonl`, `saved_ideas.jsonl`, `generation_requests.jsonl` |
| `dist/agent-aware-terminal/games-base/` | Build-time copy of the submodule — the app reads *this*, rebuild to refresh |

## Environment variables

`AAT_PROJECT`, `AAT_CONTENT_DIR`, `AAT_THEME=dark|light` (initial theme mode; the header icon cycles auto/light/dark afterwards),
`AAT_TOUR=<sec>|manual` (review mode; statistics sandboxed),
`AAT_TOUR_TYPE=game|fact|news|idea`, `AAT_MOCKSTATE=<mode>`,
`AAT_SIM_FILE`, `AAT_OPEN_OVERLAY=1`, `AAT_APP_ID` (unique id
for parallel test instances — the app is single-instance),
`AAT_CREATOR_PROVIDER=muse|codex|custom` (who creates project content; default muse), `AAT_CREATOR_MODEL`, `AAT_CREATOR_CODEX_SANDBOX` (workspace-write default; `bypass` where Codex's own sandbox cannot start) (model id for muse/codex), `AAT_CREATOR_CMD` (full command for provider=custom — any agent CLI that takes the mission as its last argument, e.g. a DeepSeek runner; setting it alone also selects custom), `AAT_CREATOR=off` (never spawn the creator), `AAT_KEYS_FILE`, `LIQUID_MODEL`, `AAT_TURN_END_CONFIRM_S` (seconds a non-work screen verdict must persist before a screen-driven turn counts as ended; default 4), plus keys from `keys.sh`.

### Creator auto-launch

The app can start the researcher itself. The bulb button
picks a content type, shows a spinner in the pane, and spawns
the configured creator with a bounded one-item mission
(researcher skill, real project, write only inside `.aat/`);
the deposited pack is served the moment the agent exits.
Opening a project with an empty `.aat/content` gets the same
treatment once (7-day marker) when a deterministic substance
gate plus a System-1 worth vote both pass. The generic
catalogue is filler for waits with no project pack yet.

## Development workflow

```bash
cd agent-aware-terminal
./run.sh                                   # from source
./build-binary.sh                          # PyInstaller binary
.venv/bin/python -m unittest discover -s backend/tests
python3 tools/test_game.py games-base/<pack>   # per-pack gate
python3 tools/simulate.py sims/demo_run.jsonl  # state pipeline
python3 tools/check_states.py                  # GUI mode matrix
AAT_TOUR=manual AAT_TOUR_TYPE=fact ./dist/agent-aware-terminal/agent-aware-terminal
```

Hard-won operational rules (learned from incidents):

- **Rebuild + fully close the app before judging UI changes.**
  `Gtk.Application` is single-instance: a new launch just
  activates the old process. Test instances: unique
  `AAT_APP_ID=dev.meta.aat.*`; the user's own instance (default
  id) is never killed. Kill only by captured PID — never
  `pkill -f` (it matches your own shell).
- **Evidence before claims**: slice `decisions.log` by line
  count around your run; a feature that doesn't log didn't
  happen. Screenshots via X11 grab of your own test window
  only; never raise windows or inject keystrokes while the
  user is at the machine, and **never touch keyboard
  layout/IBus/gsettings input sources** (two incidents).
- `keys.sh` is auto-discovered (project root, app ancestors,
  data dir). Never print, log, or commit key values.

## Git layout

Two repositories, both on GitHub (`FastAnt`):

- `FastAnt/meta_hack` (this repo, branch `main`) — the app.
- `FastAnt/agent-aware-terminal-games` — mounted as the
  `games-base` submodule. Content work commits there first,
  then the main repo bumps the submodule pointer. Both must be
  pushed; a checkpoint tag `checkpoint-before-300-games`
  exists on both.

Repo text (code, docs, UI) is English. Commit messages state
the *why*, not just the what.
