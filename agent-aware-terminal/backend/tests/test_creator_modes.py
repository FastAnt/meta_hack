"""The bulb button is a three-state creator mode, and news packs
get a text-graphic visual.

Creator modes: "one" (create a single pack in the selected
category — the old bulb behaviour, and the default), "auto"
(keep creating in the selected category until 10 unread packs
pile up, then pause), "off" (create nothing). News packs arrive
as bare JSON with no scene, so the pane synthesizes a minimal
animated text graphic for them instead of showing a blank.
"""
import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from backend.app import content_engine as ce  # noqa: E402


def _native():
    spec = importlib.util.spec_from_file_location(
        "native_terminal", ROOT / "desktop" / "native_terminal.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


NT = _native()


class CreatorModeTests(unittest.TestCase):
    def test_cycle_is_one_auto_off(self):
        self.assertEqual(NT.next_creator_mode("one"), "auto")
        self.assertEqual(NT.next_creator_mode("auto"), "off")
        self.assertEqual(NT.next_creator_mode("off"), "one")
        self.assertEqual(NT.next_creator_mode("???"), "one")

    def test_auto_spawns_only_below_the_unread_cap(self):
        self.assertTrue(NT.creator_spawn_wanted(
            "auto", busy=False, unread=9))
        self.assertFalse(NT.creator_spawn_wanted(
            "auto", busy=False, unread=10))
        self.assertFalse(NT.creator_spawn_wanted(
            "auto", busy=True, unread=0))

    def test_off_never_spawns_and_one_is_click_driven(self):
        self.assertFalse(NT.creator_spawn_wanted(
            "off", busy=False, unread=0))
        self.assertFalse(NT.creator_spawn_wanted(
            "one", busy=False, unread=0))

    def test_unread_count_uses_served_ids(self):
        packs = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        self.assertEqual(NT.unread_pack_count(packs, ["a"]), 2)
        self.assertEqual(NT.unread_pack_count(packs, []), 3)


class CreatorClickTests(unittest.TestCase):
    def test_click_cycles_even_while_busy(self):
        # The bulb must never go dead while the researcher runs:
        # a click still cycles the mode. Entering "one" mid-flight
        # queues that single creation instead of spawning a second
        # researcher.
        self.assertEqual(NT.creator_mode_click("auto", busy=True),
                         ("off", "stop"))
        self.assertEqual(NT.creator_mode_click("off", busy=True),
                         ("one", "queue_one"))
        self.assertEqual(NT.creator_mode_click("off", busy=False),
                         ("one", "spawn_one"))
        self.assertEqual(NT.creator_mode_click("one", busy=True),
                         ("auto", "start_auto"))

    def test_unread_cap_counts_project_packs_only(self):
        # The generic catalogue (500 facts) must not fill the
        # "10 unread" cap: only packs deposited for this project
        # (.aat/content) count.
        import tempfile
        root = pathlib.Path(tempfile.mkdtemp())
        proj = root / "proj" / ".aat" / "content"
        glob = root / "global"
        proj.mkdir(parents=True)
        glob.mkdir()

        def pack(pid, kind):
            return {"type": kind, "id": pid, "title": pid,
                    "body": "x", "link": "https://example.com/x"}

        (proj / "n1.json").write_text(
            __import__("json").dumps(pack("p-news", "news")))
        for i in range(15):
            (glob / f"g{i}.json").write_text(
                __import__("json").dumps(pack(f"g-news-{i}", "news")))
        engine = ce.ContentEngine([proj, glob])
        mine = engine.project_packs({"news"})
        self.assertEqual([p["id"] for p in mine], ["p-news"])
        self.assertEqual(
            NT.unread_pack_count(mine, engine._served), 1)


class NewsVisualTests(unittest.TestCase):
    PACK = {"type": "news", "id": "n1", "title": "Suno puts "
            "section-level edits in the prompt",
            "body": "You can rewrite one chorus while the rest "
                    "of the song stays intact.",
            "link": "https://suno.com/blog/v6"}

    def test_news_without_scene_gets_one(self):
        # The scene must describe THIS story (editing one
        # section), not the generic headline card every news
        # pack used to share.
        pack = ce.with_card_visual(dict(self.PACK))
        self.assertEqual(pack["scene"]["type"], "sections")
        self.assertIn("anim", pack["scene"])

    def test_visual_kind_follows_the_story(self):
        cases = [
            ({"type": "news", "title": "Music can now decompile "
              "back into code", "body": "Turns symbolic MIDI "
              "into a runnable program.",
              "link": "https://x.example/a"}, "convert"),
            ({"type": "news", "title": "New model composes "
              "melodies from text", "body": "A melody generator.",
              "link": "https://x.example/b"}, "music"),
            ({"type": "news", "title": "Compiler 2x faster in "
              "the new release", "body": "The release doubles "
              "build speed.", "link": "https://x.example/c"},
             "release"),
            ({"type": "idea", "title": "Round-trip invariant: "
              "DSL to MIDI and back unchanged",
              "body": "from_midi(to_midi(song)) == song",
              "action_hint": "Write to_midi() and from_midi()"},
             "convert"),
            ({"type": "idea", "title": "Add a cache for parsed "
              "scores", "body": "Parsing is the hot path.",
              "action_hint": "Create cache.py with a dict"},
             "blueprint"),
        ]
        for pack, kind in cases:
            with self.subTest(kind=kind):
                pack.setdefault("id", "x")
                out = ce.with_card_visual(dict(pack))
                self.assertEqual(out["scene"]["type"], kind)

    def test_researcher_can_declare_the_visual(self):
        pack = dict(self.PACK, visual={"kind": "music"})
        out = ce.with_card_visual(pack)
        self.assertEqual(out["scene"]["type"], "music")

    def test_ideas_get_a_visual_too(self):
        idea = {"type": "idea", "id": "i1",
                "title": "Add a cache for parsed scores",
                "body": "Parsing is the hot path.",
                "action_hint": "Create cache.py with a dict"}
        out = ce.with_card_visual(idea)
        self.assertEqual(out["scene"]["type"], "blueprint")
        self.assertNotIn("scene", idea)

    def test_other_types_and_own_visuals_untouched(self):
        fact = {"type": "fact", "id": "f1", "title": "x"}
        self.assertNotIn("scene", ce.with_news_visual(fact))
        own = dict(self.PACK, scene={"type": "orbits"})
        self.assertEqual(ce.with_news_visual(own)["scene"]["type"],
                         "orbits")
        # The input pack is never mutated.
        self.assertNotIn("scene", self.PACK)

    def test_news_scene_paints_and_moves(self):
        try:
            import cairo
        except ImportError:
            self.skipTest("pycairo not in this interpreter")
        colors = {"square": (0.97, 0.97, 0.97),
                  "text": (0.97, 0.97, 0.97),
                  "muted": (0.5, 0.5, 0.55),
                  "bg": (0.05, 0.05, 0.06)}
        pack = ce.with_card_visual(dict(self.PACK))

        def frame(at):
            surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 880, 300)
            cr = cairo.Context(surf)
            cr.set_source_rgb(*colors["bg"])
            cr.paint()
            ce.paint_scene(cr, pack, 880, 300, colors, t=at)
            return bytes(surf.get_data())

        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 880, 300)
        cr = cairo.Context(surf)
        cr.set_source_rgb(*colors["bg"])
        cr.paint()
        self.assertNotEqual(frame(0.0), bytes(surf.get_data()))
        self.assertNotEqual(frame(0.0), frame(2.5))  # it animates


if __name__ == "__main__":
    unittest.main()
