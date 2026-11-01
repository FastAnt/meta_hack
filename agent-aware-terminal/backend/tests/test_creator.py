"""Creator-agent launch helpers: mission text and the
project-substance pre-gate for boot generation."""
import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _native():
    spec = importlib.util.spec_from_file_location(
        "native_terminal", ROOT / "desktop" / "native_terminal.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class CreatorPromptTests(unittest.TestCase):
    def test_prompt_bounds_the_agent(self):
        text = _native().creator_task_text(
            "/app", "/proj", "fact", task="fix the parser")
        for needle in ("/app/content-researcher/SKILL.md",
                       "/proj/.aat/content/", '"fact"',
                       "fix the parser", "test_game.py",
                       "write ONLY inside"):
            self.assertIn(needle, text)

    def test_game_prompt_points_at_the_sdk(self):
        text = _native().creator_task_text("/app", "/p", "game")
        self.assertIn("/app/games-base/AGENTS.md", text)


class FailureReasonTests(unittest.TestCase):
    def test_quota_line_is_the_reason(self):
        lines = ["muse: workspace root: /p",
                 "agent loop failed: model failed: API error "
                 "429: Subscription quota exhausted. Your usage "
                 "window resets at 2026-11-02T00:00:00Z."]
        reason = _native().creator_failure_reason(lines)
        self.assertIn("quota exhausted", reason)

    def test_no_error_no_reason(self):
        self.assertEqual(
            _native().creator_failure_reason(["all good"]), "")


class SubstanceTests(unittest.TestCase):
    def test_small_project_is_not_substantial(self):
        ok, _ = _native().project_substance(
            {"domains": [{"score": 3}, {"score": 2}]})
        self.assertFalse(ok)

    def test_real_project_is_substantial(self):
        ok, _ = _native().project_substance(
            {"domains": [{"score": 18}, {"score": 7},
                         {"score": 4}]})
        self.assertTrue(ok)

    def test_no_domains(self):
        ok, _ = _native().project_substance({"domains": []})
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()


class ProjectDirTests(unittest.TestCase):
    def test_engine_follows_project_dir(self):
        import tempfile
        from backend.app import content_engine as ce
        with tempfile.TemporaryDirectory() as a, \
                tempfile.TemporaryDirectory() as b:
            pa = pathlib.Path(a) / ".aat" / "content"
            pb = pathlib.Path(b) / ".aat" / "content"
            pa.mkdir(parents=True)
            pb.mkdir(parents=True)
            (pb / "fact-one.json").write_text(
                '{"id": "fact-one", "type": "fact", '
                '"title": "T", "body": "B"}', encoding="utf-8")
            eng = ce.ContentEngine([pa], state_path=None)
            self.assertEqual(eng.packs, [])
            self.assertTrue(eng.set_project_dir(pb))
            self.assertEqual([p["id"] for p in eng.packs],
                             ["fact-one"])
            self.assertFalse(eng.set_project_dir(pb))


class ProviderCommandTests(unittest.TestCase):
    def cmd(self, env):
        return _native().creator_command(env)

    def test_default_is_muse(self):
        self.assertEqual(self.cmd({}),
                         ["muse", "exec", "--yolo"])

    def test_muse_model(self):
        self.assertEqual(
            self.cmd({"AAT_CREATOR_MODEL": "opus-x"}),
            ["muse", "exec", "--yolo", "--model", "opus-x"])

    def test_codex_with_model(self):
        self.assertEqual(
            self.cmd({"AAT_CREATOR_PROVIDER": "codex",
                      "AAT_CREATOR_MODEL": "gpt-x"}),
            ["codex", "exec", "--skip-git-repo-check",
             "-s", "workspace-write", "-m", "gpt-x"])

    def test_codex_bypass_sandbox(self):
        self.assertEqual(
            self.cmd({"AAT_CREATOR_PROVIDER": "codex",
                      "AAT_CREATOR_CODEX_SANDBOX": "bypass"}),
            ["codex", "exec", "--skip-git-repo-check",
             "--dangerously-bypass-approvals-and-sandbox"])

    def test_custom_verbatim(self):
        self.assertEqual(
            self.cmd({"AAT_CREATOR_PROVIDER": "custom",
                      "AAT_CREATOR_CMD": "deepseek exec --yolo"}),
            ["deepseek", "exec", "--yolo"])

    def test_legacy_cmd_selects_custom(self):
        self.assertEqual(
            self.cmd({"AAT_CREATOR_CMD": "my-agent run"}),
            ["my-agent", "run"])

    def test_custom_without_cmd_is_an_error(self):
        with self.assertRaises(ValueError):
            self.cmd({"AAT_CREATOR_PROVIDER": "custom"})

    def test_unknown_provider_is_an_error(self):
        with self.assertRaises(ValueError):
            self.cmd({"AAT_CREATOR_PROVIDER": "nope"})


class KiloProviderTests(unittest.TestCase):
    def test_kilo_preset(self):
        self.assertEqual(
            _native().creator_command(
                {"AAT_CREATOR_PROVIDER": "kilo"}),
            ["kilo", "run"])

    def test_kilo_model(self):
        self.assertEqual(
            _native().creator_command(
                {"AAT_CREATOR_PROVIDER": "kilo",
                 "AAT_CREATOR_MODEL": "anthropic/claude-x"}),
            ["kilo", "run", "-m", "anthropic/claude-x"])

    def test_llm_is_not_an_argv_provider(self):
        with self.assertRaises(ValueError):
            _native().creator_command(
                {"AAT_CREATOR_PROVIDER": "llm"})


class AgentBinaryTests(unittest.TestCase):
    def test_kilo_is_built_in(self):
        from backend.app import session_observer as so
        self.assertIn("kilo", so.agent_binaries())

    def test_env_extras_merge(self):
        import os
        from backend.app import session_observer as so
        old = os.environ.get("AAT_AGENT_PROCS")
        os.environ["AAT_AGENT_PROCS"] = "aider, goose"
        try:
            names = so.agent_binaries()
        finally:
            if old is None:
                os.environ.pop("AAT_AGENT_PROCS", None)
            else:
                os.environ["AAT_AGENT_PROCS"] = old
        self.assertIn("aider", names)
        self.assertIn("goose", names)
