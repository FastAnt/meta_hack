"""Modified Enter (Shift+Enter / Alt+Enter) must reach TUIs as a
distinct key, not collapse into plain Enter.

Regression: in prime-agent's chat Shift+Enter submits instead of
inserting a newline. The VTE in this app (0.76) grants neither the
kitty keyboard protocol nor xterm modifyOtherKeys (prime-agent's
own telemetry reports both false), so VTE sends a bare CR for
every Enter variant and the TUI sees plain Enter. The app therefore
translates modified Enter itself into the kitty CSI-u sequence,
which crossterm-based TUIs parse as Enter+modifier."""
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
SEQ = NT.modified_enter_sequence
# Gdk keyvals / modifier masks (stable values).
RETURN, KP_ENTER = 0xFF0D, 0xFF8D
SHIFT, CTRL, ALT, LOCK = 1 << 0, 1 << 2, 1 << 3, 1 << 1


class ModifiedEnterTests(unittest.TestCase):
    def test_plain_enter_is_left_to_vte(self):
        self.assertIsNone(SEQ(RETURN, 0))
        self.assertIsNone(SEQ(KP_ENTER, 0))
        self.assertIsNone(SEQ(RETURN, LOCK))

    def test_shift_enter_is_kitty_csi_u(self):
        self.assertEqual(SEQ(RETURN, SHIFT), b"\x1b[13;2u")
        self.assertEqual(SEQ(KP_ENTER, SHIFT), b"\x1b[13;2u")

    def test_alt_enter_keeps_its_own_modifier(self):
        # prime-agent: Alt+Enter queues a follow-up, Shift+Enter
        # inserts a newline — the two must stay distinct.
        self.assertEqual(SEQ(RETURN, ALT), b"\x1b[13;3u")
        self.assertEqual(SEQ(RETURN, ALT | SHIFT), b"\x1b[13;4u")

    def test_ctrl_combinations_encode_too(self):
        self.assertEqual(SEQ(RETURN, CTRL | SHIFT), b"\x1b[13;6u")

    def test_lock_masks_do_not_create_a_phantom_modifier(self):
        self.assertEqual(SEQ(RETURN, SHIFT | LOCK), b"\x1b[13;2u")

    def test_other_keys_untouched(self):
        self.assertIsNone(SEQ(ord("a"), SHIFT))
        self.assertIsNone(SEQ(0xFF1B, SHIFT))  # Escape


if __name__ == "__main__":
    unittest.main()
