"""The built-in LLM creator harness: tool loop, write
confinement, honest API errors. Transport is faked; no network."""
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.app import llm_creator  # noqa: E402

FACT = ('GAME = {"id": "fact-x", "type": "fact", '
        '"title": "X", "body": "B"}\n')


def _scripted(replies):
    state = {"i": 0}

    def post(url, body, api_key):
        reply = replies[state["i"]]
        state["i"] += 1
        return reply

    return post


def _tool_reply(name, args, cid="c1"):
    import json
    return {"choices": [{"message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": cid, "type": "function",
                        "function": {"name": name,
                                     "arguments": json.dumps(args)}}]}}]}


def _final(text="created fact-x"):
    return {"choices": [{"message": {
        "role": "assistant", "content": text}}]}


class HarnessTests(unittest.TestCase):
    def _run(self, replies, project):
        return llm_creator.run_mission(
            "http://stub/v1", "k", "m", "make one fact",
            project, ROOT, post=_scripted(replies))

    def test_write_validate_finish(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = str(pathlib.Path(tmp) / ".aat" / "content"
                         / "fact-x.py")
            final = self._run([
                _tool_reply("write_file",
                            {"path": target, "content": FACT}),
                _tool_reply("run_validator", {"path": target}, "c2"),
                _final(),
            ], tmp)
            self.assertIn("fact-x", final)
            self.assertTrue(pathlib.Path(target).exists())

    def test_write_outside_aat_is_refused(self):
        tools = llm_creator.CreatorTools(
            pathlib.Path("/proj"), ROOT)
        out = tools.write_file("/proj/src/evil.py", "x")
        self.assertIn("refused", out)
        out = tools.write_file("notes.txt", "x")  # project root
        self.assertIn("refused", out)

    def test_read_confined_to_project_and_app(self):
        tools = llm_creator.CreatorTools(
            pathlib.Path("/proj"), ROOT)
        self.assertIn("refused", tools.read_file("/etc/passwd"))

    def test_missing_settings_raise(self):
        with self.assertRaises(RuntimeError):
            llm_creator.run_mission("", "", "", "m", "/tmp", ROOT,
                                    post=_scripted([]))

    def test_step_budget(self):
        loop = _tool_reply("list_files", {"path": "."})
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                llm_creator.run_mission(
                    "http://stub/v1", "k", "m", "x", tmp, ROOT,
                    post=_scripted([loop] * 50), max_steps=3)


class TransportErrorTests(unittest.TestCase):
    def test_http_error_surfaces(self):
        import urllib.error

        def boom(url, body, key):
            raise RuntimeError("LLM API error 429: quota")

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError) as ctx:
                llm_creator.run_mission(
                    "http://stub/v1", "k", "m", "x", tmp, ROOT,
                    post=boom)
            self.assertIn("429", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
