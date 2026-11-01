# Skill: game forge (quiz games)

Build one Python game module per `games-base/AGENTS.md`.
Project-correlated edition:

1. **Scene first.** The picture must be the subject:
   - audio → spectrogram bands, waveform, filter curve, amp chain;
   - cloud → pipeline with queues and a visible bottleneck;
   - crypto → cipher wheel, block diagram, key/lock flow;
   - waves/math → the actual wave, sum, or geometry.
   If the scene would work unchanged for another question,
   start over (see games-base/REVIEW.md).
2. **Question second.** One sentence, answerable from the
   scene + knowledge, 2–4 choices or clickable targets.
   The correct target must be reachable by clicking — the test
   tool probes a grid and will fail you otherwise.
3. **Animation with meaning**: pulses travel the pipeline,
   the wave propagates, the wheel turns. Motion illustrates
   the mechanism; decorative motion is a last resort.
4. **Answered state** shows the outcome in-scene; explanation
   teaches the point in 1–2 sentences.
5. Test loop (mandatory): `tools/test_game.py`, open previews
   in both themes, check both frames differ, iterate.
6. Name: `<domain>-<slug>-NNN.py`, id = file stem, NNN = next
   free number in `.aat/content/` for that slug family.
