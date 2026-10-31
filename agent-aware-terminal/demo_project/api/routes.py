"""HTTP-ish layer: turns requests into engine calls and back."""
from __future__ import annotations

from core.engine import Engine
from core.models import Dashboard


class Routes:
    def __init__(self, engine: Engine):
        self.engine = engine

    def post_metrics(self, title: str, raw: dict[str, float]) -> dict:
        dash = self.engine.build_dashboard(title, raw)
        return {"dashboard": dash.title, "summary": self.engine.summary(dash)}

    def get_dashboard(self, dash: Dashboard) -> dict:
        return {"dashboard": dash.title, "summary": self.engine.summary(dash)}
