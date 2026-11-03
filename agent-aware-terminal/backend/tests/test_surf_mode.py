"""Surf mode (the companion content) is user-summoned, never
automatic.

Regression: once an agent had been busy for 7 s the left pane
swapped the terminal for content on its own, and a "long task"
gate verdict opened the split too — both stole the screen while
the user might still be typing. Now the busy transition only
offers: a 4-second, focus-free popup ("press Alt+I"), and the
content appears solely on Alt+I (or the existing manual controls).
"""
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


NT = _native()
# Gdk keyvals / modifier masks (stable values).
KEY_i, KEY_I = 0x69, 0x49
SHIFT, CTRL, ALT = 1 << 0, 1 << 2, 1 << 3


class SurfShortcutTests(unittest.TestCase):
    def test_alt_i_is_the_shortcut(self):
        self.assertTrue(NT.is_surf_shortcut(KEY_i, ALT))
        self.assertTrue(NT.is_surf_shortcut(KEY_I, ALT | SHIFT))

    def test_other_combos_are_not(self):
        self.assertFalse(NT.is_surf_shortcut(KEY_i, 0))
        self.assertFalse(NT.is_surf_shortcut(KEY_i, CTRL | ALT))
        self.assertFalse(NT.is_surf_shortcut(ord("x"), ALT))


class SurfOfferTests(unittest.TestCase):
    def test_popup_only_on_the_busy_rising_edge(self):
        self.assertEqual(NT.surf_offer(True, False), "show")
        self.assertIsNone(NT.surf_offer(True, True))

    def test_no_popup_when_content_already_on_screen(self):
        self.assertIsNone(
            NT.surf_offer(True, False, content_visible=True))

    def test_no_repopup_on_a_busy_blip(self):
        # A < 4 s dip in the busy signal is the same phase.
        self.assertIsNone(
            NT.surf_offer(True, False, resumed_blip=True))

    def test_popup_hides_when_the_agent_stops(self):
        self.assertEqual(NT.surf_offer(False, True), "hide")
        self.assertIsNone(NT.surf_offer(False, False))


class SurfPaneTests(unittest.TestCase):
    def test_left_pane_never_turns_to_content_on_its_own(self):
        # No override (whatever the agent is doing) -> terminal.
        self.assertEqual(NT.left_pane_target(None), "terminal")

    def test_manual_override_still_wins(self):
        self.assertEqual(NT.left_pane_target("content"), "content")
        self.assertEqual(NT.left_pane_target("terminal"), "terminal")


class SurfPopupCopyTests(unittest.TestCase):
    def test_popup_is_four_seconds_and_names_the_key(self):
        self.assertEqual(NT.SURF_POPUP_SECS, 4)
        self.assertIn("Alt+I", NT.SURF_POPUP_TEXT)


if __name__ == "__main__":
    unittest.main()
