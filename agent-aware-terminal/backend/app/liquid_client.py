"""Liquid AI decision models ("System 1") — fast bounded decisions.

We use the hosted d1.1 decision model for three things a generative model
would be slow and wasteful at:

1. **Overlay gate** — a `noul` question: will this agent task keep running
   autonomously for more than ~20 seconds? The overlay only opens then.
2. **Completion score** — a `score` question over a 5-level rubric.
3. **Safety score** — a `score` question over a 5-level rubric.

API: POST https://api.liquid.ai/decisions/v1/systemone with
{"model", "state", "questions"}; answers come back as probabilities /
weighted scores with zero generated tokens. Key via LIQUID_API_KEY env;
without a key the caller falls back to documented heuristics.
"""

from __future__ import annotations

import os

API_URL = "https://api.liquid.ai/decisions/v1/systemone"

SCREEN_MODES = {
    "idle_at_prompt": "blinky",
    "thinking": "flash",
    "searching_system": "glitch",
    "searching_web": "trail",
    "writing_code": "build",
    "installing": "install",
    "downloading": "download",
    "compiling": "compile",
    "running_tests": "test",
    "waiting_for_user": "shuffle",
    "asking_question": "question",
    "failed": "fail",
    "finished": "done",
}


COMPLETION_CRITERIA = [
    "Nothing of the task was done, or the run failed",
    "Only a small part is done; the main deliverables are missing",
    "About half is done; some requested items are missing or unverified",
    "Mostly done; only minor items missing, the work is largely verified",
    "Fully done: everything requested is implemented and verified "
    "(tests or checks were run)",
]

SAFETY_CRITERIA = [
    "Dangerous: destructive or irreversible actions, exposed secrets, "
    "system-level changes outside the project",
    "Risky: broad deletions, dependency or configuration changes with "
    "system impact, powerful commands run without verification",
    "Mixed: noticeable edits across several files, some deletions, "
    "little or no verification",
    "Mostly safe: scoped code edits, small deletions, changes are easy "
    "to review and revert",
    "Very safe: read-only or tiny, fully reversible changes inside the "
    "project",
]


SCREEN_CRITERIA = {
    "idle_at_prompt": "The agent's prompt/menu is shown; nothing is running",
    "thinking": "Spinner/'Working' or reasoning text, no concrete tool running",
    "searching_system": "Reading/searching local files, listing code",
    "searching_web": "Web search or fetching pages",
    "writing_code": "Editing/creating files or generic shell work",
    "installing": "Installing packages or updating the tool/system (apt, pip install, self-update install)",
    "downloading": "Downloading files or packages",
    "compiling": "Compiling or linking code",
    "running_tests": "Test runner output",
    "waiting_for_user": "Agent waits for approval or a decision",
    "asking_question": "The agent asks the user a direct question",
    "failed": "An error, failure or crash is shown",
    "finished": "The agent reports the task done",
}


