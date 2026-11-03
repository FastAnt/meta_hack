"""Copy/paste in the terminal must work on any keyboard layout.

Regression: pasting into prime-agent did nothing at all. The
app left paste to VTE's implicit keyval binding, but on a
Russian layout the physical V key reports keyval Cyrillic м
(U+043C), not "v" — the binding never fires. (The same trap
waits for Alt+I: I is ш there.) Shortcuts are therefore matched
by X11 hardware keycode as well as by Latin and Cyrillic
keyvals, and handled explicitly with VTE's paste_clipboard(),
which honours bracketed paste — a multiline prompt lands in
the agent's editor instead of submitting line by line.
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
SHIFT, CTRL, ALT = 1 << 0, 1 << 2, 1 << 3
KEY_V, KEY_C, KEY_I = 0x76, 0x63, 0x69
CYR_V, CYR_C, CYR_I = 0x043C, 0x0441, 0x0448  # м, с, ш
INSERT = 0xFF63
HW_V, HW_C, HW_I = 55, 54, 31  # evdev keycodes + 8 (X11)


class PasteKeyTests(unittest.TestCase):
    def test_paste_shortcuts_latin(self):
        self.assertEqual(
            NT.clipboard_key_action(KEY_V, CTRL | SHIFT, 0), "paste")
        self.assertEqual(
            NT.clipboard_key_action(KEY_V, CTRL, 0), "paste")
        self.assertEqual(
            NT.clipboard_key_action(INSERT, SHIFT, 0), "paste")

    def test_paste_shortcuts_russian_layout(self):
        # Keyvals alone, and hardware keycodes alone, both work.
        self.assertEqual(
            NT.clipboard_key_action(CYR_V, CTRL | SHIFT, 0), "paste")
        self.assertEqual(
            NT.clipboard_key_action(CYR_V, CTRL, 0), "paste")
        self.assertEqual(
            NT.clipboard_key_action(0, CTRL | SHIFT, HW_V), "paste")

    def test_copy_only_with_ctrl_shift(self):
        self.assertEqual(
            NT.clipboard_key_action(KEY_C, CTRL | SHIFT, 0), "copy")
        self.assertEqual(
            NT.clipboard_key_action(CYR_C, CTRL | SHIFT, 0), "copy")
        self.assertEqual(
            NT.clipboard_key_action(0, CTRL | SHIFT, HW_C), "copy")
        # Plain Ctrl+C stays the terminal's interrupt.
        self.assertIsNone(NT.clipboard_key_action(KEY_C, CTRL, 0))

    def test_typing_is_never_stolen(self):
        self.assertIsNone(NT.clipboard_key_action(KEY_V, 0, HW_V))
        self.assertIsNone(
            NT.clipboard_key_action(KEY_V, ALT | CTRL, HW_V))
        self.assertIsNone(
            NT.clipboard_key_action(ord("x"), CTRL | SHIFT, 0))


class SurfLayoutTests(unittest.TestCase):
    def test_alt_i_on_russian_layout(self):
        self.assertTrue(NT.is_surf_shortcut(CYR_I, ALT))
        self.assertTrue(NT.is_surf_shortcut(0, ALT, HW_I))
        self.assertFalse(NT.is_surf_shortcut(0, ALT, HW_V))


if __name__ == "__main__":
    unittest.main()
