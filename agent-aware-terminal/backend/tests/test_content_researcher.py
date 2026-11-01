"""Content Researcher: profiler, lifecycle, deposits."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.app import content_researcher as cr  # noqa: E402
from backend.app.content_engine import ContentEngine  # noqa: E402


def make_project(tmp, readme, files=None):
    root = Path(tmp)
    (root / "README.md").write_text(readme)
    for name, text in (files or {}).items():
        f = root / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    return root


class ProfilerTests(unittest.TestCase):
    def test_audio_domain_from_project_and_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = make_project(
                tmp, "# Neural audio codec\nWe render spectrograms "
                     "and train a denoiser.",
                {"src/codec.py": "import torch  # opus-like codec\n"})
            profile = cr.profile_project(
                proj, "fix spectrogram noise above 8 kHz")
            ids = [d["id"] for d in profile["domains"]]
            self.assertIn("audio-dsp", ids)
            self.assertEqual(ids[0], "audio-dsp")  # task wins

    def test_cloud_and_crypto_domains(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = make_project(
                tmp, "# API gateway\nLow latency, high throughput.",
                {"go.mod": "module x\n"})
            profile = cr.profile_project(
                proj, "audit the TLS handshake and AES-GCM nonce use")
            ids = [d["id"] for d in profile["domains"]]
            self.assertIn("security-crypto", ids)
            self.assertIn("cloud-data", ids)

    def test_wave_math_angles_present_for_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = make_project(tmp, "# Synth engine with FFT")
            profile = cr.profile_project(proj, "add an oscillator")
            audio = profile["domains"][0]
            self.assertTrue(any("wave" in a for a in audio["angles"]))


class LifecycleTests(unittest.TestCase):
    def test_brief_writes_hidden_research_pack(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = make_project(tmp, "# Synth engine with FFT")
            cr.write_brief(proj, "tune the oscillator")
            rdir = Path(tmp) / ".aat" / "research"
            self.assertTrue((rdir / "brief.md").is_file())
            self.assertTrue((rdir / "domain_profile.json").is_file())
            self.assertTrue((Path(tmp) / ".aat" / "content").is_dir())
            self.assertEqual(cr.lifecycle_state(proj), "briefed")

    def test_lifecycle_rejects_unknown_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                cr.advance_lifecycle(tmp, "shipped")

    def test_deposited_fact_card_is_served_by_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = make_project(tmp, "# Synth engine with FFT")
            cr.write_brief(proj, "tune the oscillator")
            card = {"id": "audio-fact-nyquist-001", "kind": "card",
                    "kicker": "PROJECT FACT", "topic": "Audio & DSP",
                    "title": "Why 44.1 kHz?",
                    "body": "Early digital masters lived on video "
                            "tape; the rate is a TV fossil.",
                    "source_hint": "Red Book history"}
            (Path(tmp) / ".aat" / "content"
             / "audio-fact-nyquist-001.json").write_text(
                json.dumps(card))
            out = cr.summarize_outputs(proj)
            self.assertEqual([p["id"] for p in out["packs"]],
                             ["audio-fact-nyquist-001"])
            self.assertEqual(out["errors"], [])
            engine = ContentEngine([cr.content_dir(proj)], seed=1)
            game = engine.next_game()
            self.assertEqual(game["kind"], "card")
            self.assertIsNone(engine.answer("x")["correct"])


if __name__ == "__main__":
    unittest.main()
