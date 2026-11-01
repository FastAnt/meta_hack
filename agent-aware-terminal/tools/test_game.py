#!/usr/bin/env python3
"""Test content games the way an authoring agent should.

Games are Python modules (GAME + paint) or legacy JSON packs.
For every game (file or directory; default games-base/):

1. load + meta validation (the engine's own rules),
2. a scripted playthrough against the real engine — the
   correct target must score, every other target must fail,
   cards must not score,
3. scene sanity for JSON scenes (bounds / overlap),
4. preview renders (PNG, dark + light, two time frames)
   written to --out so the author can LOOK at the result —
   identical frames mean the scene does not move.

Each game is tested in a worker subprocess with a timeout, so
a crashing or hanging game fails cleanly instead of taking
the tester down. Run with the system python3 (needs pycairo):

    python3 tools/test_game.py games-base/
    python3 tools/test_game.py my_new_game.py --out /tmp/previews

Exit code 1 when anything fails. See games-base/AGENTS.md.
"""
import argparse
import json
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DARK = {"square": (0.969, 0.973, 0.973), "text": (0.973, 0.976, 0.976),
        "muted": (0.541, 0.561, 0.596), "bg": (0.043, 0.047, 0.055)}
LIGHT = {"square": (0.078, 0.082, 0.09), "text": (0.043, 0.047, 0.055),
         "muted": (0.384, 0.4, 0.427), "bg": (0.976, 0.976, 0.98)}
W, H = 880, 620


# ---------------------------------------------------------------------------
# Worker: everything for ONE file, prints one JSON result line.
# ---------------------------------------------------------------------------

def load_one(path):
    from backend.app import content_engine as ce
    path = pathlib.Path(path)
    if path.suffix == ".py":
        pack, err = ce.load_py_pack(path)
        if pack is None:
            return None, err
        pack["_source"] = str(path)
        return ce.validate_pack(pack, str(path))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"unreadable: {exc}"
    return ce.validate_pack(raw, str(path))


def scene_problems(pack):
    from backend.app import content_engine as ce
    problems = []
    scene = pack.get("scene") or {}
    if scene.get("type") == "diagram":
        nodes = scene.get("nodes") or []
        for n in nodes:
            if not (0 <= float(n.get("x", -1)) <= 1
                    and 0 <= float(n.get("y", -1)) <= 1):
                problems.append(f"node {n.get('id')} out of bounds")
        rects = ce.node_rects(nodes, W, H)
        ids = list(rects)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                ax, ay, aw, ah = rects[a]
                bx, by, bw, bh = rects[b]
                ox = max(0, min(ax + aw, bx + bw) - max(ax, bx))
                oy = max(0, min(ay + ah, by + bh) - max(ay, by))
                if ox * oy > 0.04 * min(aw * ah, bw * bh):
                    problems.append(f"nodes {a} and {b} overlap")
    if scene.get("type") == "orbits":
        radii = ce.ring_radii(W, H, int(scene.get("rings", 0)))
        if any(radii[i + 1] - radii[i] < 12
               for i in range(len(radii) - 1)):
            problems.append("orbit rings too tight to click")
    return problems


def playthrough(pack):
    from backend.app.content_engine import ContentEngine, scene_targets
    problems = []
    src = pathlib.Path(pack.get("_source") or "pack.json")
    with tempfile.TemporaryDirectory() as tmp:
        dst = pathlib.Path(tmp, src.name)
        if src.suffix == ".py":
            dst.write_bytes(src.read_bytes())
        else:
            dst.write_text(json.dumps(
                {k: v for k, v in pack.items()
                 if not k.startswith("_")}))
        engine = ContentEngine([tmp], seed=1)
        game = engine.next_game()
        if game is None:
            return ["engine would not serve the game"]
        targets = [c["id"] for c in game.get("choices") or []] + \
            scene_targets(game)
        from backend.app.content_engine import resolve_type
        if resolve_type(game) != "game":
            out = engine.answer(targets[0] if targets else "x")
            if out["correct"] is not None:
                problems.append("non-game content must not score")
            return problems
        if callable(game.get("_target_at")):
            # Probe the click surface: the answer must be
            # reachable, and there must be a real choice to make.
            from backend.app.content_engine import GameView
            found = set()
            view = GameView(W, H, 0.0, {})
            for gx in range(0, W, 22):
                for gy in range(0, H, 22):
                    hit = game["_target_at"](view, gx, gy)
                    if hit:
                        found.add(hit)
            if game["answer"] not in found | set(targets):
                problems.append("answer is not reachable by clicking")
            if len(found | set(targets)) < 2:
                problems.append("quiz needs at least 2 answerable "
                                "targets")
        out = engine.answer(game["answer"])
        if out["correct"] is not True:
            problems.append("the correct target does not score")
        if not out["explanation"]:
            problems.append("no explanation after a correct answer")
        for target in set(targets) - {game["answer"]}:
            again = ContentEngine([tmp], seed=1)
            again.next_game()
            if again.answer(target)["correct"] is not False:
                problems.append(f"wrong target {target} scores")
                break
    return problems


