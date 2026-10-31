"""Domain models: metrics, dashboards and status events."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Metric:
    name: str
    value: float
    unit: str = ""


@dataclass
class Dashboard:
    title: str
    metrics: list[Metric] = field(default_factory=list)

    def add(self, metric: Metric) -> None:
        self.metrics.append(metric)
