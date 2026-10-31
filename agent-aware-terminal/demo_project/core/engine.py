"""Pulseboard engine: builds a metrics summary and stores it."""
from __future__ import annotations

from core.models import Dashboard, Metric
from storage.db import Storage


class Engine:
    def __init__(self, storage: Storage):
        self.storage = storage

    def build_dashboard(self, title: str, raw: dict[str, float]) -> Dashboard:
        dash = Dashboard(title=title)
        for name, value in raw.items():
            dash.add(Metric(name=name, value=value))
        self.storage.save_dashboard(dash)
        return dash

    def summary(self, dash: Dashboard) -> dict[str, float]:
        if not dash.metrics:
            return {"count": 0, "avg": 0.0, "max": 0.0}
        values = [m.value for m in dash.metrics]
        return {
            "count": len(values),
            "avg": sum(values) / len(values),
            "max": max(values),
        }
