"""Local dashboard storage: a JSON file, no external dependencies."""
from __future__ import annotations

import json
import os


class Storage:
    def __init__(self, path: str = "pulseboard-data.json"):
        self.path = path
        self._dashboards: dict[str, dict] = {}
        if os.path.isfile(path):
            try:
                self._dashboards = json.loads(open(path).read())
            except Exception:
                self._dashboards = {}

    def save_dashboard(self, dash) -> None:
        self._dashboards[dash.title] = {
            "metrics": [{"name": m.name, "value": m.value, "unit": m.unit}
                        for m in dash.metrics]
        }
        if self.path == ":memory:":
            return
        with open(self.path, "w") as fh:
            json.dump(self._dashboards, fh, ensure_ascii=False, indent=1)

    def titles(self) -> list[str]:
        return sorted(self._dashboards)
