# meta_hack — Agent-Aware Terminal

A hackathon project built for the Meta Muse hackathon: a Linux terminal
that **understands when an AI coding agent is working inside it** and turns
the waiting time into an interactive, living map of your project.

## Documentation

- **`AGENTS.md`** — agent quickstart: read order, task routing,
  commands, hard rules. Start here if you are an agent.
- **`ARCHITECTURE.md`** — full architecture: runtime pipeline,
  content system, contextual selection, data layout, git flow.

## The idea

When a coding agent (here: **Muse**) works autonomously for minutes —
reading files, editing code, running tests — a regular terminal degrades
into an opaque `Working...`. The user loses the thread, alt-tabs away, and
then pays a context-rebuild cost when the agent finishes or needs them.

**Agent-Aware Terminal keeps the user in the loop without stealing the
terminal.** It is a plain, full-power terminal (GTK + VTE — bash, vim,
tmux, ssh all work as usual) with a calm visual layer on top of it:

- the overlay **opens by itself** when the agent has been working for a
  while, and closes back to the console in one keystroke;
- it shows **what the agent is doing right now as one animated square**
  in the center of the screen — blinking when idle, glitching when
  searching the codebase, trailing when searching the web, shuffling
  when it needs you (click it and you are back in the terminal),
  flashing color while thinking, building up block by block while
  writing code;
- the surface is minimal and follows the **system theme** (dark/light)
  with grotesk typography — no generated imagery in the UI;
- the Muse Image / Muse Spark pipelines (project worlds, session
  posters, explanations, quizzes) run in the backend and return to the
  UI with the next, data-rich design iteration;
- the overlay opens only when a **System-1 decision model (Liquid AI
  d1.1)** predicts the task will run autonomously for more than ~20 s —
  and when the agent finishes, three small squares summarize the run:
  files changed (real git diff, green/red), task completeness (1–5
  score), and change safety (1–5 score).

The core product principle: **AI draws, code tells the truth.** Generated
images never carry text, numbers, or diagrams — every fact on screen
(components, connections, files, statistics) is computed deterministically
from a real index of the project. The AI provides atmosphere and
explanations; the code provides facts.

A second principle, straight from the spec: **observation is separated
from inference.** OS facts from `/proc` (`source=observed`), heuristics
(`source=heuristic`, with confidence), and true agent events from the
agent's own session log (`source=agent_event`) are never mixed.

## What is implemented

All four phases of the [spec](meta_hack/agent_aware_terminal_tz_v0_2.md)
as one working vertical slice:

1. **Terminal + overlay** — a real VTE terminal; a native GTK overlay on
   `Ctrl+Shift+O`. No web pages, no WebKit, no localhost: the app is a
   single native binary.
2. **Session awareness** — a `/proc` observer: foreground process, cwd,
   process tree, agent-CLI detection, command lifecycle.
3. **Agent awareness** — a Muse adapter that tails Muse's replayable
   JSONL session logs and normalizes them into the spec's event contract
   (state, tool, files, plan, approval waits, subagents); overlay
   auto-show with an anti-flicker threshold.
4. **Project awareness** — a project indexer (components and
   import-derived connections), deterministic fact cards, Muse Spark
   explanations + quiz, card saving, Muse Image worlds and posters
   (generated and cached; their display is the next design iteration —
   the shipped overlay is the minimal agent square).

Verified end-to-end on a live machine: a real `muse` run edited the demo
project's README and ran its tests while the overlay tracked it; Muse
Image worlds/posters and Spark cards were generated through the Meta
Model API; 9 backend unit tests pass.

## Quick start

```bash
export MODEL_API_KEY="LLM|..."        # Meta Model API key (dev.meta.ai)
agent-aware-terminal/dist/agent-aware-terminal/agent-aware-terminal
```

Or from sources: `agent-aware-terminal/run.sh`. Rebuild the binary:
`agent-aware-terminal/build-binary.sh`. Details, the 90-second demo
script, and known limitations: [agent-aware-terminal/README.md](agent-aware-terminal/README.md).

## Architecture

![Architecture](agent-aware-terminal/docs/assets/architecture.png)

Explorable diagram with links into the source:
[`.archify/architecture-agent-aware-terminal-20261031-1240/agent-aware-terminal.html`](.archify/architecture-agent-aware-terminal-20261031-1240/agent-aware-terminal.html)
(open in a browser; export to PNG/SVG from its menu).

## Repository layout

| Path | What |
|---|---|
| `agent-aware-terminal/` | The application: native GTK+VTE terminal, overlay, Muse/Meta integration |
| `agent-aware-terminal/backend/` | Python modules: Muse adapter, session observer, project indexer, cards, Meta client |
| `agent-aware-terminal/desktop/` | The native app (`native_terminal.py`) and desktop installer |
| `agent-aware-terminal/demo_project/` | Pulseboard — the toy project the agent works on in the demo |
| `meta_hack/` | The original task spec (TZ v0.2) this was built against |
| `.archify/` | The architecture diagram spec, HTML and PNG |

## Credits

Built with Meta's Muse family — **Muse** (the coding agent in the
terminal), **Muse Spark** (explanations and quiz), **Muse Image 1.0**
(project worlds and session posters) — via the Meta Model API.
