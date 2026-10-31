"""Content engine: pack loading, validation, flow, geometry."""
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.app import content_engine as ce  # noqa: E402

GAMES_DIR = ROOT / "games-base"


class ShippedPacksTests(unittest.TestCase):
    def test_three_test_games_load_clean(self):
        packs, errors = ce.scan_packs([GAMES_DIR])
        self.assertEqual(errors, [])
        self.assertEqual(len(packs), 3)
        self.assertEqual({p["id"] for p in packs},
                         {"physics-orbits-001", "architecture-mvc-001",
                          "ml-gradient-001"})

    def test_each_game_is_answerable(self):
        packs, _ = ce.scan_packs([GAMES_DIR])
        for pack in packs:
            targets = {c["id"] for c in pack.get("choices") or []} | \
                set(ce.scene_targets(pack))
            self.assertIn(pack["answer"], targets, pack["id"])


class ValidationTests(unittest.TestCase):
    def _bad(self, raw):
        pack, err = ce.validate_pack(raw)
        self.assertIsNone(pack)
        self.assertTrue(err)

    def test_rejects_garbage(self):
        self._bad([1, 2])
        self._bad({"title": "no id"})
        self._bad({"id": "x", "kind": "teleport", "title": "t"})
        self._bad({"id": "x", "title": "t", "answer": "nope",
                   "choices": [{"id": "a", "label": "A"}]})
        self._bad({"id": "x", "title": "t",
                   "scene": {"type": "hologram"}})
        self._bad({"id": "x", "title": "t",
                   "scene": {"type": "orbits", "rings": 1},
                   "answer": "ring-1"})
        self._bad({"id": "x", "title": "t",
                   "scene": {"type": "diagram",
                             "nodes": [{"id": "a", "label": "A"}],
                             "edges": [{"from": "a", "to": "ghost"}]},
                   "answer": "a"})

    def test_duplicate_ids_rejected_at_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack = {"id": "dup", "title": "T", "answer": "a",
                    "choices": [{"id": "a", "label": "A"}]}
            # an identical twin (same pack via two dirs) is fine
            for name in ("one.json", "two.json"):
                pathlib.Path(tmp, name).write_text(json.dumps(pack))
            packs, errors = ce.scan_packs([tmp])
            self.assertEqual(len(packs), 1)
            self.assertEqual(errors, [])
            # a different pack reusing the id is a conflict
            pathlib.Path(tmp, "two.json").write_text(
                json.dumps(dict(pack, title="Other")))
            packs, errors = ce.scan_packs([tmp])
            self.assertEqual(len(packs), 1)
            self.assertTrue(any("duplicate" in e for e in errors))