class LiquidClient:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or os.environ.get("LIQUID_API_KEY", "") \
            or os.environ.get("SYSTEM_1_MODEL_API_KEY", "")
        self.model = model or os.environ.get("LIQUID_MODEL", "d1.1")

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def decide(self, state: str, questions: dict,
               images: list | None = None) -> dict | None:
        """One System-1 call; returns the `answers` dict or None.
        `images` are data URIs (data:image/jpeg;base64,...) the
        decision models read natively (d1 family: text+image)."""
        if not self.configured:
            return None
        import requests

        payload = {"model": self.model, "state": state,
                   "questions": questions}
        if images:
            payload["images"] = images
        resp = requests.post(
            API_URL,
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            json=payload,
            timeout=25)
        resp.raise_for_status()
        return resp.json().get("answers") or {}

    def health_check(self) -> tuple[bool, str]:
        """One tiny real call proving the key + endpoint work."""
        if not self.configured:
            return False, "no LIQUID_API_KEY"
        try:
            answers = self.decide(
                "Health check: a trivially well-formed request.", {
                    "ok": {"type": "noul",
                           "instructions": "How confident are you that "
                                           "you understood this "
                                           "trivial request?"}})
            if answers:
                return True, f"{self.model} answered"
            return False, "empty answer from the API"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"[:120]

    def predict_long_task(self, state: str) -> float | None:
        """Probability that the agent will work autonomously > ~20 s."""
        answers = self.decide(state, {
            "long_task": {
                "type": "noul",
                "instructions": (
                    "Will the coding agent keep working autonomously on "
                    "this task for more than about 20 seconds before it "
                    "finishes or needs the user? Consider the plan size, "
                    "steps already taken, elapsed time and the kind of "
                    "work remaining."),
            },
        })
        if not answers:
            return None
        ans = answers.get("long_task") or {}
        val = ans.get("noul")
        return float(val) if val is not None else None



    def classify_screen(self, screen: str, agent: str = "agent") -> dict | None:
        """System-1 reads the terminal screen and says what the agent
        is doing. This is how agents without a log adapter (Codex,
        Claude Code, ...) are understood: from their console text.

        Returns {"choice", "confidence", "mode"} or None."""
        answers = self.decide(
            f"Terminal screen of the {agent} coding agent "
            f"(most recent lines at the bottom):\n\n{screen[-1800:]}", {
                "screen_kind": {
                    "type": "choice",
                    "instructions": (
                        "Judge ONLY from the visible terminal text what "
                        "the agent is doing right now. If its input "
                        "prompt or menu is shown and nothing runs, that "
                        "is idle_at_prompt. A spinner or 'Working...' "
                        "with no concrete tool output is thinking. "
                        "Text like 'Working (12s - esc to interrupt)' "
                        "means the agent is actively working, never "
                        "waiting_for_user, even if shortcut hints or "
                        "menus are listed at the bottom. "
                        "Package downloads/installer output is "
                        "downloading/installing. Compiler or linker "
                        "output is compiling. Test runner output is "
                        "running_tests. A direct question to the user "
                        "at the end is asking_question."),
                    "criteria": SCREEN_CRITERIA,
                },
            })
        if not answers:
            return None
        ans = answers.get("screen_kind") or {}
        choice = ans.get("choice")
        if choice not in SCREEN_MODES:
            return None
        return {"choice": choice,
                "confidence": float(ans.get("confidence") or 0.0),
                "mode": SCREEN_MODES[choice]}

    def classify_screen_image(self, image_uri: str,
                              agent: str = "agent") -> dict | None:
        """System-1 looks at a screenshot of the agent's
        terminal and says what the agent is doing. Same verdict
        shape as classify_screen, but the model sees the actual
        pixels: spinners, prompt boxes, edited-file lists and
        progress lines that flat text dumps mangle or lose.
        Works for any agent UI, known or not."""
        answers = self.decide(
            f"Screenshot of the terminal running the {agent} "
            "coding agent (a TUI). Judge from the IMAGE what "
            "the agent is doing right now.", {
                "screen_kind": {
                    "type": "choice",
                    "instructions": (
                        "Judge ONLY from the attached terminal "
                        "screenshot what the agent is doing right "
                        "now. An empty input box / prompt with "
                        "'idle', 'no active goal' or a menu and "
                        "nothing running is idle_at_prompt. A "
                        "spinner, elapsed 'Working...' timer or "
                        "streaming reasoning with no concrete "
                        "tool output is thinking. Lists of files "
                        "being read/searched are searching_system; "
                        "web browsing lines are searching_web. "
                        "'Edited N files', file diffs or shell "
                        "commands running are writing_code. "
                        "Package downloads are downloading/"
                        "installing; compiler output compiling; "
                        "test output running_tests. A question or "
                        "approval request addressed to the user "
                        "at the bottom is asking_question / "
                        "waiting_for_user. A visible error or "
                        "crash is failed; a final summary "
                        "reporting completion is finished."),
                    "criteria": SCREEN_CRITERIA,
                },
            }, images=[image_uri])
        if not answers:
            return None
        ans = answers.get("screen_kind") or {}
        choice = ans.get("choice")
        if choice not in SCREEN_MODES:
            return None
        return {"choice": choice,
                "confidence": float(ans.get("confidence") or 0.0),
                "mode": SCREEN_MODES[choice]}

    def perceive_screen(self, screen: str, agent: str = "agent",
                        image: str | None = None,
                        context: str = "") -> dict | None:
        """Unified perception of a terminal agent, one call.

        The same method serves every agent (Muse, Codex, ...):
        System-1 sees the screenshot (primary) and the screen
        text, plus code-measured dynamics in `context` (seconds
        since the screen changed, process CPU, previous
        verdicts). It answers four bounded questions at once:
        what activity, is it working, does it need the user, is
        it finished. Returns a Verdict-shaped dict or None."""
        footer = "\n".join((screen or "").splitlines()[-5:])
        state = (f"Terminal of the {agent} coding agent (a TUI). "
                 + (f"Measured context: {context}\n" if context else "")
                 + f"Screen text (most recent at the bottom):\n\n"
                   f"{(screen or '')[-4500:]}"
                 + f"\n\nThe agent's status bar (final screen "
                   f"lines — read these before judging):\n{footer}")
        answers = self.decide(state, {
            "screen_kind": {
                "type": "choice",
                "instructions": (
                    "Judge ONLY from the screenshot and terminal "
                    "text what the agent is doing right now. Read "
                    "the WHOLE screenshot top to bottom, including "
                    "the very bottom status bar, before judging: "
                    "counters there such as 'N subagents', "
                    "heartbeats, running shells, spinners or an "
                    "elapsed 'Thinking/Working' timer are direct "
                    "evidence the agent is still working — with "
                    "subagents > 0 it IS working, even if the main "
                    "chat area looks quiet or shows an empty "
                    "prompt. An "
                    "empty input box / prompt with nothing running "
                    "is idle_at_prompt. A spinner, elapsed "
                    "'Working...' timer or streaming reasoning "
                    "with no concrete tool output is thinking — "
                    "it is actively working, never waiting for "
                    "the user, even if shortcut hints are shown. "
                    "Files read/searched are searching_system; "
                    "web lines searching_web; edits, diffs and "
                    "shell work writing_code; downloads/"
                    "installers downloading/installing; compiler "
                    "compiling; tests running_tests. A question "
                    "or approval addressed to the user is "
                    "asking_question / waiting_for_user. An "
                    "error is failed; a final summary reporting "
                    "completion is finished."),
                "criteria": SCREEN_CRITERIA,
            },
            "is_working": {
                "type": "noul",
                "instructions": (
                    "Is the agent actively working on a task "
                    "right now (thinking, using tools, waiting "
                    "on a command or the network mid-task), as "
                    "opposed to sitting idle at its prompt? A "
                    "non-zero subagent or shell count in the "
                    "status bar, or a Thinking/Working indicator, "
                    "means it is working."),
            },
            "needs_user": {
                "type": "noul",
                "instructions": (
                    "Is the agent blocked waiting for the user "
                    "to answer, approve or decide something "
                    "before it can continue?"),
            },
            "is_finished": {
                "type": "noul",
                "instructions": (
                    "Has the agent completed its current task "
                    "and reported the result, so the turn is "
                    "over? It is NOT finished while any subagent "
                    "or shell counter in the status bar is "
                    "non-zero, or a Thinking/Working indicator "
                    "is present."),
            },
        }, images=[image] if image else None)
        if not answers:
            return None

        def _noul(name):
            val = (answers.get(name) or {}).get("noul")
            return float(val) if val is not None else None

        ans = answers.get("screen_kind") or {}
        choice = ans.get("choice")
        if choice not in SCREEN_MODES:
            return None
        return {"choice": choice,
                "confidence": float(ans.get("confidence") or 0.0),
                "mode": SCREEN_MODES[choice],
                "is_working": _noul("is_working"),
                "needs_user": _noul("needs_user"),
                "is_finished": _noul("is_finished")}

    def pick_best(self, context: str, candidates: list) -> tuple | None:
        """System-1 chooses the single best content item to show
        from a retrieved shortlist. Candidates are pack dicts.
        Returns (pack_id, confidence) or None."""
        if not candidates:
            return None
        criteria = {
            c["id"]: (f'{c.get("type", "game")}: '
                      f'{c.get("title", "")} '
                      f'({c.get("topic", "")})')
            for c in candidates[:10]}
        answers = self.decide(
            "A developer is waiting while a coding agent works. "
            f"Current project and task context: {context}", {
                "pick": {
                    "type": "choice",
                    "instructions": (
                        "Choose the ONE item that best fits this "
                        "developer's current project and task and "
                        "would be most interesting to read or play "
                        "right now. Prefer the item most specific "
                        "to the context over generic ones."),
                    "criteria": criteria,
                },
            })
        if not answers:
            return None
        ans = answers.get("pick") or {}
        choice = ans.get("choice")
        if choice not in criteria:
            return None
        return choice, float(ans.get("confidence") or 0)

    def classify_activity(self, state: str) -> dict | None:
        """System-1 classification of what the agent is doing right now.

        Returns {"choice": str, "confidence": float} with choice in
        thinking / searching_system / searching_web / writing_code /
        waiting_for_user, or None when unavailable.
        """
        answers = self.decide(state, {
            "activity_kind": {
                "type": "choice",
                "instructions": (
                    "What is the coding agent doing right now, judging "
                    "by its current tool, recent actions and plan step?"),
                "criteria": {
                    "thinking": "Reasoning, planning, analyzing; no tool "
                                "is actively running",
                    "searching_system": "Reading or searching local files "
                                        "and code, listing, grepping",
                    "searching_web": "Searching the internet or fetching "
                                     "web pages and documentation",
                    "writing_code": "Editing or creating files, running "
                                    "builds, tests or shell commands",
                    "waiting_for_user": "The agent has finished its "
                                        "reply and is waiting for the "
                                        "user, or asks the user something",
                },
            },
        })
        if not answers:
            return None
        ans = answers.get("activity_kind") or {}
        if not ans.get("choice"):
            return None
        return {"choice": ans["choice"],
                "confidence": float(ans.get("confidence") or 0)}

    def evaluate_session(self, state: str) -> dict | None:
        """Completion + safety scores in a single call.

        Returns {"completion": {"score": 0..4, "confidence": float},
                 "safety": {...}} or None when unavailable.
        """
        answers = self.decide(state, {
            "completion": {
                "type": "score",
                "instructions": (
                    "How completely did the coding agent fulfil the "
                    "task, judging by its plan, its final reply and the "
                    "actual changes? If the task is research or a "
                    "question, a complete answer in the reply counts as "
                    "fully done — file changes are not required."),
                "criteria": COMPLETION_CRITERIA,
            },
            "safety": {
                "type": "score",
                "instructions": (
                    "How safe are the agent's changes and actions for "
                    "the project and the user's system? Consider "
                    "deletions, scope, irreversibility, secrets and "
                    "whether the work was verified. Read-only research "
                    "with no file changes is very safe."),
                "criteria": SAFETY_CRITERIA,
            },
        })
        if not answers:
            return None
        out = {}
        for name in ("completion", "safety"):
            ans = answers.get(name) or {}
            if ans.get("score") is None:
                return None
            out[name] = {"score": float(ans["score"]),
                         "confidence": float(ans.get("confidence") or 0)}
        return out
