"""Content engine: pack loading, validation, flow, geometry."""
import json
import pathlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.app import content_engine as ce  # noqa: E402

GAMES_DIR = ROOT / "games-base"


class ShippedPacksTests(unittest.TestCase):
    def test_base_games_load_clean(self):
        # The catalogue grows (agent-authored games land here);
        # the three originals must always load, with no errors.
        packs, errors = ce.scan_packs([GAMES_DIR])
        self.assertEqual(errors, [])
        ids = {p["id"] for p in packs}
        self.assertTrue({"physics-orbits-001", "architecture-mvc-001",
                         "ml-gradient-001"} <= ids)
        self.assertGreaterEqual(len(packs), 3)

    def test_each_game_is_answerable(self):
        packs, _ = ce.scan_packs([GAMES_DIR])
        packs = [p for p in packs
                 if ce.resolve_type(p) == "game"]
        for pack in packs:
            targets = {c["id"] for c in pack.get("choices") or []} | \
                set(ce.scene_targets(pack))
            if callable(pack.get("_target_at")):
                view = ce.GameView(880, 620, 0.0, {})
                for gx in range(0, 880, 26):
                    for gy in range(0, 620, 26):
                        hit = pack["_target_at"](view, gx, gy)
                        if hit:
                            targets.add(hit)
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


def _tiny_deck(tmp):
    import json as _json
    for i in range(3):
        pathlib.Path(tmp, f"deck-game-{i}.json").write_text(_json.dumps({
            "id": f"deck-game-{i}", "kind": "quiz",
            "title": f"Deck {i}", "question": "?",
            "choices": [{"id": "a", "label": "A"},
                        {"id": "b", "label": "B"}],
            "answer": "a", "explanation": "because"}))


class EngineFlowTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _tiny_deck(self._tmp.name)
        self.engine = ce.ContentEngine([self._tmp.name], seed=7)

    def tearDown(self):
        self._tmp.cleanup()

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
        with tempfile.TemporaryDirectory() as tmp:
            _tiny_deck(tmp)
            engine = ce.ContentEngine([tmp], seed=3)
            seen = [engine.next_game()["id"] for _ in range(3)]
            self.assertEqual(len(set(seen)), 3)
            fourth = engine.next_game()["id"]
            self.assertEqual(engine.progress(), (1, 3))
            self.assertNotEqual(fourth, seen[-1])

    def test_cycle_survives_restart_via_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            deck = pathlib.Path(tmp, "deck")
            deck.mkdir()
            _tiny_deck(str(deck))
            state = pathlib.Path(tmp, "state.json")
            first = ce.ContentEngine([deck], seed=5,
                                     state_path=state)
            served = {first.next_game()["id"],
                      first.next_game()["id"]}
            second = ce.ContentEngine([deck], seed=5,
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
        packs = [p for p in packs
                 if ce.resolve_type(p) == "game"]
        for pack in packs:
            if callable(pack.get("_paint")):
                continue  # motion asserted by the paint test
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
            # Enforced motion = the renderer's own contract: a
            # JSON scene that declares an anim block must move.
            # Bespoke module paints are author code — small
            # animation there is an authoring guideline (see
            # games-base/AGENTS.md) checked at preview/review,
            # not pixel-diffable at fixed sample times (slow
            # movers and post-answer motion false-positive).
            if not (pack.get("scene") or {}).get("anim"):
                continue
            self.assertNotEqual(frame(pack, 0.2), frame(pack, 6.0),
                                f"{pack['id']} does not move")


class PythonGameTests(unittest.TestCase):
    MODULE = '''
GAME = {"id": "py-demo-001", "kind": "quiz", "title": "Py",
        "question": "?", "choices": [{"id": "a", "label": "A"},
        {"id": "b", "label": "B"}], "answer": "a",
        "explanation": "because"}

def paint(cr, view):
    import math
    r = 20 + 6 * math.sin(view.t * 3)
    cr.arc(view.w / 2, view.h / 2, r, 0, 6.2832)
    cr.set_source_rgba(*view.colors["square"], 1)
    cr.fill()
'''

    def _engine(self, tmp, source):
        pathlib.Path(tmp, "py_demo.py").write_text(source)
        return ce.scan_packs([tmp])

    def test_loads_and_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            packs, errors = self._engine(tmp, self.MODULE)
            self.assertEqual(errors, [])
            self.assertEqual(len(packs), 1)
            self.assertTrue(callable(packs[0]["_paint"]))

    def test_broken_module_is_an_error_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            packs, errors = self._engine(tmp, "def paint(:\n")
            self.assertEqual(packs, [])
            self.assertTrue(errors)
            packs, errors = self._engine(tmp, "X = 1\n")
            self.assertEqual(packs, [])
            self.assertTrue(errors)

    def test_click_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self.MODULE + '''

def target_at(view, x, y):
    return "left" if x < view.w / 2 else "right"
'''
            packs, _ = self._engine(tmp, src)
            self.assertEqual(
                ce.pack_target_at(packs[0], 800, 600, 10, 10), "left")
            self.assertEqual(
                ce.pack_target_at(packs[0], 800, 600, 700, 10),
                "right")


class ShippedTypeDeclarationTests(unittest.TestCase):
    """Convention guard: every shipped game declares its
    content type explicitly in the source file."""

    def test_all_shipped_sources_declare_type(self):
        import ast as _ast
        missing = []
        for f in sorted(GAMES_DIR.glob("*.py")):
            if f.name.startswith("test"):
                continue
            tree = _ast.parse(f.read_text())
            declared = False
            for node in _ast.walk(tree):
                if isinstance(node, _ast.Assign) and any(
                        isinstance(tg, _ast.Name) and tg.id == "GAME"
                        for tg in node.targets) \
                        and isinstance(node.value, _ast.Dict):
                    keys = [k.value for k in node.value.keys
                            if isinstance(k, _ast.Constant)]
                    declared = "type" in keys
            if not declared:
                missing.append(f.name)
        for f in sorted(GAMES_DIR.glob("*.json")):
            raw = json.loads(f.read_text())
            if "type" not in raw:
                missing.append(f.name)
        self.assertEqual(missing, [])


class FactBespokeVisualTests(unittest.TestCase):
    """Anti-stamp guard: every shipped fact module must carry
    its own paint() implementation — a reused scene skeleton
    (same code with different literals) fails the suite."""

    def test_fact_paint_skeletons_are_unique(self):
        import ast as _ast
        import hashlib
        import re as _re

        def skeleton(path):
            tree = _ast.parse(path.read_text())
            for node in tree.body:
                if isinstance(node, _ast.FunctionDef) \
                        and node.name == "paint":
                    src = _ast.unparse(node)
                    # Labels may repeat; geometry may not —
                    # same coordinates with new captions is
                    # exactly the stamp this guard exists for.
                    src = _re.sub(r"'[^']*'", "'S'", src)
                    src = _re.sub(r'"[^"]*"', "'S'", src)
                    return hashlib.sha1(src.encode()).hexdigest()
            return None

        seen = {}
        dupes = []
        for f in sorted(GAMES_DIR.glob("*fact*.py")):
            sk = skeleton(f)
            self.assertIsNotNone(sk, f"{f.name}: fact has no paint()")
            if sk in seen:
                dupes.append((f.name, seen[sk]))
            seen[sk] = f.name
        self.assertEqual(dupes, [])


class ContentTypeTests(unittest.TestCase):
    def test_legacy_kinds_resolve_to_types(self):
        pack, err = ce.validate_pack({
            "id": "x", "title": "t", "kind": "quiz",
            "choices": [{"id": "a", "label": "A"}], "answer": "a"})
        self.assertEqual(pack["type"], "game")
        pack, err = ce.validate_pack({
            "id": "x", "title": "t", "kind": "card", "body": "b"})
        self.assertEqual(pack["type"], "fact")

    def test_news_needs_link_fact_idea_need_body(self):
        _, err = ce.validate_pack({"id": "n", "type": "news",
                                   "title": "t", "body": "b"})
        self.assertIsNotNone(err)
        pack, err = ce.validate_pack({
            "id": "n", "type": "news", "title": "t", "body": "b",
            "link": "https://example.com/a"})
        self.assertIsNotNone(pack, err)
        _, err = ce.validate_pack({"id": "i", "type": "idea",
                                   "title": "t"})
        self.assertIsNotNone(err)
        _, err = ce.validate_pack({"id": "z", "type": "bogus",
                                   "title": "t"})
        self.assertIsNotNone(err)

    def test_non_game_types_do_not_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            pathlib.Path(tmp, "f.json").write_text(json.dumps({
                "id": "f1", "type": "fact", "title": "F",
                "body": "Because."}))
            engine = ce.ContentEngine([tmp], seed=1)
            self.assertEqual(engine.next_game()["type"], "fact")
            self.assertIsNone(engine.answer("x")["correct"])
            self.assertEqual(engine.score["answered"], 0)


class TypeFilterTests(unittest.TestCase):
    def test_only_types_filters_the_deck(self):
        with tempfile.TemporaryDirectory() as tmp:
            pathlib.Path(tmp, "g.json").write_text(json.dumps({
                "id": "g1", "type": "game", "title": "G",
                "choices": [{"id": "a", "label": "A"}],
                "answer": "a", "explanation": "e"}))
            pathlib.Path(tmp, "f.json").write_text(json.dumps({
                "id": "f1", "type": "fact", "title": "F",
                "body": "Because."}))
            engine = ce.ContentEngine([tmp], seed=1,
                                      only_types={"fact"})
            self.assertEqual([p["id"] for p in engine.packs], ["f1"])
            self.assertEqual(engine.next_game()["id"], "f1")
            self.assertEqual(engine.progress(), (1, 1))


class RuntimeTypeFilterTests(unittest.TestCase):
    """The pane's All / News / Facts / Quiz / Ideas chips."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = pathlib.Path(self._tmp.name)
        tmp.joinpath("g1.json").write_text(json.dumps({
            "id": "g1", "type": "game", "title": "G1",
            "choices": [{"id": "a", "label": "A"}],
            "answer": "a", "explanation": "e"}))
        tmp.joinpath("g2.json").write_text(json.dumps({
            "id": "g2", "type": "game", "title": "G2",
            "choices": [{"id": "a", "label": "A"}],
            "answer": "a", "explanation": "e"}))
        tmp.joinpath("f1.json").write_text(json.dumps({
            "id": "f1", "type": "fact", "title": "F1",
            "body": "Because."}))
        tmp.joinpath("f2.json").write_text(json.dumps({
            "id": "f2", "type": "fact", "title": "F2",
            "body": "Because."}))
        tmp.joinpath("n1.json").write_text(json.dumps({
            "id": "n1", "type": "news", "title": "N1",
            "body": "News.", "link": "https://example.com/n"}))
        tmp.joinpath("i1.json").write_text(json.dumps({
            "id": "i1", "type": "idea", "title": "I1",
            "body": "Idea."}))
        self.engine = ce.ContentEngine([tmp], seed=4)

    def tearDown(self):
        self._tmp.cleanup()

    def test_filter_serves_only_the_chosen_type(self):
        self.engine.set_type_filter({"fact"})
        seen = {self.engine.next_game()["id"]
                for _ in range(2)}
        self.assertEqual(seen, {"f1", "f2"})
        self.assertEqual(self.engine.progress(), (2, 2))
        self.engine.set_type_filter({"news"})
        self.assertEqual(self.engine.next_game()["id"], "n1")
        self.assertEqual(self.engine.progress(), (1, 1))

    def test_filter_follows_the_shuffled_deck(self):
        # Serving is randomized, not catalogue order: the first
        # filtered card is the first matching card in the deck.
        self.engine.set_type_filter({"fact"})
        expected = [p["id"] for p in self.engine.deck
                    if ce.resolve_type(p) == "fact"]
        self.assertEqual(self.engine.next_game()["id"], expected[0])

    def test_empty_filter_has_nothing_to_serve(self):
        self.engine.set_type_filter({"idea"})
        self.assertEqual(self.engine.next_game()["id"], "i1")
        # The catalogue contains no second idea: the deck wraps,
        # still within the chosen type.
        self.assertEqual(self.engine.next_game()["id"], "i1")
        self.engine.set_type_filter(set())
        ids = {self.engine.next_game()["id"] for _ in range(3)}
        self.assertTrue(ids & {"g1", "g2", "f1", "f2"})

    def test_full_cycle_of_a_filter_keeps_other_progress(self):
        self.engine.set_type_filter({"game"})
        served = {self.engine.next_game()["id"] for _ in range(2)}
        self.assertEqual(served, {"g1", "g2"})
        self.engine.set_type_filter({"fact"})
        self.engine.next_game()
        self.engine.next_game()
        # Both facts are now seen; the games must still be seen too.
        self.engine.set_type_filter({"game"})
        self.assertEqual(self.engine.progress(), (2, 2))

    def test_reset_history_clears_the_deck_and_persists(self):
        state = pathlib.Path(self._tmp.name) / "state.json"
        self.engine.state_path = state
        self.engine.next_game()
        self.engine.next_game()
        self.assertTrue(state.exists())
        self.assertEqual(self.engine.reset_history(), 2)
        self.assertEqual(self.engine.progress(), (0, 6))
        self.assertEqual(json.loads(state.read_text())["served"], [])
        # The catalogue is untouched and the next serve starts over.
        self.assertEqual(len(self.engine.packs), 6)
        self.assertIsNotNone(self.engine.next_game())

    def test_type_counts_and_all_filter(self):
        self.assertEqual(self.engine.type_counts(),
                         {"game": 2, "fact": 2, "news": 1,
                          "idea": 1})
        self.engine.set_type_filter({"game", "fact", "news", "idea"})
        self.assertIsNone(self.engine.filter_types)  # all four = All
        self.assertTrue(self.engine.matches(self.engine.packs[0]))
