# Content Researcher — a separate agent component

Agent-Aware Terminal has two agents with two different jobs:

| | Coding agent | **Content Researcher (this component)** |
| --- | --- | --- |
| Lives | in the terminal, writes code | separate session (e.g. Muse), never touches code |
| Input | user tasks | the project's domain + the user's **latest task** |
| Output | source changes | mini games and fact cards **about this project** |
| Skills | its own | `SKILL.md` + `skills/` here |
| Lifecycle | agent states | profiled → briefed → drafting → validating → deposited (or failed) |
| Writes to | the project tree | only `<project>/.aat/` |

An audio engine gets spectrogram puzzles, amplifier-chain
questions and wave math. A cloud app gets bottleneck and
security questions. A crypto library gets cipher games. Content
correlates with what the developer is doing *right now* because
the latest user task is the strongest signal in the profile.

## Where things land (inside the user's project)

```
<project>/.aat/
  content/     # games (*.py) and fact cards (*.json) — the app serves these
  research/
    domain_profile.json   # detected domains + evidence
    brief.md              # the assignment for the content agent
    lifecycle.json        # current lifecycle state
    lifecycle.jsonl       # full lifecycle history
```

`.aat/` is hidden but meant to be committed: other developers
on the project may enjoy (and review) the games later.

## Flow

1. **Profile** (deterministic, no model):
   `python3 tools/content_researcher.py profile --project <p> --task "<latest user request>"`
   writes the brief.
2. **Create** (the separate agent, following `SKILL.md`): reads
   the brief, crafts one game or fact card into `.aat/content/`.
3. **Validate**: `tools/test_game.py` on the new file, look at
   the previews, then
   `python3 tools/content_researcher.py validate --project <p>`
   and the lifecycle is marked `deposited`.
4. The terminal app loads `.aat/content/` with top priority, so
   the new game appears in the pane for that project.

## Deliberately not here (yet)

- Automatically installing/launching the creator agent and
  injecting its keys. The operator runs the agent; keys come
  from the environment (`keys.sh` is the operator's file — this
  component never reads, prints or stores key values).
- Writing production code. Ever. The researcher's write access
  is `.aat/` by contract, and its skills say so.

Code: `backend/app/content_researcher.py` (profiler, lifecycle),
`tools/content_researcher.py` (CLI). Skills: `SKILL.md` (main),
`skills/domain-profiler.md`, `skills/game-forge.md`,
`skills/fact-forge.md`. Templates in `templates/`.