class EngineFlowTests(unittest.TestCase):
    def setUp(self):
        self.engine = ce.ContentEngine([GAMES_DIR], seed=7)

    def test_answer_scoring_and_streak(self):
        game = self.engine.next_game()
        self.assertIsNotNone(game)
        out = self.engine.answer(game["answer"])
        self.assertTrue(out["correct"])
        self.assertEqual(self.engine.score["correct"], 1)
        self.assertEqual(self.engine.score["streak"], 1)
        game = self.engine.next_game()
        out = self.engine.answer("__wrong__")
        self.assertFalse(out["correct"])
        self.assertEqual(self.engine.score["streak"], 0)
        self.assertTrue(out["explanation"])

    def test_progress_walks_the_deck(self):
        seen = set()
        for _ in range(3):
            game = self.engine.next_game()
            seen.add(game["id"])
        self.assertEqual(len(seen), 3)
        self.assertEqual(self.engine.progress(), (3, 3))
        self.engine.next_game()  # wraps and reshuffles
        self.assertEqual(self.engine.progress(), (1, 3))

    def test_hot_reload_picks_up_new_pack(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = ce.ContentEngine([tmp], seed=1)
            self.assertIsNone(engine.next_game())
            pack = {"id": "late-news", "kind": "card",
                    "title": "New pack dropped in live",
                    "body": "Agent-generated content appears here."}
            pathlib.Path(tmp, "late.json").write_text(json.dumps(pack))
            game = engine.next_game()
            self.assertIsNotNone(game)
            self.assertEqual(game["id"], "late-news")
            out = engine.answer("anything")
            self.assertIsNone(out["correct"])  # cards never score
            self.assertEqual(engine.score["answered"], 0)


class GeometryTests(unittest.TestCase):
    def test_orbit_hit(self):
        radii = ce.ring_radii(400, 400, 4)
        self.assertEqual(len(radii), 4)
        self.assertEqual(
            ce.orbit_hit(200, 200, radii, 200 + radii[0], 200), "ring-1")
        self.assertEqual(
            ce.orbit_hit(200, 200, radii, 200, 200 - radii[3]), "ring-4")
        self.assertIsNone(ce.orbit_hit(200, 200, radii, 200, 200))

    def test_node_hit(self):
        nodes = [{"id": "a", "x": 0.5, "y": 0.5, "w": 0.2, "h": 0.2}]
        rects = ce.node_rects(nodes, 1000, 800)
        self.assertEqual(ce.node_hit(rects, 500, 400), "a")
        self.assertIsNone(ce.node_hit(rects, 10, 10))

    def test_network_positions(self):
        pts = ce.network_positions([3, 4, 2], 900, 600)
        self.assertEqual(len(pts), 9)
        self.assertEqual({p[2] for p in pts}, {0, 1, 2})


if __name__ == "__main__":
    unittest.main()


class NoRepeatCycleTests(unittest.TestCase):
    def test_full_cycle_without_repeats_then_resets(self):
        engine = ce.ContentEngine([GAMES_DIR], seed=3)
        seen = [engine.next_game()["id"] for _ in range(3)]
        self.assertEqual(len(set(seen)), 3)
        fourth = engine.next_game()["id"]
        self.assertEqual(engine.progress(), (1, 3))
        self.assertNotEqual(fourth, seen[-1])

    def test_cycle_survives_restart_via_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = pathlib.Path(tmp, "state.json")
            first = ce.ContentEngine([GAMES_DIR], seed=5,
                                     state_path=state)
            served = {first.next_game()["id"],
                      first.next_game()["id"]}
            second = ce.ContentEngine([GAMES_DIR], seed=5,
                                      state_path=state)
            nxt = second.next_game()["id"]
            self.assertNotIn(nxt, served)  # still the unseen one
            self.assertEqual(second.progress(), (3, 3))


class AnimationTests(unittest.TestCase):
    def test_anim_validation(self):
        pack, err = ce.validate_pack({
            "id": "x", "title": "t", "answer": "a",
            "choices": [{"id": "a", "label": "A"}],
            "scene": {"type": "network", "layers": [2, 2],
                      "anim": {"type": "orbit"}}})
        self.assertIsNone(pack)  # orbit is not a network anim
        pack, err = ce.validate_pack({
            "id": "x", "title": "t", "answer": "a",
            "choices": [{"id": "a", "label": "A"}],
            "scene": {"type": "network", "layers": [2, 2],
                      "anim": {"type": "pulse", "speed": 0.8}}})
        self.assertIsNotNone(pack, err)

    def test_shipped_games_are_animated(self):
        packs, _ = ce.scan_packs([GAMES_DIR])
        for pack in packs:
            self.assertTrue((pack.get("scene") or {}).get("anim"),
                            f"{pack['id']} should animate")

    def test_paint_moves_with_time(self):
        try:
            import cairo
        except ImportError:
            self.skipTest("pycairo not in this interpreter")
        packs, _ = ce.scan_packs([GAMES_DIR])
        colors = {"square": (0.97, 0.97, 0.97),
                  "text": (0.97, 0.97, 0.97),
                  "muted": (0.5, 0.5, 0.55), "bg": (0.05, 0.05, 0.06)}

        def frame(pack, at):
            surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 880, 620)
            cr = cairo.Context(surf)
            cr.set_source_rgb(*colors["bg"])
            cr.paint()
            ce.paint_scene(cr, pack, 880, 620, colors, t=at)
            return bytes(surf.get_data())

        for pack in packs:
            self.assertNotEqual(frame(pack, 0.2), frame(pack, 1.8),
                                f"{pack['id']} does not move")
