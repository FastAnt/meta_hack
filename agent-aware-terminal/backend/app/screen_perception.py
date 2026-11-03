"""Unified screen perception for terminal agents.

One state machine for every coding agent (Muse, Codex, Claude
Code, ...): the terminal app does not parse per-agent logs to
decide what the agent is doing. System-1 looks at the terminal
screen (screenshot + text + code-measured dynamics) and this
module turns its stream of verdicts into a stable turn state.

Design notes (learned from a live incident): a working agent
waiting on the network burns no CPU and may leave the text
dump static for minutes, so neither CPU burn nor screen quiet
may close a turn. Only confirmed screen evidence opens and
closes turns here; hard process facts (agent present / gone)
enter through note_agent_gone / set_agent.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

WORK_CHOICES = {"thinking", "searching_system", "searching_web",
                "writing_code", "installing", "downloading",
                "compiling", "running_tests"}
WAIT_CHOICES = {"waiting_for_user", "asking_question"}
REST_CHOICES = {"idle_at_prompt", "finished"}


@dataclass
class Verdict:
    choice: str
    confidence: float = 0.0
    is_working: float | None = None
    needs_user: float | None = None
    is_finished: float | None = None

    @property
    def p_work(self) -> float:
        if self.is_working is not None:
            return float(self.is_working)
        if self.choice in WORK_CHOICES:
            return float(self.confidence)
        if self.choice in WAIT_CHOICES:
            return 0.1
        return 0.0

    @property
    def p_wait(self) -> float:
        if self.needs_user is not None:
            return float(self.needs_user)
        return float(self.confidence) \
            if self.choice in WAIT_CHOICES else 0.0

    @property
    def p_finish(self) -> float:
        if self.is_finished is not None:
            return float(self.is_finished)
        if self.choice == "finished":
            return float(self.confidence)
        return 0.0

    @property
    def is_rest_evidence(self) -> bool:
        # A finished screen still shows the turn's history, so
        # is_working stays noisy (0.3-0.6 live) long after the
        # end: demanding p_work < 0.3 here wedged finished
        # turns open forever. A high finish probability or an
        # explicit finished choice is rest evidence on its own;
        # a bare idle prompt needs the strict work test.
        if self.choice == "finished":
            return True
        if self.p_finish >= 0.6 and self.p_work < 0.6:
            return True
        return self.choice == "idle_at_prompt" and self.p_work < 0.3


class ScreenPerception:
    """Verdict stream -> stable turn state with hysteresis.

    States: idle, working, waiting, finished, failed.
    finished/failed are sticky outcomes: they stay until real
    new work opens the next turn. update() returns a turn edge
    event dict ({kind, turn_id, secs}) or None.
    """

    def __init__(self):
        self.agent: str | None = None
        self.state = "idle"
        self.mode_choice: str | None = None
        self.turn_id = 0
        self.turn_started_at: float | None = None
        self.belief_work = 0.0
        self.last_verdict: Verdict | None = None
        self.last_verdict_at = 0.0
        self._work_streak = 0
        self._rest_streak = 0
        self._fail_streak = 0
        self._pending_mode: str | None = None

    # -- identity -------------------------------------------------
    AGENT_FLICKER_GRACE_S = 3.0

    def set_agent(self, agent: str | None, now: float | None = None):
        """A different agent in the terminal: fresh perception."""
        if agent == self.agent:
            return None
        now = time.time() if now is None else now
        ev = None
        if self.state in ("working", "waiting"):
            if self.turn_started_at is not None and \
                    now - self.turn_started_at \
                    < self.AGENT_FLICKER_GRACE_S:
                # Detection flicker (the agent name / presence
                # blinking between snapshots seconds into a
                # turn) is not an ending: a live prime turn was
                # closed at 0 s ten times this way, arming a
                # fake DONE while its subagents still ran.
                return None
            ev = self.note_agent_gone(now)
        self.agent = agent
        if self.state not in ("finished", "failed"):
            self.state = "idle"
        else:
            self.state = "idle"  # outcome belonged to the old agent
        self.mode_choice = None
        self.belief_work = 0.0
        self._work_streak = self._rest_streak = self._fail_streak = 0
        return ev

    def note_agent_gone(self, now: float | None = None):
        """Deterministic edge: the process left mid-turn."""
        now = time.time() if now is None else now
        if self.state not in ("working", "waiting"):
            return None
        return self._close("finished", now)

    # -- main entry ----------------------------------------------
    def update(self, verdict: Verdict, now: float | None = None):
        now = time.time() if now is None else now
        self.last_verdict = verdict
        self.last_verdict_at = now
        self.belief_work = 0.5 * self.belief_work + 0.5 * verdict.p_work

        if verdict.choice in WORK_CHOICES and (
                verdict.confidence >= 0.35 or verdict.p_work >= 0.45):
            self._work_streak += 1
        else:
            self._work_streak = 0
        self._rest_streak = self._rest_streak + 1 \
            if verdict.is_rest_evidence else 0
        self._fail_streak = self._fail_streak + 1 \
            if verdict.choice == "failed" \
            and verdict.confidence >= 0.6 else 0

        if self.state in ("working", "waiting"):
            if self._fail_streak >= 2:
                return self._close("failed", now)
            if self._rest_streak >= 2:
                return self._close("finished", now)
            if self.state == "working" and verdict.p_wait >= 0.65 \
                    and verdict.choice in WAIT_CHOICES:
                self.state = "waiting"
                self.mode_choice = verdict.choice
                return None
            if self.state == "waiting" and self._work_evidence(verdict):
                self.state = "working"
            self._smooth_mode(verdict)
            return None

        # idle / finished / failed: look for real new work.
        # An empty prompt also "needs the user" (the next
        # task): only an explicit question/approval choice may
        # open a waiting turn out of idle.
        if verdict.p_wait >= 0.65 and self.state == "idle" \
                and verdict.choice in WAIT_CHOICES:
            self.state = "waiting"
            self.mode_choice = verdict.choice
            self.turn_id += 1
            self.turn_started_at = now
            return {"kind": "turn_started", "turn_id": self.turn_id,
                    "secs": 0}
        if self._work_evidence(verdict):
            self.state = "working"
            self.mode_choice = verdict.choice
            self.turn_id += 1
            self.turn_started_at = now
            self._rest_streak = self._fail_streak = 0
            return {"kind": "turn_started", "turn_id": self.turn_id,
                    "secs": 0}
        return None

    # -- internals ------------------------------------------------
    def _work_evidence(self, verdict: Verdict) -> bool:
        return verdict.p_work >= 0.6 or (
            self._work_streak >= 2 and self.belief_work >= 0.35)

    def _smooth_mode(self, verdict: Verdict):
        if verdict.choice not in WORK_CHOICES:
            return
        if verdict.confidence >= 0.8 \
                or self._pending_mode == verdict.choice:
            self.mode_choice = verdict.choice
            self._pending_mode = None
        else:
            self._pending_mode = verdict.choice

    def _close(self, outcome: str, now: float):
        secs = int(now - (self.turn_started_at or now))
        self.state = outcome
        self.turn_started_at = None
        self._work_streak = self._rest_streak = self._fail_streak = 0
        self.belief_work = 0.0
        return {"kind": "turn_failed" if outcome == "failed"
                else "turn_finished",
                "turn_id": self.turn_id, "secs": secs}
