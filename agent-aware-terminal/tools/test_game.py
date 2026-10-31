#!/usr/bin/env python3
"""Test content packs the way an authoring agent should.

For every pack (file or directory given; default games-base/):

1. schema validation (the engine's own rules),
2. a scripted playthrough — the correct target must score,
   every other target must fail, cards must not score,
3. scene sanity (diagram nodes in bounds and not overlapping),
4. preview renders (PNG, dark + light, two frames if animated)
   written to --out so the author can LOOK at the result.

Run with the system python3 (needs pycairo):

    python3 tools/test_game.py games-base/
    python3 tools/test_game.py my-new-game.json --out /tmp/previews

Exit code 1 when anything fails. See games-base/AGENTS.md.
"""
import argparse
import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DARK = {"square": (0.969, 0.973, 0.973), "text": (0.973, 0.976, 0.976),
        "muted": (0.541, 0.561, 0.596), "bg": (0.043, 0.047, 0.055)}
LIGHT = {"square": (0.078, 0.082, 0.09), "text": (0.043, 0.047, 0.055),
         "muted": (0.384, 0.4, 0.427), "bg": (0.976, 0.976, 0.98)}
W, H = 880, 620


def find_packs(paths):
    from backend.app.content_engine import validate_pack
    out = []
    for raw in paths:
        p = pathlib.Path(raw)
        files = sorted(p.rglob("*.json")) if p.is_dir() else [p]
        for f in files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception as exc:
                out.append((None, f"{f}: unreadable: {exc}"))
                continue
            pack, err = validate_pack(data, str(f))
            out.append((pack, f"{f}: {err}" if err else None))
    return out


def scene_problems(pack):
    from backend.app import content_engine as ce
    problems = []
    scene = pack.get("scene") or {}
    if not scene:
        return problems
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
    if not scene.get("anim"):
        problems.append("no scene animation (strongly recommended: "
                        "add scene.anim — see AGENTS.md)")
    return problems


def playthrough(pack):
    """Drive the real engine over a one-pack deck."""
    from backend.app.content_engine import ContentEngine, scene_targets
    problems = []
    with tempfile.TemporaryDirectory() as tmp:
        src = pack.get("_source") or "pack.json"
        name = pathlib.Path(src).name or "pack.json"
        pathlib.Path(tmp, name).write_text(json.dumps(
            {k: v for k, v in pack.items() if not k.startswith("_")}))
        engine = ContentEngine([tmp], seed=1)
        game = engine.next_game()
        if game is None:
            return ["engine would not serve the pack"]
        targets = [c["id"] for c in game.get("choices") or []] + \
            scene_targets(game)
        if game.get("kind", "quiz") == "card":
            out = engine.answer(targets[0] if targets else "x")
            if out["correct"] is not None:
                problems.append("card must not score")
            return problems
        if len(set(targets)) < 2:
            problems.append("quiz needs at least 2 answerable targets")
        out = engine.answer(game["answer"])
        if out["correct"] is not True:
            problems.append("the correct target does not score")
        if not out["explanation"]:
            problems.append("no explanation after a correct answer")
        for target in set(targets) - {game["answer"]}:
            engine2 = ContentEngine([tmp], seed=1)
            engine2.next_game()
            if engine2.answer(target)["correct"] is not False:
                problems.append(f"wrong target {target} scores")
                break
    return problems


def render(pack, out_dir):
    import cairo
    from backend.app.content_engine import paint_scene
    out_dir.mkdir(parents=True, exist_ok=True)
    animated = bool((pack.get("scene") or {}).get("anim"))
    frames = [(0.2, "")] + ([(1.8, "-t2")] if animated else [])
    written = []
    for theme, colors in (("dark", DARK), ("light", LIGHT)):
        for at, suffix in frames:
            surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
            cr = cairo.Context(surf)
            cr.set_source_rgb(*colors["bg"])
            cr.paint()
            paint_scene(cr, pack, W, H, colors, t=at)
            path = out_dir / f"{pack['id']}.{theme}{suffix}.png"
            surf.write_to_png(str(path))
            written.append(path)
    return written


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="*", default=["games-base"],
                    help="pack files or directories")
    ap.add_argument("--out", default="/tmp/game-previews",
                    help="where preview PNGs go")
    args = ap.parse_args()
    targets = args.paths or ["games-base"]
    failures = 0
    for pack, err in find_packs(targets):
        if err:
            print(f"FAIL {err}")
            failures += 1
            continue
        problems = scene_problems(pack) + playthrough(pack)
        hard = [p for p in problems if not p.startswith("no scene")]
        notes = [p for p in problems if p.startswith("no scene")]
        previews = []
        try:
            previews = render(pack, pathlib.Path(args.out))
        except ImportError:
            print("WARN pycairo missing — run with system python3 "
                  "for previews")
        status = "FAIL" if hard else "OK  "
        failures += 1 if hard else 0
        scene = (pack.get("scene") or {}).get("type", "none")
        print(f"{status} {pack['id']:<24} scene={scene:<8} "
              f"kind={pack.get('kind', 'quiz')}")
        for prob in hard:
            print(f"     - {prob}")
        for note in notes:
            print(f"     ~ {note}")
        if previews:
            print(f"     preview: {previews[0].parent}/"
                  f"{pack['id']}.*.png — LOOK at it before shipping")
    print(f"\n{'ALL OK' if not failures else f'{failures} pack(s) failed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
