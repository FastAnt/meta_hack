"""Unit tests: project indexer, Muse adapter normalisation, cards.

Run: .venv/bin/python -m unittest discover -s backend/tests -v
"""
import glob
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.content import deterministic_cards          # noqa: E402
from app.muse_adapter import MuseAdapter             # noqa: E402
from app.project_indexer import component_for_file, index_project  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEMO = os.path.join(ROOT, "demo_project")


class TestIndexer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = index_project(DEMO)

    def test_components_found(self):
        names = {c["name"] for c in self.project["components"]}
        self.assertTrue({"core", "storage", "api", "ui"} <= names)

    def test_connections_from_imports(self):
        comps = {c["name"]: c for c in self.project["components"]}
        by_id = {c["id"]: c["name"] for c in self.project["components"]}
        core_links = {by_id[i] for i in comps["core"]["connections"]}
        self.assertIn("storage", core_links)
        api_links = {by_id[i] for i in comps["api"]["connections"]}
        self.assertIn("core", api_links)

    def test_file_to_component(self):
        comp = component_for_file(self.project, "core/engine.py")
        self.assertIsNotNone(comp)
        self.assertEqual(comp["name"], "core")
        comp2 = component_for_file(
            self.project, os.path.join(DEMO, "ui", "cli.py"))
        self.assertEqual(comp2["name"], "ui")

    def test_cards_are_deterministic(self):
        cards = deterministic_cards(self.project)
        self.assertEqual(cards[0]["kind"], "overview")
        self.assertTrue(any(c["kind"] == "component" for c in cards))
        self.assertTrue(all(c["source"] == "project_indexer" for c in cards))


def _rec(payload_type, payload):
    return {"record_type": "event", "payload_type": payload_type,
            "payload": payload, "stream": {"kind": "session", "id": "s1"}}


class TestMuseAdapter(unittest.TestCase):
    def _events_for(self, records):
        events = []
        adapter = MuseAdapter(events.append, lambda: DEMO,
                              sessions_root="/nonexistent")
        for rec in records:
            adapter._handle_record(rec)
        return events

    def test_tool_batch_event(self):
        evs = self._events_for([_rec("tool_batch.effect.started", {
            "record": {"tool_name": "bash"}, "kind": "tool_batch_effect"})])
        self.assertEqual(evs[0]["state"], "tool_running")
        self.assertEqual(evs[0]["tool"], "bash")
        self.assertEqual(evs[0]["source"], "agent_event")

    def test_tool_call_files_extracted(self):
        evs = self._events_for([_rec("runtime.session", {"event": {
            "kind": "assistant_tool_calls_committed",
            "tool_calls": [{"name": "file_edit",
                            "args": '{"path": "core/engine.py"}'}]}})])
        self.assertEqual(evs[0]["state"], "tool_running")
        self.assertIn("core/engine.py", evs[0]["files"])

    def test_todo_plan(self):
        evs = self._events_for([_rec("runtime.session", {"event": {
            "kind": "todo_snapshot_updated",
            "items": [{"status": "in_progress", "text": "Step one"},
                      {"status": "pending", "text": "Step two"}]}})])
        self.assertEqual(evs[0]["state"], "planning")
        self.assertEqual(len(evs[0]["plan"]), 2)

    def test_user_intent(self):
        evs = self._events_for([_rec("runtime.user_intent.accepted", {
            "model_messages": [{"content": [
                {"kind": "text", "text": "Fix the bug in storage"}]}]})])
        self.assertEqual(evs[0]["state"], "planning")
        self.assertIn("Fix the bug", evs[0]["activity"])

    def test_replay_real_session_if_present(self):
        files = sorted(
            glob.glob(os.path.expanduser(
                "~/.local/share/muse/sessions/*/*/*/*/session.jsonl")),
            key=os.path.getmtime, reverse=True)
        if not files:
            self.skipTest("no muse sessions on this machine")
        adapter = MuseAdapter(lambda ev: None, lambda: DEMO,
                              sessions_root="/nonexistent")
        events = adapter.replay_file(files[0], max_events=60)
        self.assertTrue(events)
        self.assertTrue(all("state" in e and "source" in e for e in events))


if __name__ == "__main__":
    unittest.main()
