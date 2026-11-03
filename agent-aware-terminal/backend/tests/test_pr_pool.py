"""Reset must be one click, and the PRs category must serve
real pull requests as cards.

Part 1 is engine-level (the deck revives after reset_history —
the settings button used to need a secret second click and
even then left the end card on screen).

Part 2: the PR pool reads the project's GitHub remote with gh
and measures the cards (description, files, discussion). The
visual is a diagram of the components the PR touches.
"""
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from backend.app import content_engine as ce  # noqa: E402
from backend.app import pr_pool  # noqa: E402


RAW_PR = {
    "number": 7, "title": "Fix the parser cache",
    "url": "https://github.com/x/y/pull/7",
    "author": {"login": "ann"}, "body": "Cache parsed scores.",
    "createdAt": "2026-11-01T10:00:00Z",
    "updatedAt": "2026-11-03T10:00:00Z",
    "additions": 40, "deletions": 5, "changedFiles": 2,
    "files": [{"path": "backend/app/cache.py"},
              {"path": "backend/tests/test_cache.py"}],
    "comments": [{"author": {"login": "bob"}, "body": "Looks good"},
                 {"author": {"login": "ann"}, "body": "Fixed, PTAL"}],
    "latestReviews": [{"author": {"login": "bob"},
                       "state": "APPROVED"}],
    "reviewDecision": "APPROVED", "isDraft": False,
    "headRefName": "fix-cache", "baseRefName": "main",
}


class PrPackTests(unittest.TestCase):
    def test_card_is_measured_from_gh_json(self):
        pack = pr_pool.pr_to_pack(dict(RAW_PR))
        self.assertEqual(pack["type"], "pr")
        self.assertEqual(pack["title"], "#7 Fix the parser cache")
        self.assertIn("Cache parsed scores.", pack["body"])
        self.assertIn("2 comments", pack["body"])
        self.assertIn("@bob: APPROVED", pack["body"])
        self.assertIn("Latest, @ann", pack["body"])
        self.assertEqual(pack["link"],
                         "https://github.com/x/y/pull/7")
        self.assertEqual(pack["files"][0], "backend/app/cache.py")

    def test_fetch_errors_are_explained_not_raised(self):
        packs, err = pr_pool.fetch_pull_requests(
            tempfile.mkdtemp())
        self.assertEqual(packs, [])
        self.assertEqual(err, "not-a-git-repo")

    def test_fetch_parses_gh_output(self):
        root = pathlib.Path(tempfile.mkdtemp())
        (root / ".git").mkdir()

        class Proc:
            returncode = 0
            stdout = json.dumps([RAW_PR])
            stderr = ""

        packs, err = pr_pool.fetch_pull_requests(
            str(root), runner=lambda *a, **k: Proc())
        self.assertIsNone(err)
        self.assertEqual([p["id"] for p in packs], ["pr-7"])

    def test_engine_serves_live_pr_packs(self):
        engine = ce.ContentEngine([])
        engine.set_live_packs([pr_pool.pr_to_pack(dict(RAW_PR))])
        pool = engine.packs_for({"pr"})
        self.assertEqual([p["id"] for p in pool], ["pr-7"])
        out = ce.with_card_visual(pool[0])
        self.assertEqual(out["scene"]["type"], "pr")

    def test_pr_scene_paints(self):
        try:
            import cairo
        except ImportError:
            self.skipTest("pycairo not in this interpreter")
        pack = pr_pool.pr_to_pack(dict(RAW_PR))
        pack["components"] = [{"name": "backend", "files": 2}]
        pack = ce.with_card_visual(pack)
        colors = {"square": (0.97, 0.97, 0.97),
                  "text": (0.97, 0.97, 0.97),
                  "muted": (0.5, 0.5, 0.55),
                  "bg": (0.05, 0.05, 0.06)}
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 880, 300)
        cr = cairo.Context(surf)
        cr.set_source_rgb(*colors["bg"])
        cr.paint()
        before = bytes(surf.get_data())
        ce.paint_scene(cr, pack, 880, 300, colors, t=1.0)
        self.assertNotEqual(bytes(surf.get_data()), before)


if __name__ == "__main__":
    unittest.main()
