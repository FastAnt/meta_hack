import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.app import embeddings as emb  # noqa: E402


class WorkerDegradationTests(unittest.TestCase):
    def test_dead_worker_degrades_to_hashing(self):
        e = emb.SubprocessGemmaEmbedder(Path("/nonexistent-model"))

        def boom():
            raise RuntimeError("no such process")

        e._spawn = boom
        vecs = e.embed(["hello terminal", "agent working"], "doc")
        self.assertEqual(len(vecs), 2)
        self.assertEqual(len(vecs[0]), len(vecs[1]))
        self.assertGreater(len(vecs[0]), 0)
        # and it stays degraded without retrying the spawn
        vecs = e.embed(["again"], "query")
        self.assertEqual(len(vecs), 1)


if __name__ == "__main__":
    unittest.main()
