# AGENTS.md — working on Agent-Aware Terminal

You are in `FastAnt/meta_hack`: a native Linux terminal
(GTK 3 + VTE, **no web stack**) that visualizes what a coding
agent is doing and serves contextual mini-content while the
user waits. Full picture: **`ARCHITECTURE.md`** (root). Read
it before your first change.

## Read order

1. This file (rules + routing).
2. `ARCHITECTURE.md` — architecture, data flows, invariants.
3. Your track's guide:
   - App code → `agent-aware-terminal/README.md`, then the code.
   - Games/facts → `agent-aware-terminal/games-base/AGENTS.md`
     (+ `FACTS.md` for facts, `FACTS_REVIEW.md` for the visual
     rules, `REVIEW.md` for game quality verdicts).
   - Project content (researcher) →
     `agent-aware-terminal/content-researcher/README.md` and
     `SKILL.md`. You write **only** into `<project>/.aat/`.

## Where things live

- App (GUI + orchestration): `agent-aware-terminal/desktop/native_terminal.py`
- Backend services: `agent-aware-terminal/backend/app/`
  (`muse_adapter`, `session_observer`, `liquid_client`,
  `content_engine`, `content_selector`, `embeddings`,
  `vector_store`, `content_researcher`, …)
- Tests: `agent-aware-terminal/backend/tests/`
- Tools: `agent-aware-terminal/tools/` (`test_game.py`,
  `simulate.py`, `check_states.py`, `content_researcher.py`)
- Content catalogue: `agent-aware-terminal/games-base/`
  (**separate git repo**, submodule — commit there, then bump
  the pointer here; push both)
- Secrets: `agent-aware-terminal/keys.sh` (git-ignored,
  auto-loaded). Never print, log, or commit key values.

## Commands

```bash
cd agent-aware-terminal
./run.sh                                            # dev run
./build-binary.sh                                   # release binary
.venv/bin/python -m unittest discover -s backend/tests   # unit suite
python3 tools/test_game.py <pack-or-dir>            # content gate
python3 tools/simulate.py sims/demo_run.jsonl       # state pipeline
AAT_TOUR=manual AAT_TOUR_TYPE=fact ./dist/agent-aware-terminal/agent-aware-terminal  # browse content
```

## Hard rules

- **No web anything** in the product (no WebKit/HTML/servers).
- **English** for all repo text, code, commits.
- **Code tells the truth**: facts/measurements are computed
  (log parsing, fs diffs); models (Liquid d1.1, Meta) only
  judge/pick and never invent displayed numbers. Replayed
  history never produces live DONE/scores.
- **Content**: stable pack ids forever; every pack declares
  `"type"` (`game|fact|news|idea`); one fact = one bespoke
  visual (reused scene skeletons fail the tests); games are
  single-file Python modules tested with `test_game.py`
  (previews *looked at*, both themes) before commit.
- **UI**: system theme, Inter, monochrome; calm motion.
- **Git**: content commits land in the games repo first, app
  bumps the submodule; push both repos.
- **Ops safety**: the app is single-instance — fully close it
  before re-testing, use unique `AAT_APP_ID=dev.meta.aat.*`
  for test instances, kill only by captured PID, never touch
  the user's running instance, keyboard layout, or IBus.
  Verify in the shipped binary + `decisions.log`, not from
  source alone.

## The two-agent split

Coding agents (terminal) never write games. The content
researcher (separate lifecycle, `content-researcher/`) never
edits production code and writes only `<project>/.aat/`.
Generation requests from the app queue in
`.aat/research/generation_requests.jsonl` — the researcher's
work list.
