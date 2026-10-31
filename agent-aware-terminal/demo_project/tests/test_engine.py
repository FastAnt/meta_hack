"""Engine tests: metrics summary and the empty dashboard."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.engine import Engine
from storage.db import Storage


def test_summary(tmp_path=None):
    engine = Engine(Storage(path=":memory:"))
    dash = engine.build_dashboard("t", {"a": 10, "b": 20})
    s = engine.summary(dash)
    assert s == {"count": 2, "avg": 15.0, "max": 20}


def test_empty_summary():
    engine = Engine(Storage(path=":memory:"))
    from core.models import Dashboard
    assert engine.summary(Dashboard("empty"))["count"] == 0
