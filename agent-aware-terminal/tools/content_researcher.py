#!/usr/bin/env python3
"""Content Researcher CLI — the handoff point between the app
and the separate content agent.

    python3 tools/content_researcher.py profile \
        --project ~/my-audio-app \
        --task "fix spectrogram noise in the neural codec"
    # -> writes <project>/.aat/research/{domain_profile.json,
    #    brief.md} and prints the detected domains.

    python3 tools/content_researcher.py mark \
        --project ~/my-audio-app --state drafting
    python3 tools/content_researcher.py validate --project ~/my-audio-app

The content agent (separate Muse session, see
content-researcher/SKILL.md) reads the brief, creates a game or
fact card in <project>/.aat/content/, validates it, and marks
the lifecycle deposited. This tool never calls a model and
never touches API keys.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app import content_researcher as cr  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("profile", help="profile project + write brief")
    p.add_argument("--project", default=".")
    p.add_argument("--task", default="",
                   help="the user's latest request to the coding agent")
    m = sub.add_parser("mark", help="advance the lifecycle")
    m.add_argument("--project", default=".")
    m.add_argument("--state", required=True, choices=cr.LIFECYCLE)
    m.add_argument("--note", default="")
    v = sub.add_parser("validate", help="scan deposited content")
    v.add_argument("--project", default=".")
    args = ap.parse_args()

    if args.cmd == "profile":
        profile = cr.write_brief(args.project, args.task)
        print(f"project: {profile['project']}")
        print(f"brief:   {cr.research_dir(args.project) / 'brief.md'}")
        print(f"drop content into: {cr.content_dir(args.project)}")
        for d in profile["domains"]:
            print(f"  domain {d['id']:<16} score={d['score']:<3} "
                  f"evidence: {', '.join(d['evidence'][:5])}")
        if not profile["domains"]:
            print("  no domains detected — brief asks for a "
                  "project-introduction fact card")
        return 0
    if args.cmd == "mark":
        rec = cr.advance_lifecycle(args.project, args.state,
                                   args.note)
        print(f"lifecycle -> {rec['state']}")
        return 0
    if args.cmd == "validate":
        out = cr.summarize_outputs(args.project)
        cr.advance_lifecycle(args.project, "validating")
        for pack in out["packs"]:
            print(f"  OK pack {pack['id']} ({pack['kind']}) "
                  f"from {pack['file']}")
        for err in out["errors"]:
            print(f"  ERROR {err}")
        ok = bool(out["packs"]) and not out["errors"]
        cr.advance_lifecycle(
            args.project, "deposited" if ok else "failed",
            f"{len(out['packs'])} packs, {len(out['errors'])} errors")
        print(f"lifecycle -> {'deposited' if ok else 'failed'}")
        return 0 if ok else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
