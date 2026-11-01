"""Local text embeddings for content retrieval.

Primary backend: Google EmbeddingGemma-2 (Apache 2.0,
ungated) via the int8-quantized ONNX export from
onnx-community — downloaded once into the data dir. Mean
pooling, Matryoshka truncation to 256 dims, retrieval
prompts (`task: search result | query:` for queries,
`title: … | text:` for documents) exactly as the model was
trained. Runs fully locally on CPU.

Fallback: `HashingEmbedder` — hashed word/char n-gram TF
vectors, zero dependencies, always available; a lexical
baseline that is honestly weaker on paraphrases. If the
ONNX model cannot be fetched or loaded, the selector swaps
to the fallback instead of breaking.

Vectors from different backends are never mixed: every
stored vector carries its model id and ranking only compares
same-model vectors.
"""
from __future__ import annotations

import hashlib
import math
import os
import sys
import re
import struct
import threading
import urllib.request
from pathlib import Path

MODEL_GEMMA = "embeddinggemma2-q8-256"
MODEL_HASHING = "hashing-384"
OUTPUT_DIM = 256

_REPO = "https://huggingface.co/onnx-community/embeddinggemma-2-ONNX/resolve/main"
_FILES = ("onnx/model_quantized.onnx",
          "onnx/model_quantized.onnx_data",
          "tokenizer.json")
QUERY_PREFIX = "task: search result | query: "


def pack_text(pack: dict) -> str:
    parts = [pack.get("title", ""), pack.get("topic", ""),
             pack.get("kicker", ""), pack.get("question", ""),
             pack.get("body", ""), pack.get("hint", ""),
             pack.get("explanation", "")]
    parts += [c.get("label", "") for c in pack.get("choices") or []]
    return " ".join(p for p in parts if p).strip()


def doc_text(pack: dict) -> str:
    """The document format EmbeddingGemma was trained with."""
    return f"title: {pack.get('title', '') or 'none'} | text: " \
           f"{pack_text(pack)}"


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

    def embed(self, texts, kind="doc"):
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
# EmbeddingGemma-2 (quantized ONNX)
# ------------------------------------------------------------------

