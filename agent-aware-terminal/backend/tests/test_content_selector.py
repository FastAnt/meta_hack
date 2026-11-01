"""Context-based selection: vector store, ranking, reactions,
generation decisions. Uses a deterministic fake embedder so the
math is asserted exactly; the ONNX backend has its own smoke
test (skipped without the runtime/model)."""
import json
import math
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from backend.app import content_engine as ce
from backend.app import embeddings as emb
from backend.app import vector_store as vs
from backend.app.content_selector import ContentSelector


class FakeEmbedder:
    """Maps known phrases to 2-D unit vectors."""
    model_id = "fake-2d"
    VECTORS = {
        "audio": [1.0, 0.0],
        "codec": [0.99, 0.14],
        "math": [0.0, 1.0],
        "prime": [0.14, 0.99],
    }

    def embed(self, texts, kind="doc"):
        out = []
        for text in texts:
            vec = [0.0, 0.0]
            for key, v in self.VECTORS.items():
                if key in text.lower():
                    vec[0] += v[0]
                    vec[1] += v[1]
            n = math.hypot(*vec) or 1.0
            out.append([vec[0] / n, vec[1] / n])
        return out


def _pack(pid, ptype, title, source_hint=None):
    raw = {"id": pid, "type": ptype, "title": title,
           "topic": "T", "body": "body text"}
    if ptype == "game":
        raw.update({"choices": [{"id": "a", "label": "A"}],
                    "answer": "a", "explanation": "e"})
    return raw


class SelectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.proj = root / "proj"
        (self.proj / ".aat" / "content").mkdir(parents=True)
        base = root / "base"
        base.mkdir()
        self.packs = [
            _pack("g-audio", "fact", "audio codec waves"),
            _pack("g-math", "fact", "math prime numbers"),
            _pack("g-misc", "game", "unrelated buttons"),
        ]
        for pack in self.packs:
            (base / f"{pack['id']}.json").write_text(
                json.dumps(pack))
        proj_pack = _pack("p-audio", "fact", "audio codec project")
        (self.proj / ".aat" / "content" / "p.json").write_text(
            json.dumps(proj_pack))
        self.engine = ce.ContentEngine(
            [base, self.proj / ".aat" / "content"], seed=3)
        self.store = vs.VectorStore(root / "idx.db")
        self.sel = ContentSelector(
            self.engine, self.store, FakeEmbedder(), None,
            lambda tag, msg: None,
            project_path=self.proj,
            project_content_dir=self.proj / ".aat" / "content")
        self.sel.index()

    def tearDown(self):
        self.tmp.cleanup()

    def test_index_covers_catalogue(self):
        self.assertEqual(self.sel.indexed, 4)
        self.assertIn("p-audio", self.sel.project_ids)

    def test_project_pool_wins(self):
        picked = self.sel.pick("audio codec work", str(self.proj),
                               set())
        self.assertEqual(picked[0], "p-audio")
        self.assertEqual(picked[1], "project")

    def test_global_fallback_and_exclusion(self):
        picked = self.sel.pick("math prime work", str(self.proj),
                               {"p-audio"})
        self.assertEqual(picked[0], "g-math")
        self.assertEqual(picked[1], "global")

    def test_dislike_penalizes_a_tie(self):
        self.store.add_reaction("g-math", str(self.proj), "dislike")
        picked = self.sel.pick("audio math", str(self.proj),
                               {"p-audio"})
        self.assertEqual(picked[0], "g-audio")

    def test_like_boosts_a_tie(self):
        self.store.add_reaction("g-math", str(self.proj), "like")
        picked = self.sel.pick("audio math", str(self.proj),
                               {"p-audio"})
        self.assertEqual(picked[0], "g-math")

    def test_relevance_beats_reactions(self):
        # A strong topical match still wins over a like elsewhere:
        # reactions nudge, they do not override relevance.
        self.store.add_reaction("g-math", str(self.proj), "like")
        picked = self.sel.pick("audio codec work", str(self.proj),
                               {"p-audio"})
        self.assertEqual(picked[0], "g-audio")

    def test_generation_request_written(self):
        req = self.sel.evaluate_generation(
            "audio codec", str(self.proj),
            best_project_sim=0.99, best_global_sim=0.20)
        # project pool fits fine (0.99) -> covered by global? no:
        # project matches, so no generation despite weak global
        self.assertIsNone(req)
        req = self.sel.evaluate_generation(
            "totally new domain", str(self.proj),
            best_project_sim=0.10, best_global_sim=0.20)
        self.assertIsNotNone(req)
        path = (self.proj / ".aat" / "research"
                / "generation_requests.jsonl")
        rows = [json.loads(x) for x in
                path.read_text().splitlines()]
        self.assertEqual(rows[0]["want_type"], "game")
        # cooldown: an immediate second request is suppressed
        self.assertIsNone(self.sel.evaluate_generation(
            "totally new domain", str(self.proj), 0.10, 0.20))


class HashingEmbedderTests(unittest.TestCase):
    def test_related_texts_are_closer(self):
        e = emb.HashingEmbedder()
        a, b, c = e.embed([
            "audio codec sampling rate spectrogram",
            "audio codec bitrate spectrogram waves",
            "medieval poetry manuscripts history"])
        self.assertGreater(emb.cosine(a, b), emb.cosine(a, c))
        self.assertAlmostEqual(emb.cosine(a, a), 1.0, places=5)

    def test_store_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = vs.VectorStore(pathlib.Path(tmp) / "i.db")
            pack = _pack("x1", "fact", "T")
            store.upsert_item(pack, "global", "m", [0.5, 0.5],
                              "hash")
            self.assertEqual(store.get_vector("x1"), [0.5, 0.5])
            self.assertEqual(store.all_vectors("m")[0][0], "x1")
            store.add_reaction("x1", "p", "like")
            self.assertEqual(store.reaction_ids("p", "like"),
                             ["x1"])


class GemmaSmokeTests(unittest.TestCase):
    def test_gemma_semantics(self):
        try:
            import onnxruntime  # noqa: F401
            import tokenizers  # noqa: F401
        except Exception:
            self.skipTest("onnxruntime/tokenizers not installed")
        embedder = emb.GemmaEmbedder(pathlib.Path.home() / (
            ".local/share/agent-aware-terminal/models/"
            "embeddinggemma2"))
        if not embedder.ensure_ready():
            self.skipTest("model unavailable (offline?)")
        a, b, c = embedder.embed([
            "audio codec sampling and spectrograms",
            "how mp3 compression removes masked frequencies",
            "medieval poetry and manuscript history"], kind="doc")
        q = embedder.embed(["audio codec"], kind="query")[0]
        self.assertEqual(len(a), 256)
        self.assertGreater(emb.cosine(a, b), emb.cosine(a, c))
        self.assertGreater(emb.cosine(q, a), emb.cosine(q, c))


if __name__ == "__main__":
    unittest.main()
