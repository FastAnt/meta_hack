# Skill: fact forge (interesting fact cards)

Canonical guide: `games-base/FACTS.md` (what makes a fact
interesting, sourcing rules, the 500-fact campaign). This
skill is the project-correlated short version.

A fact card is unscored companion content: one surprising,
true, project-relevant fact. Use it when a fact teaches more
than a quiz would, or as the deliverable for a weak-signal
brief.

Format (JSON, `"type": "fact"`). Two sibling types share
this shape: `"type": "news"` (adds a required http(s) `link`;
the pane shows Skip/Next + Open link) and `"type": "idea"`
(adds `action_hint`; the pane shows a Save button that files
the idea into `.aat/research/saved_ideas.jsonl` for a future
agent). Templates: `content-researcher/templates/`.

```json
{
  "id": "audio-fact-nyquist-001",
  "type": "fact",
  "kicker": "PROJECT FACT",
  "topic": "Audio & DSP",
  "title": "Why 44.1 kHz?",
  "body": "CD audio samples at 44,100 Hz because early digital "
          "masters were stored on video tape: 3 samples per "
          "video line × 245 lines × 60 fields. The rate is a "
          "fossil of 1970s television.",
  "source_hint": "Sony PCM-1600 / Red Book history"
}
```

Rules:
- One fact per card, 2–4 sentences, with the *why* — a bare
  trivia statement is not a fact card.
- It must connect to the project's detected domain or the
  latest task, not to the world in general.
- `source_hint` names where the claim can be checked (a doc,
  standard, paper, or file in the project). No hint, no card.
- Still test it: `tools/test_game.py <file>` (cards validate
  and must not score), and keep ids stable forever.
