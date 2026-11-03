"""Live pull-request pool for the content pane.

The "PRs" category is not generated content: it is the open
pull requests of the current project's GitHub remote, read
with the `gh` CLI and shaped into cards. Everything on a card
is measured from gh's own JSON — the PR description, the
changed-file list, comment/review counts and the latest
discussion excerpt. Nothing is summarised by a model and
no number is invented.

fetch_pull_requests returns (packs, error): error is a short
machine string ("not-a-git-repo", "gh-missing",
"gh-failed: …") the UI turns into an empty-state explanation.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

_GH_FIELDS = ("number,title,url,author,body,createdAt,updatedAt,"
              "additions,deletions,changedFiles,files,comments,"
              "latestReviews,reviewDecision,isDraft,headRefName,"
              "baseRefName")


def _excerpt(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def pr_to_pack(raw: dict) -> dict:
    """One gh PR object -> one content card."""
    number = raw.get("number")
    comments = raw.get("comments") or []
    reviews = raw.get("latestReviews") or []
    review_bits = []
    for rev in reviews:
        login = (rev.get("author") or {}).get("login") or "?"
        review_bits.append(f"@{login}: {rev.get('state') or '?'}")
    description = _excerpt(raw.get("body") or "", 420) \
        or "(no description)"
    discussion = []
    if comments:
        discussion.append(
            f"{len(comments)} comment"
            f"{'s' if len(comments) != 1 else ''}")
    if review_bits:
        discussion.append("reviews: " + ", ".join(review_bits[:3]))
    if raw.get("reviewDecision"):
        discussion.append(
            f"decision: {raw['reviewDecision']}")
    body = description
    if discussion:
        body += "\n\nDiscussion — " + " · ".join(discussion) + "."
    if comments:
        last = comments[-1]
        login = (last.get("author") or {}).get("login") or "?"
        body += (f"\nLatest, @{login}: "
                 f"“{_excerpt(last.get('body') or '', 140)}”")
    author = (raw.get("author") or {}).get("login") or "?"
    return {
        "type": "pr",
        "id": f"pr-{number}",
        "number": number,
        "kicker": "PULL REQUEST" + (" · DRAFT" if raw.get("isDraft")
                                    else ""),
        "title": f"#{number} {raw.get('title') or ''}".strip(),
        "body": body,
        "link": raw.get("url") or "",
        "author": author,
        "head": raw.get("headRefName") or "",
        "base": raw.get("baseRefName") or "",
        "additions": raw.get("additions") or 0,
        "deletions": raw.get("deletions") or 0,
        "changed_files": raw.get("changedFiles") or 0,
        "files": [f.get("path") for f in raw.get("files") or []
                  if f.get("path")],
        "updated": raw.get("updatedAt") or "",
    }


def fetch_pull_requests(repo_dir: str, limit: int = 30,
                        runner=None) -> tuple[list, str | None]:
    """Open PRs of the repo at repo_dir, as cards."""
    root = pathlib.Path(repo_dir or "")
    if not (root / ".git").exists():
        return [], "not-a-git-repo"
    if shutil.which("gh") is None:
        return [], "gh-missing"
    run = runner or subprocess.run
    try:
        proc = run(["gh", "pr", "list", "--state", "open",
                    "--limit", str(limit),
                    "--json", _GH_FIELDS],
                   cwd=str(root), capture_output=True, text=True,
                   timeout=45)
    except Exception as exc:
        return [], f"gh-failed: {exc}"[:120]
    if getattr(proc, "returncode", 1) != 0:
        err = (getattr(proc, "stderr", "") or "").strip()
        return [], ("gh-failed: "
                    + (err.splitlines()[0] if err else "?"))[:120]
    try:
        raws = json.loads(proc.stdout or "[]")
    except Exception:
        return [], "gh-failed: unparsable output"
    return [pr_to_pack(r) for r in raws], None
