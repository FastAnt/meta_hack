"""Standalone entry for the embedding worker process
(source mode; the frozen binary re-enters itself with
AAT_EMBED_WORKER=1 instead)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.app.embeddings import run_worker  # noqa: E402

if __name__ == "__main__":
    run_worker(sys.stdin, sys.stdout)