def render_frames(pack):
    """Two frames per theme; returns (files, moved?)"""
    import cairo
    from backend.app.content_engine import paint_scene
    frames = []
    pixels = []
    for theme, colors in (("dark", DARK), ("light", LIGHT)):
        for at, suffix in ((0.2, ""), (1.8, "-t2")):
            surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
            cr = cairo.Context(surf)
            cr.set_source_rgb(*colors["bg"])
            cr.paint()
            paint_scene(cr, pack, W, H, colors, t=at)
            frames.append((theme, suffix, bytes(surf.get_data()),
                           surf))
            pixels.append(bytes(surf.get_data()))
    return frames, pixels[0] != pixels[1]


def worker(path, out_dir):
    result = {"file": str(path), "id": None, "problems": [],
              "notes": [], "previews": []}
    pack, err = load_one(path)
    if err:
        result["problems"].append(err)
        print("RESULT " + json.dumps(result))
        return 1
    result["id"] = pack.get("id")
    result["kind"] = pack.get("type", "game")
    result["code"] = callable(pack.get("_paint"))
    result["problems"] += scene_problems(pack)
    result["problems"] += playthrough(pack)
    try:
        frames, moved = render_frames(pack)
        out = pathlib.Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        for theme, suffix, _data, surf in frames:
            dest = out / f"{pack['id']}.{theme}{suffix}.png"
            surf.write_to_png(str(dest))
            result["previews"].append(str(dest))
        if not moved and result["kind"] == "quiz":
            result["notes"].append(
                "scene does not move — add animation "
                "(paint with view.t, or scene.anim for JSON)")
    except ImportError:
        result["notes"].append("pycairo missing — no previews")
    except Exception as exc:
        result["problems"].append(f"render failed: {exc}")
    print("RESULT " + json.dumps(result))
    return 1 if result["problems"] else 0


# ---------------------------------------------------------------------------
# Parent: discover files, run workers, report.
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="*", default=["games-base"],
                    help="game files or directories")
    ap.add_argument("--out", default="/tmp/game-previews",
                    help="where preview PNGs go")
    ap.add_argument("--worker", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.worker:
        return worker(args.worker, args.out)

    files = []
    for raw in args.paths or ["games-base"]:
        p = pathlib.Path(raw)
        if p.is_dir():
            files += sorted(p.rglob("*.json")) + sorted(p.rglob("*.py"))
        else:
            files.append(p)
    if not files:
        print("no games found")
        return 1
    failures = 0
    for f in files:
        try:
            proc = subprocess.run(
                [sys.executable, str(pathlib.Path(__file__)),
                 "--worker", str(f), "--out", args.out],
                capture_output=True, text=True, timeout=30)
            line = [ln for ln in proc.stdout.splitlines()
                    if ln.startswith("RESULT ")]
            if not line:
                raise RuntimeError(
                    (proc.stderr or proc.stdout).strip()[-400:])
            res = json.loads(line[-1][7:])
        except Exception as exc:
            print(f"FAIL {f}: worker failed: {exc}")
            failures += 1
            continue
        hard = res["problems"]
        failures += 1 if hard else 0
        kind = "code" if res.get("code") else "json"
        print(f"{'FAIL' if hard else 'OK  '} "
              f"{(res['id'] or f.name):<24} [{kind}] "
              f"kind={res.get('kind', 'quiz')}")
        for prob in hard:
            print(f"     - {prob}")
        for note in res["notes"]:
            print(f"     ~ {note}")
        if res["previews"]:
            print(f"     preview: {pathlib.Path(res['previews'][0]).parent}"
                  f"/{res['id']}.*.png — LOOK at it before shipping")
    print(f"\n{'ALL OK' if not failures else f'{failures} game(s) failed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
