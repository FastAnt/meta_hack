"""Template for a project-correlated quiz game.

Copy into <project>/.aat/content/<domain>-<slug>-NNN.py and
replace everything. Contract: games-base/AGENTS.md. The scene
must depict the subject of the question; animation comes from
view.t; colors only from view.colors.
"""
import math

GAME = {
    "id": "domain-slug-001",          # == file stem, never changes
    "kind": "quiz",
    "kicker": "PROJECT QUIZ",
    "topic": "Detected domain label",
    "title": "Short title",
    "question": "One sentence the scene makes you think about?",
    "choices": [
        {"id": "right", "label": "The correct mechanism"},
        {"id": "wrong", "label": "A plausible misconception"},
    ],
    "answer": "right",
    "explanation": "One or two sentences that teach the point.",
}


def paint(cr, view):
    # Draw the subject itself. This placeholder breathes a ring;
    # replace it with the real mechanism (spectrogram, pipeline,
    # cipher wheel, wave...).
    cx, cy = view.w / 2, view.h / 2
    r = min(view.w, view.h) * 0.22 * (1 + 0.03 * math.sin(view.t))
    cr.arc(cx, cy, r, 0, 6.2832)
    cr.set_source_rgba(*view.colors["square"], 0.9)
    cr.set_line_width(1.6)
    cr.stroke()


def target_at(view, x, y):
    # Optional: map scene clicks to target ids. Must be able to
    # return GAME["answer"] somewhere (the tester probes a grid).
    return None
