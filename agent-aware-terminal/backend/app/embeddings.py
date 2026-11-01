"""Local text embeddings for content retrieval.

Two backends behind one interface:

- `OnnxEmbedder` — all-MiniLM-L6-v2 (int8 ONNX, ~23 MB,
  downloaded once into the data dir) with a pure-Python
  WordPiece tokenizer. Real semantic vectors, fully local.
  Other local models (e.g. Google's EmbeddingGemma) can slot
  into the same interface later.
- `HashingEmbedder` — hashed word/char n-gram TF vectors.
  Zero dependencies, always available; a lexical baseline that
  is honestly weaker on paraphrases.

Vectors from different backends are never mixed: every stored
vector carries its model id and ranking only compares
same-model vectors.
"""
from __future__ import annotations

import hashlib
import math
import re
import struct
import threading
import urllib.request
from pathlib import Path

MODEL_MINILM = "minilm-l6-onnx"
MODEL_HASHING = "hashing-384"

_HF = "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main"


def pack_text(pack: dict) -> str:
    parts = [pack.get("title", ""), pack.get("topic", ""),
             pack.get("kicker", ""), pack.get("question", ""),
             pack.get("body", ""), pack.get("hint", ""),
             pack.get("explanation", "")]
    parts += [c.get("label", "") for c in pack.get("choices") or []]
    return " ".join(p for p in parts if p).strip()


def text_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def pack_vector_bytes(vec) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack_vector(data: bytes) -> list[float]:
    n = len(data) // 4
    return list(struct.unpack(f"<{n}f", data[:n * 4]))


def cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if not na or not nb:
        return 0.0
    return dot / (na * nb)


def centroid(vectors) -> list[float] | None:
    vectors = [v for v in vectors if v]
    if not vectors:
        return None
    dim = len(vectors[0])
    out = [0.0] * dim
    for v in vectors:
        for i, x in enumerate(v):
            out[i] += x
    out = [x / len(vectors) for x in out]
    norm = math.sqrt(sum(x * x for x in out))
    return [x / norm for x in out] if norm else out


# ------------------------------------------------------------------
# Hashing fallback
# ------------------------------------------------------------------

class HashingEmbedder:
    model_id = MODEL_HASHING
    dim = 384

    def ready(self) -> bool:
        return True

    def embed(self, texts):
        return [self._one(t) for t in texts]

    def _one(self, text: str):
        vec = [0.0] * self.dim
        words = re.findall(r"[a-z0-9]+", text.lower())
        grams = list(words)
        grams += [f"{a}_{b}" for a, b in zip(words, words[1:])]
        for w in words:
            grams += [w[i:i + 3] for i in range(max(len(w) - 2, 0))]
        for g in grams:
            h = int(hashlib.md5(g.encode()).hexdigest()[:8], 16)
            vec[h % self.dim] += 1.0
        norm = math.sqrt(sum(x * x for x in vec))
        return [x / norm for x in vec] if norm else vec


# ------------------------------------------------------------------
# ONNX MiniLM
# ------------------------------------------------------------------

_WORD = re.compile(r"[a-z0-9]+|[^\sa-z0-9]")


class _WordPiece:
    def __init__(self, vocab_path: Path):
        self.vocab = {}
        for i, tok in enumerate(
                vocab_path.read_text(encoding="utf-8").splitlines()):
            self.vocab[tok] = i
        self.unk = self.vocab.get("[UNK]", 100)

    def encode(self, text: str, max_len: int = 128):
        ids = [self.vocab.get("[CLS]", 101)]
        for word in _WORD.findall(text.lower()):
            if word in self.vocab:
                ids.append(self.vocab[word])
                continue
            pieces, rest, ok = [], word, True
            while rest:
                hit = None
                for end in range(len(rest), 0, -1):
                    piece = rest[:end] if len(pieces) == 0 \
                        else "##" + rest[:end]
                    if piece in self.vocab:
                        hit = (piece, end)
                        break
                if hit is None:
                    ok = False
                    break
                pieces.append(self.vocab[hit[0]])
                rest = rest[hit[1]:]
            ids.extend(pieces if ok else [self.unk])
        ids.append(self.vocab.get("[SEP]", 102))
        return ids[:max_len]


class OnnxEmbedder:
    model_id = MODEL_MINILM
    dim = 384

    def __init__(self, model_dir: Path):
        self.dir = Path(model_dir)
        self._session = None
        self._tok = None
        self._lock = threading.Lock()
        self._failed = False

    def _files(self):
        import platform
        names = ["model_qint8_arm64.onnx" if "arm" in
                 platform.machine().lower() else "model_quint8_avx2.onnx",
                 "model.onnx"]
        for n in names:
            if (self.dir / n).exists():
                return self.dir / n
        return self.dir / names[0]

    def _download(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        model = self._files()
        if not model.exists():
            urllib.request.urlretrieve(
                f"{_HF}/onnx/{model.name}", model)
        vocab = self.dir / "vocab.txt"
        if not vocab.exists():
            urllib.request.urlretrieve(f"{_HF}/vocab.txt", vocab)

    def ensure_ready(self) -> bool:
        if self._session is not None:
            return True
        if self._failed:
            return False
        with self._lock:
            if self._session is not None:
                return True
            try:
                import onnxruntime as ort
                self._download()
                self._tok = _WordPiece(self.dir / "vocab.txt")
                self._session = ort.InferenceSession(
                    str(self._files()),
                    providers=["CPUExecutionProvider"])
                return True
            except Exception:
                self._failed = True
                return False

    def ready(self) -> bool:
        return self._session is not None

    def embed(self, texts):
        import numpy as np
        if not self.ensure_ready():
            raise RuntimeError("onnx embedder unavailable")
        out = []
        for text in texts:
            ids = self._tok.encode(text)
            mask = [1] * len(ids)
            inputs = {
                "input_ids": np.array([ids], dtype=np.int64),
                "attention_mask": np.array([mask], dtype=np.int64),
                "token_type_ids": np.zeros((1, len(ids)), dtype=np.int64),
            }
            hidden = self._session.run(None, inputs)[0][0]
            vec = [0.0] * self.dim
            for row, m in zip(hidden, mask):
                if m:
                    for i, x in enumerate(row):
                        vec[i] += float(x)
            vec = [x / max(sum(mask), 1) for x in vec]
            norm = math.sqrt(sum(x * x for x in vec))
            out.append([x / norm for x in vec] if norm else vec)
        return out


def make_embedder(data_dir: Path):
    """ONNX when the runtime is importable, else the hashing
    baseline. ONNX initialisation (download + load) happens
    lazily on first embed, in the caller's thread."""
    try:
        import onnxruntime  # noqa: F401
        return OnnxEmbedder(Path(data_dir) / "models" / "minilm-l6")
    except Exception:
        return HashingEmbedder()