class GemmaEmbedder:
    model_id = MODEL_GEMMA
    dim = OUTPUT_DIM

    def __init__(self, model_dir: Path):
        self.dir = Path(model_dir)
        self._session = None
        self._tok = None
        self._inputs: set[str] = set()
        self._lock = threading.Lock()
        self._failed = False

    def _download(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        for name in _FILES:
            target = self.dir / Path(name).name
            if not target.exists():
                urllib.request.urlretrieve(f"{_REPO}/{name}", target)

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
                from tokenizers import Tokenizer
                self._download()
                self._tok = Tokenizer.from_file(
                    str(self.dir / "tokenizer.json"))
                self._session = ort.InferenceSession(
                    str(self.dir / "model_quantized.onnx"),
                    providers=["CPUExecutionProvider"])
                self._inputs = {i.name for i in
                                self._session.get_inputs()}
                return True
            except Exception:
                self._failed = True
                return False

    def ready(self) -> bool:
        return self._session is not None

    def embed(self, texts, kind="doc"):
        import numpy as np
        if not self.ensure_ready():
            raise RuntimeError("gemma embedder unavailable")
        if kind == "query":
            texts = [QUERY_PREFIX + t for t in texts]
        out = []
        for i in range(0, len(texts), 32):
            batch = list(texts[i:i + 32])
            encs = self._tok.encode_batch(batch)
            width = max(len(e.ids) for e in encs)
            ids = np.zeros((len(encs), width), dtype=np.int64)
            mask = np.zeros((len(encs), width), dtype=np.int64)
            for row, e in enumerate(encs):
                ids[row, :len(e.ids)] = e.ids[:512]
                mask[row, :len(e.ids)] = 1
            feed = {}
            if "input_ids" in self._inputs:
                feed["input_ids"] = ids
            if "attention_mask" in self._inputs:
                feed["attention_mask"] = mask
            if "token_type_ids" in self._inputs:
                feed["token_type_ids"] = np.zeros_like(ids)
            # Multimodal export: absent modalities are fed as
            # empty feature tensors.
            for opt in ("image_features", "video_features",
                        "audio_features"):
                if opt in self._inputs:
                    feed[opt] = np.zeros((0, 512), dtype=np.float32)
            names = [o.name for o in self._session.get_outputs()]
            results = self._session.run(None, feed)
            if "sentence_embedding" in names:
                pooled = results[names.index("sentence_embedding")]
                for row in range(len(encs)):
                    vec = [float(x) for x in
                           pooled[row][:OUTPUT_DIM]]
                    norm = math.sqrt(sum(x * x for x in vec))
                    out.append([x / norm for x in vec]
                               if norm else vec)
                continue
            hidden = results[0]
            for row in range(len(encs)):
                m = mask[row]
                total = max(int(m.sum()), 1)
                vec = [0.0] * OUTPUT_DIM
                for t in range(width):
                    if m[t]:
                        for j in range(OUTPUT_DIM):
                            vec[j] += float(hidden[row, t, j])
                vec = [x / total for x in vec]
                norm = math.sqrt(sum(x * x for x in vec))
                out.append([x / norm for x in vec] if norm else vec)
        return out


def run_worker(stdin, stdout) -> None:
    """Embedding worker loop: JSON lines in, JSON lines out.
    Runs in a child process so a native onnxruntime abort
    (observed: C++ terminate inside a Split kernel) can never
    take the terminal down with it."""
    import json as _json
    impl = None
    for line in stdin:
        try:
            req = _json.loads(line)
            if impl is None:
                impl = GemmaEmbedder(
                    Path(req["model_dir"]))
            vecs = impl.embed(req["texts"], req.get("kind", "doc"))
            stdout.write(_json.dumps({"vectors": vecs}) + "\n")
        except Exception as exc:
            try:
                stdout.write(_json.dumps(
                    {"error": f"{type(exc).__name__}: {exc}"}) + "\n")
            except Exception:
                return
        stdout.flush()


class SubprocessGemmaEmbedder:
    """Gemma behind a process boundary. Any worker failure —
    including a hard native abort the parent cannot even see
    as an exception — degrades to the hashing baseline for the
    rest of the session instead of killing the app."""

    model_id = MODEL_GEMMA
    dim = OUTPUT_DIM

    def __init__(self, model_dir: Path):
        self.model_dir = Path(model_dir)
        self._proc = None
        self._fallback = None
        self._lock = threading.Lock()

    def ready(self) -> bool:
        return self._fallback is None

    def _spawn(self):
        import subprocess
        import sys as _sys
        env = dict(os.environ)
        env["AAT_EMBED_WORKER"] = "1"
        if getattr(_sys, "frozen", False):
            cmd = [_sys.executable]
        else:
            cmd = [_sys.executable,
                   str(Path(__file__).with_name("embed_worker.py"))]
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env, text=True)

    def _degrade(self, why: str):
        if self._fallback is None:
            print(f"[embeddings] worker lost ({why}); "
                  "hashing baseline takes over", file=sys.stderr)
        self._fallback = HashingEmbedder()
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass
            self._proc = None

    def embed(self, texts, kind="doc"):
        if self._fallback is not None:
            return self._fallback.embed(texts, kind)
        import json as _json
        with self._lock:
            try:
                if self._proc is None or self._proc.poll() is not None:
                    self._spawn()
                self._proc.stdin.write(_json.dumps({
                    "model_dir": str(self.model_dir),
                    "texts": list(texts), "kind": kind}) + "\n")
                self._proc.stdin.flush()
                line = self._proc.stdout.readline()
                if not line:
                    raise RuntimeError("worker closed the pipe")
                resp = _json.loads(line)
                if "error" in resp:
                    raise RuntimeError(resp["error"])
                return resp["vectors"]
            except Exception as exc:
                self._degrade(str(exc)[:100])
                return self._fallback.embed(texts, kind)


def make_embedder(data_dir: Path):
    """EmbeddingGemma-2 when its runtime is importable, else
    the hashing baseline. Model download/load is lazy."""
    try:
        import onnxruntime  # noqa: F401
        import tokenizers  # noqa: F401
        return SubprocessGemmaEmbedder(
            Path(data_dir) / "models" / "embeddinggemma2")
    except Exception:
        return HashingEmbedder()
