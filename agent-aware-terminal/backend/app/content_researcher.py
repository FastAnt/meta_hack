"""Content Researcher — a separate agent component.

The coding agent writes code. This component watches *what the
project is about* and *what the user just asked for*, and briefs
a SEPARATE content agent (in our setup: Muse, run outside the
terminal) that creates mini games and fact cards correlated with
that context — spectrogram puzzles for an audio engine, bottleneck
questions for a cloud app, cipher games for a crypto library.

Separation is the point:
- own lifecycle (profiled -> briefed -> drafting -> validating
  -> deposited / failed), recorded in the project,
- own skills (agent-aware-terminal/content-researcher/),
- own output: a hidden folder INSIDE the project,
  <project>/.aat/content/ (packs) and .aat/research/ (profile,
  brief, lifecycle log) — so other developers on the project
  find them later,
- it never edits production code; the coding agent never writes
  games.

This module is the deterministic half: project profiling and
lifecycle bookkeeping. The creative half is the external agent
following content-researcher/SKILL.md. API keys are the
operator's business (keys.sh / environment); nothing here reads
or stores secrets.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

SKIP_DIRS = {".git", ".aat", "__pycache__", ".venv", ".venv-build",
             "node_modules", "dist", "build", ".pytest_cache"}
TEXT_EXTS = {".md", ".txt", ".rst", ".py", ".js", ".ts", ".tsx",
             ".jsx", ".c", ".h", ".cpp", ".hpp", ".rs", ".go",
             ".java", ".json", ".toml", ".yaml", ".yml"}
MANIFESTS = ("README.md", "README.rst", "readme.md", "package.json",
             "pyproject.toml", "Cargo.toml", "go.mod", "pom.xml")

# Domain taxonomy: keyword -> (content angles). Keywords are
# matched against project text and, 3x weighted, the user's
# latest task. Angles seed games/facts; the agent still designs
# each scene itself.
TAXONOMY = [
    {"id": "audio-dsp", "label": "Audio & DSP",
     "keywords": ["audio", "dsp", "fft", "spectrogram", "waveform",
                  "sample rate", "samplerate", "filter", "eq",
                  "compressor", "limiter", "reverb", "delay",
                  "oscillator", "synth", "midi", "opus", "codec",
                  "amplifier", "amp sim", "gain", "distortion",
                  "neural codec", "sound", "frequency", "hertz"],
     "angles": ["read a spectrogram: which band carries the "
                "fundamental?", "wave math: period, phase, aliasing",
                "amplifier signal chain order", "codec trade-offs: "
                "bitrate vs latency", "Nyquist and sample rates"]},
    {"id": "cloud-data", "label": "Cloud & data transfer",
     "keywords": ["cloud", "latency", "throughput", "bandwidth",
                  "bottleneck", "queue", "kafka", "grpc", "http",
                  "websocket", "cdn", "load balancer", "shard",
                  "replication", "cache", "database", "sql",
                  "postgres", "redis", "kubernetes", "docker"],
     "angles": ["where is the bottleneck in this pipeline?",
                "latency budget arithmetic", "queue backpressure",
                "cache hit math", "sharding keys"]},
    {"id": "security-crypto", "label": "Security & cryptography",
     "keywords": ["crypto", "encrypt", "decrypt", "cipher", "aes",
                  "rsa", "hash", "sha", "hmac", "tls", "ssl",
                  "certificate", "auth", "oauth", "jwt", "password",
                  "nonce", "key exchange", "signature", "vulnerability",
                  "injection", "xss", "csrf"],
     "angles": ["which cipher mode leaks patterns?", "hash vs "
                "encryption vs signature", "nonce reuse disaster",
                "TLS handshake order", "threat-model the endpoint"]},
    {"id": "ml", "label": "Machine learning",
     "keywords": ["model", "training", "neural", "gradient",
                  "loss", "epoch", "dataset", "embedding",
                  "transformer", "attention", "inference",
                  "token", "classifier", "regression", "tensor"],
     "angles": ["gradient step direction", "overfitting curves",
                "attention intuition", "token budgets"]},
    {"id": "systems", "label": "Systems & performance",
     "keywords": ["thread", "async", "mutex", "lock", "memory",
                  "cache line", "cpu", "gpu", "kernel", "socket",
                  "buffer", "pipeline", "compiler", "runtime"],
     "angles": ["false sharing", "lock ordering", "buffer sizing",
                "pipeline stalls"]},
    {"id": "web-product", "label": "Web & product",
     "keywords": ["react", "component", "state", "router", "api",
                  "frontend", "backend", "session", "cookie",
                  "graphql", "rest", "ui", "ux"],
     "angles": ["request lifecycle", "state vs props", "REST "
                "statelessness", "session flow"]},
]

LIFECYCLE = ("profiled", "briefed", "drafting", "validating",
             "deposited", "failed")


def hidden_root(project) -> Path:
    return Path(project) / ".aat"


def content_dir(project) -> Path:
    return hidden_root(project) / "content"


def research_dir(project) -> Path:
    return hidden_root(project) / "research"


def _project_text(project: Path, limit_chars: int = 240_000) -> str:
    chunks = []
    total = 0
    names = []
    for name in MANIFESTS:
        f = project / name
        if f.is_file():
            try:
                chunks.append(f.read_text(errors="ignore"))
                total += len(chunks[-1])
            except Exception:
                pass
    for f in sorted(project.rglob("*")):
        if total >= limit_chars:
            break
        if any(part in SKIP_DIRS for part in f.parts):
            continue
        if not f.is_file():
            continue
        names.append(str(f.relative_to(project)))
        if f.suffix.lower() in TEXT_EXTS and f.stat().st_size < 60_000:
            try:
                text = f.read_text(errors="ignore")
            except Exception:
                continue
            chunks.append(text[:8000])
            total += min(len(text), 8000)
    chunks.append("\n".join(names[:400]))
    return "\n".join(chunks)


def profile_project(project, task_text: str = "") -> dict:
    """Deterministic domain profile of a project + latest task."""
    project = Path(project)
    text = _project_text(project).lower()
    task = (task_text or "").lower()
    domains = []
    for entry in TAXONOMY:
        score, evidence = 0, []
        for kw in entry["keywords"]:
            hits = text.count(kw)
            if hits:
                score += min(hits, 5)
                evidence.append(kw)
            if kw in task:
                score += 6  # the latest request dominates
                if kw not in evidence:
                    evidence.append(kw + " (task)")
        if score:
            domains.append({"id": entry["id"],
                            "label": entry["label"],
                            "score": score,
                            "evidence": evidence[:8],
                            "angles": entry["angles"]})
    domains.sort(key=lambda d: -d["score"])
    return {"project": str(project),
            "task": task_text,
            "generated_at": int(time.time()),
            "domains": domains[:4]}


def _lifecycle_path(project) -> Path:
    return research_dir(project) / "lifecycle.jsonl"


def advance_lifecycle(project, state: str, note: str = ""):
    if state not in LIFECYCLE:
        raise ValueError(f"unknown lifecycle state {state!r}")
    rdir = research_dir(project)
    rdir.mkdir(parents=True, exist_ok=True)
    record = {"state": state, "at": int(time.time()), "note": note}
    with _lifecycle_path(project).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    (rdir / "lifecycle.json").write_text(json.dumps(record, indent=2))
    return record


def lifecycle_state(project) -> str | None:
    path = research_dir(project) / "lifecycle.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text()).get("state")
    except Exception:
        return None


def write_brief(project, task_text: str = "") -> dict:
    """Profile + write brief.md for the external content agent."""
    project = Path(project)
    profile = profile_project(project, task_text)
    rdir = research_dir(project)
    rdir.mkdir(parents=True, exist_ok=True)
    content_dir(project).mkdir(parents=True, exist_ok=True)
    (rdir / "domain_profile.json").write_text(
        json.dumps(profile, indent=2))
    lines = [
        "# Content research brief",
        "",
        f"Project: `{project}`",
        f"Latest user task: {task_text or '(none given)'}",
        "",
        "## Detected domains (strongest first)",
    ]
    for d in profile["domains"]:
        lines.append(f"- **{d['label']}** (`{d['id']}`, score "
                     f"{d['score']}; evidence: "
                     f"{', '.join(d['evidence'])})")
        for angle in d["angles"]:
            lines.append(f"  - angle: {angle}")
    lines += [
        "",
        "## Your job (separate content agent)",
        "",
        "Create ONE mini game (quiz) or ONE fact card correlated "
        "with the strongest domain and the latest task. Follow "
        "`agent-aware-terminal/content-researcher/SKILL.md` and "
        "`games-base/AGENTS.md`. The scene must depict the "
        "subject of the question. Deposit into "
        f"`{content_dir(project)}`, validate with "
        "`tools/test_game.py`, look at the previews, then mark "
        "the lifecycle `deposited`. Never edit production code.",
        "",
    ]
    (rdir / "brief.md").write_text("\n".join(lines))
    advance_lifecycle(project, "profiled")
    advance_lifecycle(project, "briefed")
    return profile


def summarize_outputs(project) -> dict:
    """What the content agent has deposited so far."""
    from backend.app.content_engine import scan_packs
    cdir = content_dir(project)
    packs, errors = scan_packs([cdir]) if cdir.is_dir() else ([], [])
    return {"content_dir": str(cdir),
            "packs": [{"id": p.get("id"),
                       "kind": p.get("kind", "quiz"),
                       "file": Path(p.get("_source", "")).name}
                      for p in packs],
            "errors": errors}
