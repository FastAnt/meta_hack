"""A minimal built-in harness that lets ANY OpenAI-compatible
LLM act as the content creator (provider=llm).

A bare model endpoint is not an agent — it has no hands. The
creator's job is narrow, though (study a project, write ONE
pack into <project>/.aat/content/, validate it), so the app
supplies the loop and four tools:

  list_files    explore the project / component docs
  read_file     read sources, SKILL.md, the games SDK
  write_file    ONLY inside <project>/.aat/ (enforced here,
                in code — not by asking nicely)
  run_validator tools/test_game.py on a deposited pack

Settings: AAT_CREATOR_LLM_BASE_URL (e.g.
https://api.deepseek.com/v1), AAT_CREATOR_LLM_API_KEY,
AAT_CREATOR_LLM_MODEL. Works with DeepSeek, OpenAI and local
servers alike.
"""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

MAX_STEPS = 40
MAX_READ = 20_000
MAX_RESULT = 6_000

TOOLS = [
    {"type": "function", "function": {
        "name": "list_files",
        "description": "List files under a directory (relative "
                       "to the project root, or an absolute path "
                       "inside the project or the app checkout).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a UTF-8 text file (project or app "
                       "checkout paths).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Write a file. ONLY allowed inside "
                       "<project>/.aat/ — anything else is "
                       "refused.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "run_validator",
        "description": "Run tools/test_game.py on a deposited "
                       "pack; returns its verdict.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}},
            "required": ["path"]}}},
]


class CreatorTools:
    def __init__(self, project_root: Path, app_root: Path):
        self.project = Path(project_root).resolve()
        self.app = Path(app_root).resolve()
        self.deposit = self.project / ".aat"

    def _inside(self, path: Path, root: Path) -> bool:
        try:
            return path.resolve().is_relative_to(root)
        except Exception:
            return False

    def _resolve(self, raw: str) -> Path:
        p = Path(raw)
        return p if p.is_absolute() else self.project / p

    # -- tools -----------------------------------------------------
    def list_files(self, path: str) -> str:
        p = self._resolve(path)
        if not (self._inside(p, self.project)
                or self._inside(p, self.app)):
            return "refused: outside the project and the app checkout"
        if not p.is_dir():
            return f"not a directory: {p}"
        names = []
        for f in sorted(p.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                names.append(str(f.relative_to(p)))
            if len(names) >= 300:
                break
        return "\n".join(names) or "(empty)"

    def read_file(self, path: str) -> str:
        p = self._resolve(path)
        if not (self._inside(p, self.project)
                or self._inside(p, self.app)):
            return "refused: outside the project and the app checkout"
        try:
            return p.read_text(encoding="utf-8",
                               errors="replace")[:MAX_READ]
        except Exception as exc:
            return f"read failed: {exc}"

    def write_file(self, path: str, content: str) -> str:
        p = self._resolve(path)
        if not self._inside(p, self.deposit):
            return ("refused: writes are only allowed inside "
                    f"{self.deposit}")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"written: {p} ({len(content)} chars)"

    def run_validator(self, path: str) -> str:
        p = self._resolve(path)
        if not self._inside(p, self.deposit):
            return "refused: validate files inside .aat only"
        script = self.app / "tools" / "test_game.py"
        try:
            out = subprocess.run(
                [sys.executable, str(script), str(p)],
                capture_output=True, text=True, timeout=300)
            text = (out.stdout + out.stderr).strip()
            return text[-MAX_RESULT:] or f"(exit {out.returncode})"
        except Exception as exc:
            return f"validator failed to run: {exc}"

    def call(self, name: str, args: dict) -> str:
        fn = {"list_files": self.list_files,
              "read_file": self.read_file,
              "write_file": self.write_file,
              "run_validator": self.run_validator}.get(name)
        if fn is None:
            return f"unknown tool: {name}"
        try:
            return str(fn(**args))[:MAX_RESULT]
        except TypeError as exc:
            return f"bad arguments for {name}: {exc}"


def _chat(base_url: str, api_key: str, model: str,
          messages: list, post=None) -> dict:
    url = base_url.rstrip("/") + "/chat/completions"
    body = {"model": model, "messages": messages,
            "tools": TOOLS, "tool_choice": "auto",
            "temperature": 0.4}
    if post is not None:
        return post(url, body, api_key)
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        snippet = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"LLM API error {exc.code}: {snippet}")
    except Exception as exc:
        raise RuntimeError(f"LLM API unreachable: {exc}")


SYSTEM = (
    "You are the Content Researcher for Agent-Aware Terminal. "
    "Use the tools: study the project, then create exactly ONE "
    "content pack and deposit it with write_file, then "
    "run_validator on it and fix any failure. Writes are only "
    "possible inside the project's .aat/ folder. When the pack "
    "validates, answer with one line: the file you created.")


def run_mission(base_url: str, api_key: str, model: str,
                mission: str, project_root, app_root,
                log=lambda _line: None, post=None,
                max_steps: int = MAX_STEPS) -> str:
    """Run the tool loop; returns the model's final line.
    Raises RuntimeError with the endpoint's own words on API
    failure (quota/auth surface unchanged)."""
    for name, value in (("base_url", base_url),
                        ("api_key", api_key), ("model", model)):
        if not value:
            raise RuntimeError(f"missing LLM creator setting: {name}")
    tools = CreatorTools(Path(project_root), Path(app_root))
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": mission}]
    for step in range(max_steps):
        resp = _chat(base_url, api_key, model, messages, post=post)
        try:
            msg = resp["choices"][0]["message"]
        except Exception:
            raise RuntimeError(f"unexpected LLM reply: "
                               f"{str(resp)[:300]}")
        messages.append(msg)
        calls = msg.get("tool_calls") or []
        if not calls:
            final = (msg.get("content") or "").strip()
            log(f"llm creator finished in {step + 1} steps")
            return final or "(no final message)"
        for call in calls:
            fn = call.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            result = tools.call(fn.get("name", ""), args)
            log(f"tool {fn.get('name')}: {result[:120]}")
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id", ""),
                "content": result})
    raise RuntimeError(f"step budget exhausted ({max_steps})")
